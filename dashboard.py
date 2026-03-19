"""
Bottle web dashboard for sheets_sync data.

Run:
  python dashboard.py           # http://localhost:8080
  python dashboard.py --port 9000
  python dashboard.py --host 0.0.0.0 --port 8080

Pages:
  /               - home: list all synced tables + last sync time
  /table/<name>   - browse full table with search & pagination
  /api/tables     - JSON: list of tables
  /api/table/<n>  - JSON: rows for table
  /sync           - POST: trigger a manual re-sync
"""

import sqlite3
import json
import subprocess
import sys
import argparse
from datetime import datetime
from pathlib import Path

try:
    import bottle
    from bottle import route, request, response, run, template, static_file, HTTPError
except ImportError:
    print("Missing dependency. Run:  pip install bottle")
    sys.exit(1)

DB_FILE = Path(__file__).parent / "sheets_data.db"
SYNC_SCRIPT = Path(__file__).parent / "sheets_sync.py"


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn


def user_tables() -> list[str]:
    if not DB_FILE.exists():
        return []
    conn = get_conn()
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE '\\_%' ESCAPE '\\' ORDER BY name"
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
            WHERE id IN (
                SELECT MAX(id) FROM _sync_log GROUP BY table_name
            )
            ORDER BY synced_at DESC
            """
        ).fetchall()
        return [dict(r) for r in rows]
    except sqlite3.OperationalError:
        return []
    finally:
        conn.close()


def table_columns(table_name: str) -> list[str]:
    conn = get_conn()
    rows = conn.execute(f'PRAGMA table_info("{table_name}")').fetchall()
    conn.close()
    return [r["name"] for r in rows]


def fetch_rows(table_name: str, search: str = "", page: int = 1, per_page: int = 50):
    conn = get_conn()
    cols = table_columns(table_name)
    data_cols = [c for c in cols if not c.startswith("_")]

    where = ""
    params: list = []
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
# HTML helpers  (inline templates — no separate files needed)
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
  h2 { font-size: 1.2rem; margin: 1.5rem 0 0.5rem; }
  .card { background: #fff; border-radius: 8px; box-shadow: 0 1px 4px rgba(0,0,0,.1); padding: 1.2rem 1.5rem; margin-bottom: 1rem; }
  table { width: 100%; border-collapse: collapse; font-size: .88rem; }
  th { background: #16213e; color: #e2c275; text-align: left; padding: .5rem .75rem; white-space: nowrap; }
  td { padding: .45rem .75rem; border-bottom: 1px solid #eee; }
  tr:hover td { background: #f0f4ff; }
  .badge { display: inline-block; background: #e2c275; color: #16213e; border-radius: 12px;
           padding: 2px 10px; font-size: .78rem; font-weight: 700; }
  .btn { display: inline-block; padding: .45rem 1.1rem; border-radius: 6px; font-size: .9rem;
         cursor: pointer; border: none; font-weight: 600; text-decoration: none; }
  .btn-primary { background: #16213e; color: #e2c275; }
  .btn-primary:hover { background: #0f3460; }
  .btn-danger  { background: #c0392b; color: #fff; }
  .btn-sm { padding: .3rem .7rem; font-size: .8rem; }
  input[type=text], input[type=search] { padding: .4rem .8rem; border: 1px solid #ccc;
    border-radius: 6px; font-size: .9rem; width: 100%; max-width: 350px; }
  .search-bar { display: flex; gap: .5rem; margin-bottom: 1rem; align-items: center; }
  .pagination { display: flex; gap: .4rem; margin-top: 1rem; flex-wrap: wrap; align-items: center; }
  .pagination a, .pagination span { padding: .35rem .75rem; border-radius: 5px;
    text-decoration: none; border: 1px solid #ccc; font-size: .85rem; }
  .pagination a:hover { background: #16213e; color: #e2c275; border-color: #16213e; }
  .pagination .current { background: #16213e; color: #e2c275; border-color: #16213e; }
  .empty { color: #888; font-style: italic; padding: 1rem 0; }
  .sync-form { display: inline; }
  .meta { font-size: .8rem; color: #777; margin-top: .3rem; }
  .table-link { font-weight: 600; color: #0f3460; text-decoration: none; }
  .table-link:hover { text-decoration: underline; }
  .alert { padding: .75rem 1rem; border-radius: 6px; margin-bottom: 1rem; }
  .alert-success { background: #d4edda; color: #155724; }
  .alert-error   { background: #f8d7da; color: #721c24; }
</style>
"""


