from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

from scripts.repair_sqlite_copy import repair_candidate


def _hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _make_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.execute("CREATE TABLE outcomes (id INTEGER PRIMARY KEY, symbol TEXT NOT NULL)")
        conn.execute("CREATE INDEX idx_outcomes_symbol ON outcomes(symbol)")
        conn.executemany(
            "INSERT INTO outcomes(symbol) VALUES (?)",
            [("XAUUSD",), ("EURUSD",), ("XAUUSD",)],
        )
        conn.commit()
    finally:
        conn.close()


def test_repair_candidate_never_modifies_source(tmp_path: Path) -> None:
    source = tmp_path / "shadow.db"
    candidate = tmp_path / "shadow.candidate.db"
    _make_db(source)
    original_hash = _hash(source)

    code, message = repair_candidate(source, candidate)

    assert code == 0
    assert "Original source was not modified" in message
    assert candidate.is_file()
    assert _hash(source) == original_hash
    conn = sqlite3.connect(candidate)
    try:
        assert conn.execute("SELECT COUNT(*) FROM outcomes").fetchone()[0] == 3
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        conn.close()


def test_repair_candidate_refuses_existing_candidate(tmp_path: Path) -> None:
    source = tmp_path / "shadow.db"
    candidate = tmp_path / "candidate.db"
    _make_db(source)
    candidate.write_bytes(b"do not overwrite")
    original_hash = _hash(source)

    code, message = repair_candidate(source, candidate)

    assert code == 2
    assert "refusing to overwrite" in message
    assert candidate.read_bytes() == b"do not overwrite"
    assert _hash(source) == original_hash


def test_repair_candidate_refuses_nonempty_wal_sidecar(tmp_path: Path) -> None:
    source = tmp_path / "shadow.db"
    _make_db(source)
    wal = Path(str(source) + "-wal")
    wal.write_bytes(b"active wal")
    original_hash = _hash(source)

    code, message = repair_candidate(source, tmp_path / "candidate.db")

    assert code == 2
    assert "non-empty SQLite sidecar" in message
    assert not (tmp_path / "candidate.db").exists()
    assert _hash(source) == original_hash
