"""
Google Sheets -> SQLite sync script.

Setup:
  1. Create a Google Cloud project, enable the Sheets API & Drive API.
  2. Create a Service Account, download the JSON key, save as credentials.json.
  3. Share your spreadsheet with the service account email (Viewer is enough).
  4. Copy config.example.json -> config.json and fill in your values.

Run:
  python sheets_sync.py           # sync all configured sheets
  python sheets_sync.py --once    # same, exit when done (default)
  python sheets_sync.py --watch 60  # re-sync every 60 seconds
"""

import json
import sqlite3
import time
import argparse
import logging
import sys
from datetime import datetime
from pathlib import Path

try:
    import gspread
    from google.oauth2.service_account import Credentials
except ImportError:
    print("Missing dependencies. Run:  pip install gspread google-auth")
    sys.exit(1)

CONFIG_FILE = Path(__file__).parent / "config.json"
DB_FILE = Path(__file__).parent / "sheets_data.db"

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets.readonly",
    "https://www.googleapis.com/auth/drive.readonly",
]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("sheets_sync")


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def load_config() -> dict:
    if not CONFIG_FILE.exists():
        log.error("config.json not found. Copy config.example.json and fill it in.")
        sys.exit(1)
    with open(CONFIG_FILE) as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# Google Sheets helpers
# ---------------------------------------------------------------------------

def get_client(credentials_file: str) -> gspread.Client:
    creds = Credentials.from_service_account_file(credentials_file, scopes=SCOPES)
    return gspread.authorize(creds)


def fetch_sheet(client: gspread.Client, spreadsheet_id: str, worksheet_name: str | None = None):
    """Return (headers, rows) from a worksheet."""
    spreadsheet = client.open_by_key(spreadsheet_id)
    if worksheet_name:
        ws = spreadsheet.worksheet(worksheet_name)
    else:
        ws = spreadsheet.sheet1
    all_values = ws.get_all_values()
    if not all_values:
        return [], []
    headers = [h.strip() for h in all_values[0]]
    rows = all_values[1:]
    return headers, rows


# ---------------------------------------------------------------------------
# SQLite helpers
# ---------------------------------------------------------------------------

def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


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


def sanitize(name: str) -> str:
    """Turn arbitrary sheet/column names into safe SQL identifiers."""
    import re
    name = re.sub(r"[^a-zA-Z0-9_]", "_", name.strip())
    if name and name[0].isdigit():
        name = "_" + name
    return name or "_col"


def make_unique_columns(headers: list[str]) -> list[str]:
    """Ensure column names are unique after sanitization."""
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


def sync_to_db(conn: sqlite3.Connection, table_name: str, headers: list[str], rows: list[list]):
    safe_table = sanitize(table_name)
    safe_cols = make_unique_columns(headers)

    # Always include _row_num for ordering + _synced_at for freshness
    col_defs = ", ".join(f'"{c}" TEXT' for c in safe_cols)
    conn.execute(f"""
        CREATE TABLE IF NOT EXISTS "{safe_table}" (
            _row_num   INTEGER PRIMARY KEY,
            _synced_at TEXT,
            {col_defs}
        )
    """)

    # Add any new columns that didn't exist before (schema evolution)
    existing = {row[1] for row in conn.execute(f'PRAGMA table_info("{safe_table}")')}
    for col in safe_cols:
        if col not in existing:
            log.info("Adding new column %s.%s", safe_table, col)
            conn.execute(f'ALTER TABLE "{safe_table}" ADD COLUMN "{col}" TEXT')

    conn.execute(f'DELETE FROM "{safe_table}"')

    now = datetime.utcnow().isoformat(timespec="seconds") + "Z"
    placeholders = ", ".join("?" for _ in safe_cols)
    col_names = ", ".join(f'"{c}"' for c in safe_cols)

    for i, row in enumerate(rows, start=1):
        # Pad or trim row to match header count
        padded = list(row) + [""] * max(0, len(safe_cols) - len(row))
        padded = padded[: len(safe_cols)]
        conn.execute(
            f'INSERT INTO "{safe_table}" (_row_num, _synced_at, {col_names}) VALUES (?, ?, {placeholders})',
            [i, now] + padded,
        )

    conn.execute(
        "INSERT INTO _sync_log (table_name, synced_at, row_count) VALUES (?, ?, ?)",
        [safe_table, now, len(rows)],
    )
    conn.commit()
    log.info("Synced %d rows -> table '%s'", len(rows), safe_table)


# ---------------------------------------------------------------------------
# Main sync loop
# ---------------------------------------------------------------------------

def sync_all(config: dict):
    client = get_client(config["credentials_file"])
    conn = get_conn()
    ensure_meta_table(conn)

    for sheet_cfg in config["sheets"]:
        spreadsheet_id = sheet_cfg["spreadsheet_id"]
        worksheet = sheet_cfg.get("worksheet")          # None = first sheet
        table_name = sheet_cfg.get("table_name") or (worksheet or "sheet1")

        log.info("Fetching spreadsheet=%s worksheet=%s", spreadsheet_id, worksheet or "(first)")
        try:
            headers, rows = fetch_sheet(client, spreadsheet_id, worksheet)
            if not headers:
                log.warning("Sheet is empty, skipping.")
                continue
            sync_to_db(conn, table_name, headers, rows)
        except gspread.exceptions.APIError as e:
            log.error("Sheets API error: %s", e)
        except Exception as e:
            log.error("Unexpected error: %s", e, exc_info=True)

    conn.close()


def main():
    parser = argparse.ArgumentParser(description="Sync Google Sheets to local SQLite DB")
    parser.add_argument("--watch", type=int, metavar="SECONDS",
                        help="Re-sync every N seconds (omit for one-shot)")
    args = parser.parse_args()

    config = load_config()

    if args.watch:
        log.info("Watch mode: syncing every %d seconds. Ctrl-C to stop.", args.watch)
        while True:
            sync_all(config)
            time.sleep(args.watch)
    else:
        sync_all(config)
        log.info("Done.")


if __name__ == "__main__":
    main()