def nav(extra=""):
    return f"""
    <header>
      <span style="font-size:1.4rem">📊</span>
      <a href="/">Sheets Dashboard</a>
      {extra}
    </header>"""


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@route("/")
def index():
    tables = user_tables()
    sync_info = {r["table_name"]: r for r in last_sync_info()}
    msg = request.query.get("msg", "")
    msg_type = request.query.get("type", "success")

    alert = ""
    if msg:
        alert = f'<div class="alert alert-{msg_type}">{msg}</div>'

    rows_html = ""
    if tables:
        for t in tables:
            info = sync_info.get(t, {})
            synced = info.get("synced_at", "never")
            count = info.get("row_count", "?")
            rows_html += f"""
            <tr>
              <td><a class="table-link" href="/table/{t}">{t}</a></td>
              <td><span class="badge">{count}</span> rows</td>
              <td>{synced}</td>
              <td>
                <a class="btn btn-primary btn-sm" href="/table/{t}">Browse</a>
                <a class="btn btn-sm" style="background:#eee" href="/api/table/{t}">JSON</a>
              </td>
            </tr>"""
    else:
        rows_html = '<tr><td colspan="4" class="empty">No tables found. Run sheets_sync.py first.</td></tr>'

    return f"""<!DOCTYPE html>
<html>
<head>{BASE_CSS}<title>Sheets Dashboard</title></head>
<body>
{nav()}
<div class="container">
  <h1>Google Sheets Dashboard</h1>
  {alert}
  <div class="card">
    <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:.8rem">
      <strong>Synced Tables</strong>
      <form class="sync-form" method="POST" action="/sync">
        <button class="btn btn-primary" type="submit">↻ Sync Now</button>
      </form>
    </div>
    <table>
      <thead><tr><th>Table</th><th>Rows</th><th>Last Synced</th><th>Actions</th></tr></thead>
      <tbody>{rows_html}</tbody>
    </table>
  </div>
  <p class="meta">DB: {DB_FILE} &nbsp;|&nbsp; <a href="/api/tables">tables JSON</a></p>
</div>
</body></html>"""


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

    cols, rows, total = fetch_rows(name, search, page, per_page)
    data_cols = [c for c in cols if not c.startswith("_")]
    total_pages = max(1, (total + per_page - 1) // per_page)

    # Table head
    thead = "".join(f"<th>{c}</th>" for c in data_cols)

    # Table body
    tbody = ""
    if rows:
        for r in rows:
            cells = "".join(f"<td>{r.get(c, '')}</td>" for c in data_cols)
            tbody += f"<tr>{cells}</tr>"
    else:
        tbody = f'<tr><td colspan="{len(data_cols)}" class="empty">No rows found.</td></tr>'

    # Pagination
    def page_link(p, label=None):
        label = label or str(p)
        css = "current" if p == page else ""
        href = f"/table/{name}?q={search}&page={p}"
        return f'<a class="{css}" href="{href}">{label}</a>'

    pag = ""
    if total_pages > 1:
        if page > 1:
            pag += page_link(page - 1, "‹ Prev")
        start = max(1, page - 3)
        end = min(total_pages, page + 3)
        for p in range(start, end + 1):
            pag += page_link(p)
        if page < total_pages:
            pag += page_link(page + 1, "Next ›")

    showing = f"Showing {len(rows)} of {total} rows"
    if search:
        showing += f" matching &ldquo;{search}&rdquo;"

    return f"""<!DOCTYPE html>
<html>
<head>{BASE_CSS}<title>{name} – Sheets Dashboard</title></head>
<body>
{nav(f'<span style="color:#aaa">/ <a style="color:#e2c275" href="/table/{name}">{name}</a></span>')}
<div class="container">
  <h1>Table: {name}</h1>
  <div class="card">
    <form class="search-bar" method="GET" action="/table/{name}">
      <input type="search" name="q" value="{search}" placeholder="Search all columns…">
      <button class="btn btn-primary" type="submit">Search</button>
      {'<a class="btn btn-sm" style="background:#eee" href="/table/' + name + '">Clear</a>' if search else ''}
    </form>
    <p class="meta">{showing}</p>
    <div style="overflow-x:auto">
      <table>
        <thead><tr>{thead}</tr></thead>
        <tbody>{tbody}</tbody>
      </table>
    </div>
    <div class="pagination">{pag}</div>
  </div>
  <p class="meta">
    <a href="/">← Back</a> &nbsp;|&nbsp;
    <a href="/api/table/{name}">JSON export</a>
  </p>
</div>
</body></html>"""


# ---------------------------------------------------------------------------
# API routes
# ---------------------------------------------------------------------------

@route("/api/tables")
def api_tables():
    response.content_type = "application/json"
    sync_info = {r["table_name"]: r for r in last_sync_info()}
    tables = user_tables()
    return json.dumps({
        "tables": [
            {
                "name": t,
                "last_synced": sync_info.get(t, {}).get("synced_at"),
                "row_count": sync_info.get(t, {}).get("row_count"),
            }
            for t in tables
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
# Manual sync trigger
# ---------------------------------------------------------------------------

@route("/sync", method="POST")
def trigger_sync():
    try:
        result = subprocess.run(
            [sys.executable, str(SYNC_SCRIPT)],
            capture_output=True, text=True, timeout=120
        )
        if result.returncode == 0:
            msg = "Sync completed successfully."
            msg_type = "success"
        else:
            msg = f"Sync failed: {result.stderr[:200]}"
            msg_type = "error"
    except subprocess.TimeoutExpired:
        msg = "Sync timed out after 120 seconds."
        msg_type = "error"
    except Exception as e:
        msg = f"Error: {e}"
        msg_type = "error"

    from urllib.parse import quote
    bottle.redirect(f"/?msg={quote(msg)}&type={msg_type}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Sheets Dashboard (Bottle)")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()

    print(f"Dashboard running at  http://{args.host}:{args.port}")
    run(host=args.host, port=args.port, debug=args.debug, reloader=args.debug)


if __name__ == "__main__":
    main()
