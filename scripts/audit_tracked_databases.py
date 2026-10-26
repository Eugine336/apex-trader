#!/usr/bin/env python3
"""Read-only inventory of tracked SQLite artifacts. Never emits row contents."""
from __future__ import annotations

import subprocess
import sqlite3
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[1]
tracked = subprocess.run(
    ["git", "ls-files", "*.db"], cwd=ROOT, capture_output=True, text=True, check=True
).stdout.splitlines()
print(f"TRACKED_DATABASE_COUNT={len(tracked)}")
for rel in tracked:
    path = ROOT / rel
    print(f"\nDATABASE={rel} SIZE_BYTES={path.stat().st_size if path.exists() else 'MISSING'}")
    if not path.exists():
        print("STATUS=MISSING")
        continue
    uri = "file:" + quote(str(path.resolve()), safe="/") + "?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=1.0)
        try:
            for check_name, pragma in (
                ("QUICK_CHECK", "PRAGMA quick_check"),
                ("INTEGRITY_CHECK", "PRAGMA integrity_check"),
            ):
                check_rows = conn.execute(pragma).fetchall()
                issues = [
                    str(row[0]) for row in check_rows
                    if row and str(row[0]).strip().lower() != "ok"
                ]
                if issues:
                    print(f"{check_name}=ISSUES COUNT={len(issues)}")
                    for issue in issues[:50]:
                        print(f"{check_name}_ISSUE={issue}")
                    if len(issues) > 50:
                        print(f"{check_name}_ISSUES_TRUNCATED={len(issues) - 50}")
                else:
                    print(f"{check_name}=ok")
            tables = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            ).fetchall()
            print(f"TABLE_COUNT={len(tables)}")
            for (table,) in tables:
                # Table names originate from SQLite metadata; quote identifiers safely.
                quoted = '"' + table.replace('"', '""') + '"'
                columns = [row[1] for row in conn.execute(f"PRAGMA table_info({quoted})")]
                try:
                    count = conn.execute(f"SELECT COUNT(*) FROM {quoted}").fetchone()[0]
                    print(f"TABLE={table} ROWS={count} COLUMNS={','.join(columns)}")
                except sqlite3.DatabaseError as exc:
                    print(f"TABLE={table} ROW_COUNT_ERROR={type(exc).__name__}")
        finally:
            conn.close()
    except (sqlite3.DatabaseError, OSError) as exc:
        print(f"STATUS=UNREADABLE ERROR={type(exc).__name__}: {exc}")
