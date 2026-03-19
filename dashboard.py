from __future__ import annotations

"""
Bottle web dashboard for SQLite data.

Run:
  python dashboard.py           # http://localhost:8080
  python dashboard.py --port 9000
  python dashboard.py --host 0.0.0.0 --port 8080

Pages:
  /                        - home: list all tables
  /table/<name>            - browse table with search, pagination, edit/delete buttons
  /table/<name>/new        - add a new row
  /table/<name>/edit/<id>  - edit a single row
  /table/<name>/delete/<id>- delete a single row (POST)
  /table/<name>/drop       - delete entire table (POST)
  /import                  - upload a CSV file → new/existing table
  /export/<name>           - download table as CSV
  /api/tables              - JSON: list of tables
  /api/table/<name>        - JSON: rows for table
"""

import csv
import html as _html
import io
import json
import re
import sqlite3
import sys
import argparse
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

try:
    import bottle
    from bottle import route, request, response, run, HTTPError
except ImportError:
    print("Missing dependency. Run:  pip install bottle")
    sys.exit(1)

DB_FILE = Path(__file__).parent / "sheets_data.db"


def h(value) -> str:
    """HTML-escape a value for safe inline rendering."""
    return _html.escape(str(value) if value is not None else "")


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def user_tables() -> list[str]:
    if not DB_FILE.exists():
        return []
    conn = get_conn()
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name NOT LIKE '\\_%' ESCAPE '\\' ORDER BY name"
    ).fetchall()
    conn.close()
    return [r["name"] for r in rows]


def last_sync_info() -> list[dict]:
    if not DB_FILE.exists():
        return []
    conn = get_conn()
    try:
        rows = conn.execute(
            """
            SELECT table_name, synced_at, row_count
            FROM _sync_log
            WHERE id IN (SELECT MAX(id) FROM _sync_log GROUP BY table_name)
            ORDER BY synced_at DESC
            """
        ).fetchall()
        return [dict(r) for r in rows]
    except sqlite3.OperationalError:
        return []
    finally:
        conn.close()


