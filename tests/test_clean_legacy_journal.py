"""Tests for scripts/clean_legacy_journal.py."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest


def _create_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            pair TEXT NOT NULL,
            direction TEXT NOT NULL,
            entry REAL NOT NULL,
            exit REAL NOT NULL,
            pnl REAL NOT NULL,
            score INTEGER NOT NULL,
            confluences TEXT NOT NULL,
            regime TEXT NOT NULL,
            session TEXT NOT NULL,
            spread REAL NOT NULL,
            slippage REAL NOT NULL,
            entry_type TEXT NOT NULL,
            time_to_tp1 REAL,
            time_to_exit REAL,
            outcome TEXT NOT NULL,
            timestamp TEXT NOT NULL
        )
        """
    )
    conn.commit()


def _insert_trade(
    conn: sqlite3.Connection,
    *,
    pair: str = "EURUSD",
    direction: str = "BUY",
    entry: float = 0.0,
    exit_: float = 0.0,
    pnl: float = 25.0,
    outcome: str = "WIN",
) -> int:
    cur = conn.execute(
        """
        INSERT INTO trades (
            pair, direction, entry, exit, pnl, score, confluences, regime,
            session, spread, slippage, entry_type, time_to_tp1, time_to_exit,
            outcome, timestamp
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            pair,
            direction,
            entry,
            exit_,
            pnl,
            85,
            json.dumps([]),
            "trending",
            "london",
            1.0,
            0.5,
            "zone",
            None,
            120.0,
            outcome,
            "2026-06-01T12:00:00+00:00",
        ),
    )
    conn.commit()
    return cur.lastrowid


def _count_by_outcome(conn: sqlite3.Connection, outcome: str) -> int:
    row = conn.execute("SELECT COUNT(*) FROM trades WHERE outcome = ?", (outcome,)).fetchone()
    return row[0]


def _total_rows(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0]


@pytest.fixture()
def legacy_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "trade_journal.db"
    conn = sqlite3.connect(str(db_path))
    _create_schema(conn)

    _insert_trade(conn, pair="EURUSD", entry=0.0, exit_=0.0, pnl=25.0, outcome="WIN")
    _insert_trade(conn, pair="UK100", entry=0.0, exit_=0.0, pnl=-10.0, outcome="LOSS")
    _insert_trade(conn, pair="BNBUSD", entry=0.0, exit_=0.0, pnl=333.0, outcome="WIN")
    _insert_trade(conn, pair="V50_1S", entry=0.0, exit_=0.0, pnl=0.0, outcome="BREAKEVEN")

    _insert_trade(conn, pair="GBPUSD", entry=1.27450, exit_=1.27600, pnl=15.0, outcome="WIN")
    _insert_trade(conn, pair="XAUUSD", entry=2380.50, exit_=2375.00, pnl=-5.5, outcome="LOSS")

    conn.close()
    return db_path


class TestDryRun:
    def test_identifies_legacy_rows(self, legacy_db: Path) -> None:
        from scripts.clean_legacy_journal import run

        result = run(legacy_db, apply=False, delete=False)
        assert result["matched"] == 4
        assert result["action"] == "dry_run"

        conn = sqlite3.connect(str(legacy_db))
        assert _total_rows(conn) == 6
        assert _count_by_outcome(conn, "LEGACY") == 0
        conn.close()


class TestApplyFlag:
    def test_flags_legacy_rows(self, legacy_db: Path) -> None:
        from scripts.clean_legacy_journal import run

        result = run(legacy_db, apply=True, delete=False)
        assert result["matched"] == 4
        assert result["action"] == "flagged"
        assert result["backup"] is not None

        conn = sqlite3.connect(str(legacy_db))
        assert _total_rows(conn) == 6
        assert _count_by_outcome(conn, "LEGACY") == 4
        assert _count_by_outcome(conn, "WIN") == 1
        assert _count_by_outcome(conn, "LOSS") == 1
        conn.close()

        assert Path(result["backup"]).exists()

    def test_good_rows_untouched(self, legacy_db: Path) -> None:
        from scripts.clean_legacy_journal import run

        run(legacy_db, apply=True, delete=False)

        conn = sqlite3.connect(str(legacy_db))
        conn.row_factory = sqlite3.Row
        good = conn.execute("SELECT * FROM trades WHERE outcome != 'LEGACY'").fetchall()

        assert len(good) == 2
        pairs = {r["pair"] for r in good}
        assert pairs == {"GBPUSD", "XAUUSD"}
        for r in good:
            assert r["entry"] != 0.0
            assert r["exit"] != 0.0
        conn.close()


class TestApplyDelete:
    def test_deletes_legacy_rows(self, legacy_db: Path) -> None:
        from scripts.clean_legacy_journal import run

        result = run(legacy_db, apply=True, delete=True)
        assert result["matched"] == 4
        assert result["action"] == "deleted"

        conn = sqlite3.connect(str(legacy_db))
        assert _total_rows(conn) == 2
        assert _count_by_outcome(conn, "LEGACY") == 0
        conn.close()


class TestIdempotency:
    def test_second_run_is_noop(self, legacy_db: Path) -> None:
        from scripts.clean_legacy_journal import run

        run(legacy_db, apply=True, delete=False)
        result2 = run(legacy_db, apply=True, delete=False)

        assert result2["matched"] == 0
        assert result2["action"] == "none"

        conn = sqlite3.connect(str(legacy_db))
        assert _total_rows(conn) == 6
        assert _count_by_outcome(conn, "LEGACY") == 4
        conn.close()


class TestBackup:
    def test_backup_created(self, legacy_db: Path) -> None:
        from scripts.clean_legacy_journal import run

        result = run(legacy_db, apply=True, delete=False)
        backup_path = Path(result["backup"])
        assert backup_path.exists()
        assert backup_path.stat().st_size == legacy_db.stat().st_size

    def test_no_backup_on_dry_run(self, legacy_db: Path) -> None:
        from scripts.clean_legacy_journal import run

        result = run(legacy_db, apply=False)
        assert result["backup"] is None


class TestEdgeCases:
    def test_no_legacy_rows(self, tmp_path: Path) -> None:
        from scripts.clean_legacy_journal import run

        db_path = tmp_path / "clean.db"
        conn = sqlite3.connect(str(db_path))
        _create_schema(conn)
        _insert_trade(conn, pair="GBPUSD", entry=1.275, exit_=1.276, pnl=10.0, outcome="WIN")
        conn.close()

        result = run(db_path, apply=True)
        assert result["matched"] == 0
        assert result["action"] == "none"

    def test_missing_db(self, tmp_path: Path) -> None:
        from scripts.clean_legacy_journal import run

        result = run(tmp_path / "nonexistent.db")
        assert result["matched"] == 0
        assert result["action"] == "none"

    def test_already_legacy_not_matched(self, legacy_db: Path) -> None:
        """Rows already flagged LEGACY should not be re-matched."""
        from scripts.clean_legacy_journal import run

        run(legacy_db, apply=True, delete=False)

        conn = sqlite3.connect(str(legacy_db))
        assert _count_by_outcome(conn, "LEGACY") == 4
        conn.close()

        result = run(legacy_db, apply=True, delete=False)
        assert result["matched"] == 0
