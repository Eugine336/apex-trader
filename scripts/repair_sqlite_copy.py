#!/usr/bin/env python3
"""Create and optionally repair a candidate copy of a SQLite database.

This utility NEVER changes, deletes, renames, or replaces the source database.
Stop Apex and any other process using the database before running it. The
candidate is retained even when repair fails, for later inspection.

Examples:
    python scripts/repair_sqlite_copy.py data/apex_shadow.db
    python scripts/repair_sqlite_copy.py data/counterfactual.db --candidate data/counterfactual.repair-candidate.db

Only run against a local runtime database after the application is stopped.
The tool intentionally refuses non-empty WAL/SHM sidecars rather than making an
inconsistent byte-copy or attempting to checkpoint the source database.
"""
from __future__ import annotations

import argparse
import hashlib
import sqlite3
import sys
from pathlib import Path


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def integrity_errors(connection: sqlite3.Connection) -> list[str]:
    try:
        rows = connection.execute("PRAGMA integrity_check").fetchall()
    except sqlite3.DatabaseError as exc:
        return [f"integrity_check raised {type(exc).__name__}: {exc}"]
    errors = [str(row[0]) for row in rows if not row or str(row[0]).strip().lower() != "ok"]
    return errors or ([] if rows else ["integrity_check returned no rows"])


def repair_candidate(source: Path, candidate: Path | None = None) -> tuple[int, str]:
    """Copy source, run repair only against the copy, and report verification."""
    source = source.resolve()
    if not source.is_file():
        return 2, f"Source database does not exist or is not a file: {source}"
    for suffix in ("-wal", "-shm"):
        sidecar = Path(str(source) + suffix)
        if sidecar.exists() and sidecar.stat().st_size > 0:
            return 2, (
                f"Refusing repair: non-empty SQLite sidecar exists: {sidecar}. "
                "Stop all database users and create a consistent SQLite backup first."
            )
    if candidate is None:
        import datetime
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        candidate = source.with_name(f"{source.stem}.repair-candidate.{stamp}{source.suffix}")
    candidate = candidate.resolve()
    if candidate == source:
        return 2, "Candidate path must differ from source."
    if candidate.exists():
        return 2, f"Candidate already exists; refusing to overwrite: {candidate}"
    candidate.parent.mkdir(parents=True, exist_ok=True)
    source_hash_before = sha256(source)
    import shutil
    shutil.copy2(source, candidate)
    if sha256(source) != source_hash_before:
        return 3, "Source changed during copy; candidate retained but must not be trusted."

    connection = None
    try:
        connection = sqlite3.connect(str(candidate), timeout=5)
        before = integrity_errors(connection)
        if not before:
            connection.close()
            connection = None
            return 0, (
                f"Candidate copy is already integrity-clean.\n"
                f"Source SHA-256 unchanged: {source_hash_before}\n"
                f"Candidate: {candidate}\n"
                "Original source was not modified."
            )
        # Repair only the candidate. REINDEX repairs derived index structures;
        # VACUUM rewrites the candidate database. Neither touches source bytes.
        try:
            connection.execute("REINDEX")
            connection.commit()
            connection.execute("VACUUM")
            connection.commit()
        except sqlite3.DatabaseError as exc:
            after_failure = integrity_errors(connection)
            return 4, (
                f"Candidate repair failed: {type(exc).__name__}: {exc}\n"
                f"Candidate retained for inspection: {candidate}\n"
                f"Integrity findings: {'; '.join(after_failure[:20]) or 'not reported'}\n"
                f"Original source SHA-256 remains {source_hash_before}."
            )
        after = integrity_errors(connection)
        if after:
            return 4, (
                f"Candidate remains corrupt after REINDEX/VACUUM.\n"
                f"Candidate retained: {candidate}\n"
                f"Integrity findings: {'; '.join(after[:20])}\n"
                f"Original source SHA-256: {source_hash_before}"
            )
        connection.close()
        connection = None
        return 0, (
            "Candidate repair and integrity_check succeeded.\n"
            f"Source SHA-256 unchanged: {source_hash_before}\n"
            f"Candidate: {candidate}\n"
            "Original source was not modified or replaced. Review schema and row "
            "counts before deciding whether to use this candidate."
        )
    except (OSError, sqlite3.DatabaseError) as exc:
        return 4, (
            f"Candidate retained at {candidate}; operation failed: "
            f"{type(exc).__name__}: {exc}\nSource SHA-256: {source_hash_before}"
        )
    finally:
        if connection is not None:
            connection.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("--candidate", type=Path)
    args = parser.parse_args()
    code, message = repair_candidate(args.source, args.candidate)
    print(message)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