def ensure_meta_table(conn: sqlite3.Connection):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS _sync_log (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            table_name  TEXT NOT NULL,
            synced_at   TEXT NOT NULL,
            row_count   INTEGER NOT NULL
        )
    """)
    conn.commit()


def table_columns(table_name: str) -> list[str]:
    conn = get_conn()
    rows = conn.execute(f'PRAGMA table_info("{table_name}")').fetchall()
    conn.close()
    return [r["name"] for r in rows]


def fetch_rows(table_name: str, search: str = "", page: int = 1, per_page: int = 50):
    conn = get_conn()
    cols = table_columns(table_name)
    data_cols = [c for c in cols if not c.startswith("_")]

    where, params = "", []
    if search:
        clauses = [f'"{c}" LIKE ?' for c in data_cols]
        where = "WHERE " + " OR ".join(clauses)
        params = [f"%{search}%" for _ in data_cols]

    total = conn.execute(
        f'SELECT COUNT(*) FROM "{table_name}" {where}', params
    ).fetchone()[0]

    offset = (page - 1) * per_page
    rows = conn.execute(
        f'SELECT * FROM "{table_name}" {where} ORDER BY _row_num LIMIT ? OFFSET ?',
        params + [per_page, offset],
    ).fetchall()
    conn.close()
    return cols, [dict(r) for r in rows], total


# ---------------------------------------------------------------------------
# CSV / schema helpers (no dependency on sheets_sync.py)
# ---------------------------------------------------------------------------

def sanitize(name: str) -> str:
    name = re.sub(r"[^a-zA-Z0-9_]", "_", name.strip())
    if name and name[0].isdigit():
        name = "_" + name
    return name or "_col"


def make_unique_columns(headers: list[str]) -> list[str]:
    seen: dict[str, int] = {}
    result = []
    for h in headers:
        s = sanitize(h) or "_col"
        if s in seen:
            seen[s] += 1
            s = f"{s}_{seen[s]}"
        else:
            seen[s] = 0
        result.append(s)
    return result


def upsert_table(conn: sqlite3.Connection, table_name: str,
                 headers: list[str], rows: list[list], replace: bool = True):
    """Write rows into a table, creating/evolving schema as needed."""
    safe_table = sanitize(table_name)
    safe_cols = make_unique_columns(headers)
    col_defs = ", ".join(f'"{c}" TEXT' for c in safe_cols)

    conn.execute(f"""
        CREATE TABLE IF NOT EXISTS "{safe_table}" (
            _row_num   INTEGER PRIMARY KEY,
            _synced_at TEXT,
            {col_defs}
        )
    """)

    existing = {r[1] for r in conn.execute(f'PRAGMA table_info("{safe_table}")')}
    for col in safe_cols:
        if col not in existing:
            conn.execute(f'ALTER TABLE "{safe_table}" ADD COLUMN "{col}" TEXT')

    if replace:
        conn.execute(f'DELETE FROM "{safe_table}"')
        start_num = 1
    else:
        result = conn.execute(f'SELECT COALESCE(MAX(_row_num), 0) FROM "{safe_table}"').fetchone()
        start_num = result[0] + 1

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    placeholders = ", ".join("?" for _ in safe_cols)
    col_names = ", ".join(f'"{c}"' for c in safe_cols)

    for i, row in enumerate(rows, start=start_num):
        padded = list(row) + [""] * max(0, len(safe_cols) - len(row))
        padded = padded[: len(safe_cols)]
        conn.execute(
            f'INSERT OR REPLACE INTO "{safe_table}" '
            f'(_row_num, _synced_at, {col_names}) VALUES (?, ?, {placeholders})',
            [i, now] + padded,
        )

    conn.execute(
        "INSERT INTO _sync_log (table_name, synced_at, row_count) VALUES (?, ?, ?)",
        [safe_table, now, len(rows)],
    )
    conn.commit()
    return safe_table, len(rows)


# ---------------------------------------------------------------------------
# HTML / CSS
# ---------------------------------------------------------------------------

BASE_CSS = """
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  *, *::before, *::after { box-sizing: border-box; }
  body { font-family: system-ui, sans-serif; margin: 0; background: #f5f7fa; color: #1a1a2e; }
  header { background: #16213e; color: #eee; padding: 1rem 2rem; display: flex; align-items: center; gap: 1rem; }
  header a { color: #e2c275; text-decoration: none; font-weight: 600; font-size: 1.1rem; }
  header a:hover { text-decoration: underline; }
  .container { max-width: 1200px; margin: 0 auto; padding: 1.5rem; }
  h1 { margin: 0 0 1.2rem; font-size: 1.5rem; }
  .card { background: #fff; border-radius: 8px; box-shadow: 0 1px 4px rgba(0,0,0,.1);
          padding: 1.2rem 1.5rem; margin-bottom: 1rem; }
  table { width: 100%; border-collapse: collapse; font-size: .88rem; }
  th { background: #16213e; color: #e2c275; text-align: left; padding: .5rem .75rem; white-space: nowrap; }
  td { padding: .45rem .75rem; border-bottom: 1px solid #eee; vertical-align: top; }
  tr:hover td { background: #f0f4ff; }
  .badge { display: inline-block; background: #e2c275; color: #16213e; border-radius: 12px;
           padding: 2px 10px; font-size: .78rem; font-weight: 700; }
  .btn { display: inline-block; padding: .45rem 1.1rem; border-radius: 6px; font-size: .9rem;
         cursor: pointer; border: none; font-weight: 600; text-decoration: none; line-height: 1.4; }
  .btn-primary { background: #16213e; color: #e2c275; }
  .btn-primary:hover { background: #0f3460; }
  .btn-success { background: #1a7a4a; color: #fff; }
  .btn-success:hover { background: #145e38; }
  .btn-warning { background: #b8860b; color: #fff; }
  .btn-warning:hover { background: #9a720a; }
  .btn-danger  { background: #c0392b; color: #fff; }
  .btn-danger:hover  { background: #a93226; }
  .btn-sm { padding: .28rem .6rem; font-size: .78rem; }
  .btn-group { display: flex; gap: .35rem; flex-wrap: wrap; }
  input[type=text], input[type=search], input[type=file], select, textarea {
    padding: .4rem .8rem; border: 1px solid #ccc; border-radius: 6px;
    font-size: .9rem; font-family: inherit; }
  input[type=text], input[type=search] { width: 100%; max-width: 350px; }
  textarea { width: 100%; resize: vertical; }
  .form-group { margin-bottom: .9rem; }
  .form-group label { display: block; font-size: .85rem; font-weight: 600; margin-bottom: .3rem; }
  .search-bar { display: flex; gap: .5rem; margin-bottom: 1rem; align-items: center; }
  .pagination { display: flex; gap: .4rem; margin-top: 1rem; flex-wrap: wrap; align-items: center; }
  .pagination a, .pagination span { padding: .35rem .75rem; border-radius: 5px;
    text-decoration: none; border: 1px solid #ccc; font-size: .85rem; }
  .pagination a:hover { background: #16213e; color: #e2c275; border-color: #16213e; }
  .pagination .current { background: #16213e; color: #e2c275; border-color: #16213e; }
  .empty { color: #888; font-style: italic; padding: 1rem 0; }
  .meta { font-size: .8rem; color: #777; margin-top: .3rem; }
  .table-link { font-weight: 600; color: #0f3460; text-decoration: none; }
  .table-link:hover { text-decoration: underline; }
  .alert { padding: .75rem 1rem; border-radius: 6px; margin-bottom: 1rem; }
  .alert-success { background: #d4edda; color: #155724; }
  .alert-error   { background: #f8d7da; color: #721c24; }
  .toolbar { display: flex; gap: .5rem; align-items: center; flex-wrap: wrap; margin-bottom: .8rem; }
  .toolbar-right { margin-left: auto; display: flex; gap: .5rem; flex-wrap: wrap; }
  .edit-td { white-space: nowrap; }
</style>
"""


def nav(extra=""):
    return f"""<header>
  <span style="font-size:1.4rem">📊</span>
  <a href="/">Dashboard</a>
  {extra}
</header>"""


def flash(msg, msg_type="success"):
    if not msg:
        return ""
    return f'<div class="alert alert-{msg_type}">{msg}</div>'


# ---------------------------------------------------------------------------
# Route: home
# ---------------------------------------------------------------------------

@route("/")
def index():
    tables = user_tables()
    sync_info = {r["table_name"]: r for r in last_sync_info()}
    msg = request.query.get("msg", "")
    msg_type = request.query.get("type", "success")

    rows_html = ""
    if tables:
        for t in tables:
            info = sync_info.get(t, {})
            synced = info.get("synced_at", "—")
            count = info.get("row_count", "?")
            rows_html += f"""
            <tr>
              <td><a class="table-link" href="/table/{t}">{h(t)}</a></td>
              <td><span class="badge">{h(count)}</span></td>
              <td style="font-size:.82rem;color:#555">{h(synced)}</td>
              <td>
                <div class="btn-group">
                  <a class="btn btn-primary btn-sm" href="/table/{t}">Browse</a>
                  <a class="btn btn-success btn-sm" href="/table/{t}/new">+ Add row</a>
                  <a class="btn btn-success btn-sm" href="/export/{t}">⬇ CSV</a>
                  <a class="btn btn-sm" style="background:#eee" href="/api/table/{t}">JSON</a>
                  <form style="display:inline" method="POST" action="/table/{t}/drop"
                        onsubmit="return confirm('Delete table {h(t)} and all its rows?')">
                    <button class="btn btn-danger btn-sm" type="submit">Delete table</button>
                  </form>
                </div>
              </td>
            </tr>"""
    else:
        rows_html = '<tr><td colspan="4" class="empty">No tables yet — import a CSV to get started.</td></tr>'

    return f"""<!DOCTYPE html>
<html>
<head>{BASE_CSS}<title>Dashboard</title></head>
<body>
{nav()}
<div class="container">
  <h1>Dashboard</h1>
  {flash(msg, msg_type)}
  <div class="card">
    <div class="toolbar">
      <strong>Tables</strong>
      <div class="toolbar-right">
        <a class="btn btn-success" href="/import">⬆ Import CSV</a>
      </div>
    </div>
    <table>
      <thead><tr><th>Table</th><th>Rows</th><th>Last Modified</th><th>Actions</th></tr></thead>
      <tbody>{rows_html}</tbody>
    </table>
  </div>
  <p class="meta">DB: {DB_FILE} &nbsp;|&nbsp; <a href="/api/tables">tables JSON</a></p>
</div>
</body></html>"""


# ---------------------------------------------------------------------------
# Route: browse table
# ---------------------------------------------------------------------------

@route("/table/<name>")
def browse_table(name):
    if name not in user_tables():
        raise HTTPError(404, f"Table '{name}' not found")

    search = request.query.get("q", "")
    try:
        page = max(1, int(request.query.get("page", 1)))
    except ValueError:
        page = 1
    per_page = 50
    msg = request.query.get("msg", "")
    msg_type = request.query.get("type", "success")

    cols, rows, total = fetch_rows(name, search, page, per_page)
    data_cols = [c for c in cols if not c.startswith("_")]
    total_pages = max(1, (total + per_page - 1) // per_page)

    thead = "".join(f"<th>{c}</th>" for c in data_cols) + "<th></th>"

    tbody = ""
    if rows:
        for r in rows:
            row_num = r.get("_row_num", "")
            cells = "".join(f"<td>{h(r.get(c, ''))}</td>" for c in data_cols)
            edit_url = f"/table/{name}/edit/{row_num}"
            del_form = (
                f'<form style="display:inline" method="POST" action="/table/{name}/delete/{row_num}"'
                f' onsubmit="return confirm(\'Delete row {row_num}?\')">'
                f'<button class="btn btn-danger btn-sm" type="submit">Delete</button></form>'
            )
            tbody += (
                f'<tr>{cells}'
                f'<td class="edit-td" style="white-space:nowrap">'
                f'<div class="btn-group">'
                f'<a class="btn btn-warning btn-sm" href="{edit_url}">Edit</a>'
                f'{del_form}'
                f'</div>'
                f'</td></tr>'
            )
    else:
        tbody = f'<tr><td colspan="{len(data_cols)+1}" class="empty">No rows found.</td></tr>'

    def page_link(p, label=None):
        label = label or str(p)
        css = "current" if p == page else ""
        return f'<a class="{css}" href="/table/{name}?q={quote(search)}&page={p}">{label}</a>'

    pag = ""
    if total_pages > 1:
        if page > 1:
            pag += page_link(page - 1, "‹ Prev")
        for p in range(max(1, page - 3), min(total_pages, page + 3) + 1):
            pag += page_link(p)
        if page < total_pages:
            pag += page_link(page + 1, "Next ›")

    showing = f"Showing {len(rows)} of {total} rows"
    if search:
        showing += f" matching &ldquo;{search}&rdquo;"

    clear_btn = (
        f'<a class="btn btn-sm" style="background:#eee" href="/table/{name}">Clear</a>'
        if search else ""
    )

    return f"""<!DOCTYPE html>
<html>
<head>{BASE_CSS}<title>{h(name)} – Dashboard</title></head>
<body>
{nav(f'<span style="color:#aaa">/ <a style="color:#e2c275" href="/table/{name}">{name}</a></span>')}
<div class="container">
  <h1>Table: {name}</h1>
  {flash(msg, msg_type)}
  <div class="card">
    <div class="toolbar">
      <form class="search-bar" method="GET" action="/table/{name}" style="margin:0">
        <input type="search" name="q" value="{search}" placeholder="Search all columns…">
        <button class="btn btn-primary" type="submit">Search</button>
        {clear_btn}
      </form>
      <div class="toolbar-right">
        <a class="btn btn-success btn-sm" href="/table/{name}/new">+ Add row</a>
        <a class="btn btn-success btn-sm" href="/export/{name}">⬇ Export CSV</a>
        <a class="btn btn-sm" style="background:#eee" href="/api/table/{name}">JSON</a>
      </div>
    </div>
    <p class="meta">{showing}</p>
    <div style="overflow-x:auto">
      <table>
        <thead><tr>{thead}</tr></thead>
        <tbody>{tbody}</tbody>
      </table>
    </div>
    <div class="pagination">{pag}</div>
  </div>
  <p class="meta"><a href="/">← Back to all tables</a></p>
</div>
</body></html>"""


# ---------------------------------------------------------------------------
# Route: edit a row
# ---------------------------------------------------------------------------

@route("/table/<name>/edit/<row_num:int>", method=["GET", "POST"])
def edit_row(name, row_num):
    if name not in user_tables():
        raise HTTPError(404, f"Table '{name}' not found")

    cols = table_columns(name)
    data_cols = [c for c in cols if not c.startswith("_")]

    conn = get_conn()

    if request.method == "POST":
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        assignments = ", ".join(f'"{c}" = ?' for c in data_cols)
        values = [request.forms.get(c, "") for c in data_cols]
        conn.execute(
            f'UPDATE "{name}" SET {assignments}, _synced_at = ? WHERE _row_num = ?',
            values + [now, row_num],
        )
        conn.commit()
        conn.close()
        bottle.redirect(f"/table/{name}?msg=Row+{row_num}+updated.&type=success")

    # GET — load current values
    row = conn.execute(
        f'SELECT * FROM "{name}" WHERE _row_num = ?', [row_num]
    ).fetchone()
    conn.close()

    if row is None:
        raise HTTPError(404, f"Row {row_num} not found in table '{name}'")

    row = dict(row)
    fields_html = ""
    for c in data_cols:
        val = row.get(c, "")
        if len(str(val)) > 80:
            widget = f'<textarea name="{c}" rows="3">{h(val)}</textarea>'
        else:
            widget = f'<input type="text" name="{c}" value="{h(val)}">'
        fields_html += f'<div class="form-group"><label>{h(c)}</label>{widget}</div>'

    return f"""<!DOCTYPE html>
<html>
<head>{BASE_CSS}<title>Edit row {row_num} – {h(name)}</title></head>
<body>
{nav(f'<span style="color:#aaa">/ <a style="color:#e2c275" href="/table/{name}">{h(name)}</a> / edit row {row_num}</span>')}
<div class="container">
  <h1>Edit row {row_num} in &ldquo;{h(name)}&rdquo;</h1>
  <div class="card" style="max-width:700px">
    <form method="POST" action="/table/{name}/edit/{row_num}">
      {fields_html}
      <div class="btn-group">
        <button class="btn btn-warning" type="submit">Save changes</button>
        <a class="btn btn-sm" style="background:#eee" href="/table/{name}">Cancel</a>
      </div>
    </form>
    <hr style="margin:1.2rem 0;border:none;border-top:1px solid #eee">
    <form method="POST" action="/table/{name}/delete/{row_num}"
          onsubmit="return confirm('Permanently delete row {row_num}?')">
      <button class="btn btn-danger btn-sm" type="submit">Delete this row</button>
    </form>
  </div>
</div>
</body></html>"""


# ---------------------------------------------------------------------------
# Route: add a new row
# ---------------------------------------------------------------------------

@route("/table/<name>/new", method=["GET", "POST"])
def new_row(name):
    if name not in user_tables():
        raise HTTPError(404, f"Table '{name}' not found")

    cols = table_columns(name)
    data_cols = [c for c in cols if not c.startswith("_")]
    conn = get_conn()

    if request.method == "POST":
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        result = conn.execute(
            f'SELECT COALESCE(MAX(_row_num), 0) + 1 FROM "{name}"'
        ).fetchone()
        next_num = result[0]
        col_names = ", ".join(f'"{c}"' for c in data_cols)
        placeholders = ", ".join("?" for _ in data_cols)
        values = [request.forms.get(c, "") for c in data_cols]
        conn.execute(
            f'INSERT INTO "{name}" (_row_num, _synced_at, {col_names}) VALUES (?, ?, {placeholders})',
            [next_num, now] + values,
        )
        conn.commit()
        conn.close()
        bottle.redirect(f"/table/{name}?msg=Row+added.&type=success")

    conn.close()
    fields_html = "".join(
        f'<div class="form-group"><label>{h(c)}</label>'
        f'<input type="text" name="{c}" value=""></div>'
        for c in data_cols
    )

    return f"""<!DOCTYPE html>
<html>
<head>{BASE_CSS}<title>Add row – {h(name)}</title></head>
<body>
{nav(f'<span style="color:#aaa">/ <a style="color:#e2c275" href="/table/{name}">{h(name)}</a> / new row</span>')}
<div class="container">
  <h1>Add row to &ldquo;{h(name)}&rdquo;</h1>
  <div class="card" style="max-width:700px">
    <form method="POST" action="/table/{name}/new">
      {fields_html}
      <div class="btn-group">
        <button class="btn btn-success" type="submit">Add row</button>
        <a class="btn btn-sm" style="background:#eee" href="/table/{name}">Cancel</a>
      </div>
    </form>
  </div>
</div>
</body></html>"""


# ---------------------------------------------------------------------------
# Route: delete a row
# ---------------------------------------------------------------------------

@route("/table/<name>/delete/<row_num:int>", method="POST")
def delete_row(name, row_num):
    if name not in user_tables():
        raise HTTPError(404, f"Table '{name}' not found")
    conn = get_conn()
    conn.execute(f'DELETE FROM "{name}" WHERE _row_num = ?', [row_num])
    conn.commit()
    conn.close()
    bottle.redirect(f"/table/{name}?msg=Row+{row_num}+deleted.&type=success")


# ---------------------------------------------------------------------------
# Route: drop an entire table
# ---------------------------------------------------------------------------

@route("/table/<name>/drop", method="POST")
def drop_table(name):
    if name not in user_tables():
        raise HTTPError(404, f"Table '{name}' not found")
    conn = get_conn()
    conn.execute(f'DROP TABLE IF EXISTS "{name}"')
    conn.execute("DELETE FROM _sync_log WHERE table_name = ?", [name])
    conn.commit()
    conn.close()
    bottle.redirect(f"/?msg=Table+{quote(name)}+deleted.&type=success")


# ---------------------------------------------------------------------------
# Route: CSV export
# ---------------------------------------------------------------------------

@route("/export/<name>")
def export_csv(name):
    if name not in user_tables():
        raise HTTPError(404, f"Table '{name}' not found")

    cols, rows, _ = fetch_rows(name, per_page=1_000_000)
    data_cols = [c for c in cols if not c.startswith("_")]

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(data_cols)
    for r in rows:
        writer.writerow([r.get(c, "") for c in data_cols])

    response.content_type = "text/csv; charset=utf-8"
    response.headers["Content-Disposition"] = f'attachment; filename="{name}.csv"'
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Route: CSV import
# ---------------------------------------------------------------------------

@route("/import", method="GET")
def import_form():
    tables = user_tables()
    if tables:
        links = ", ".join('<a href="/table/' + t + '">' + t + "</a>" for t in tables)
        tables_note = "<p class='meta'>Existing tables: " + links + "</p>"
    else:
        tables_note = ""

    return f"""<!DOCTYPE html>
<html>
<head>{BASE_CSS}<title>Import CSV – Dashboard</title></head>
<body>
{nav('<span style="color:#aaa">/ import CSV</span>')}
<div class="container">
  <h1>Import CSV</h1>
  <div class="card" style="max-width:600px">
    <form method="POST" action="/import" enctype="multipart/form-data">

      <div class="form-group">
        <label>CSV file <span style="color:#c0392b">*</span></label>
        <input type="file" name="csvfile" accept=".csv,text/csv" required>
        <p class="meta">First row must be column headers.</p>
      </div>

      <div class="form-group">
        <label>Target table name</label>
        <input type="text" name="table_name" placeholder="Leave blank to use filename">
        <p class="meta">Letters, digits, underscores only. Existing table = schema-evolve + replace.</p>
      </div>

      <div class="form-group">
        <label>Import mode</label>
        <select name="mode">
          <option value="replace">Replace — clear existing rows, write CSV rows</option>
          <option value="append">Append — add CSV rows after existing rows</option>
        </select>
      </div>

      <div class="btn-group">
        <button class="btn btn-success" type="submit">⬆ Import</button>
        <a class="btn btn-sm" style="background:#eee" href="/">Cancel</a>
      </div>
    </form>
  </div>
  {tables_note}
</div>
</body></html>"""


@route("/import", method="POST")
def import_csv():
    upload = request.files.get("csvfile")
    if not upload or not upload.filename:
        bottle.redirect("/import?msg=No+file+selected.&type=error")

    raw_name = request.forms.get("table_name", "").strip()
    if not raw_name:
        raw_name = Path(upload.filename).stem
    table_name = sanitize(raw_name) or "imported"
    mode = request.forms.get("mode", "replace")

    try:
        content = upload.file.read().decode("utf-8-sig")  # strip BOM if present
        reader = csv.reader(io.StringIO(content))
        all_rows = list(reader)
    except Exception as e:
        msg = quote(f"Could not parse CSV: {e}")
        bottle.redirect(f"/import?msg={msg}&type=error")

    if len(all_rows) < 1:
        bottle.redirect("/import?msg=CSV+is+empty.&type=error")

    headers = all_rows[0]
    data_rows = all_rows[1:]

    conn = get_conn()
    ensure_meta_table(conn)
    try:
        safe_table, n = upsert_table(conn, table_name, headers, data_rows,
                                     replace=(mode == "replace"))
    except Exception as e:
        conn.close()
        msg = quote(f"Import error: {e}")
        bottle.redirect(f"/import?msg={msg}&type=error")
    conn.close()

    msg = quote(f"Imported {n} rows into table '{safe_table}'.")
    bottle.redirect(f"/table/{safe_table}?msg={msg}&type=success")


# ---------------------------------------------------------------------------
# API routes
# ---------------------------------------------------------------------------

@route("/api/tables")
def api_tables():
    response.content_type = "application/json"
    sync_info = {r["table_name"]: r for r in last_sync_info()}
    return json.dumps({
        "tables": [
            {
                "name": t,
                "last_synced": sync_info.get(t, {}).get("synced_at"),
                "row_count": sync_info.get(t, {}).get("row_count"),
            }
            for t in user_tables()
        ]
    }, indent=2)


@route("/api/table/<name>")
def api_table(name):
    response.content_type = "application/json"
    if name not in user_tables():
        response.status = 404
        return json.dumps({"error": f"Table '{name}' not found"})
    cols, rows, total = fetch_rows(name, per_page=10_000)
    data_cols = [c for c in cols if not c.startswith("_")]
    data = [{c: r[c] for c in data_cols} for r in rows]
    return json.dumps({"table": name, "total": total, "rows": data}, indent=2)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Data Dashboard (Bottle)")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()
    print(f"Dashboard running at  http://{args.host}:{args.port}")
    run(host=args.host, port=args.port, debug=args.debug, reloader=args.debug)


if __name__ == "__main__":
    main()
