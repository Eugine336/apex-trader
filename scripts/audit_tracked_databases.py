#!/usr/bin/env python3
"""Read-only inventory of tracked SQLite artifacts. Never emits row contents."""
from __future__ import annotations

import subprocess
import sqlite3
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[1]
tracked_paths = subprocess.run(
    ["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, check=True
).stdout.split(b"\\0")
artifact_suffixes = (
    ".db", ".db-wal", ".db-shm", ".db-journal",
    ".sqlite", ".sqlite3", ".sqlite-wal", ".sqlite-shm",
    ".sqlite-journal", "-wal", "-shm", "-journal",
)
artifacts = sorted(
    item.decode("utf-8", errors="strict")
    for item in tracked_paths
    if item and item.decode("utf-8", errors="strict").lower().endswith(artifact_suffixes)
)
sidecar_suffixes = (
    ".db-wal", ".db-shm", ".db-journal", ".sqlite-wal", ".sqlite-shm",
    ".sqlite-journal", "-wal", "-shm", "-journal",
)
tracked = [rel for rel in artifacts if not rel.lower().endswith(sidecar_suffixes)]
print(f"TRACKED_DATABASE_COUNT={len(tracked)}")
print(f"TRACKED_DATABASE_ARTIFACT_COUNT={len(artifacts)}")
for rel in artifacts:
    path = ROOT / rel
    if rel.lower().endswith(sidecar_suffixes):
        print(f"ARTIFACT={rel} KIND=SIDE_CAR SIZE_BYTES={path.stat().st_size if path.exists() else 'MISSING'}")
        continue
    print(f"\nDATABASE={rel} SIZE_BYTES={path.stat().st_size if path.exists() else 'MISSING'}")
    if not path.exists():
        print("STATUS=MISSING")
        continue
    # Read-only inspection: schemas and row counts only, never row payloads.
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
