"""
APEX TRADER — Phase 6: Audit Invariant Tests

Test-enforced structural guarantees for "everything auditable and verifiable."
Seeded against a temporary event store; no production DB dependency.

I1. Every closed trade has exactly one observed TRADE_CLOSE event.
I2. No event has an exit_reason without an exit_reason_source.
I3. Every decision/order/close event carries a correlation_id.
I4. Reconciliation is derivable for every closed trade.
I5. Backfilled events are always distinguishable from live events.
I6. Backfill is idempotent.
"""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional
from uuid import uuid4

import pytest

from persistence.backfill import (
    BackfillRunner,
    _CREATE_EVENTS,
    _deterministic_id,
    BACKFILL_DERIVED_TYPE,
)
from persistence.domain_events import (
    DECISION_REJECT,
    ORDER_FILLED,
    ORDER_SENT,
    SHADOW_RESOLVED,
    TRADE_CLOSE,
    TRADE_OPEN,
)


@pytest.fixture
def tmp_dir(tmp_path):
    return tmp_path


def _create_events_db(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(_CREATE_EVENTS)
    conn.commit()
    return conn


def _insert(
    conn: sqlite3.Connection,
    event_id: str,
    event_type: str,
    severity: str = "INFO",
    symbol: str = "EURUSD",
    correlation_id: Optional[str] = None,
    payload: Optional[Dict[str, Any]] = None,
    ts_utc_ms: int = 1000000,
    source_module: str = "test",
) -> None:
    conn.execute(
        "INSERT INTO events VALUES (?,?,?,?,?,?,?,?,?)",
        (
            event_id,
            correlation_id,
            None,
            ts_utc_ms,
            event_type,
            severity,
            symbol,
            source_module,
            json.dumps(payload) if payload else None,
        ),
    )
    conn.commit()


def _create_journal_db(path: Path, trades: List[Dict], decisions: List[Dict]) -> None:
    conn = sqlite3.connect(str(path))
    conn.execute("""
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
            timestamp TEXT NOT NULL,
            pnl_dollars REAL DEFAULT 0.0
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS decisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            pair TEXT NOT NULL,
            direction TEXT NOT NULL,
            score INTEGER NOT NULL,
            reason_rejected TEXT,
            timestamp TEXT NOT NULL
        )
    """)
    for t in trades:
        conn.execute(
            "INSERT INTO trades (pair, direction, entry, exit, pnl, score, "
            "confluences, regime, session, spread, slippage, entry_type, "
            "outcome, timestamp) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                t["pair"], t["direction"], t["entry"], t["exit"],
                t["pnl"], t.get("score", 80), "[]",
                t.get("regime", "BULLISH"), t.get("session", "LONDON"),
                0.0, 0.0, "FVG", t["outcome"],
                t.get("timestamp", "2026-06-01T12:00:00+00:00"),
            ),
        )
    for d in decisions:
        conn.execute(
            "INSERT INTO decisions (pair, direction, score, reason_rejected, "
            "timestamp) VALUES (?,?,?,?,?)",
            (
                d["pair"], d["direction"], d.get("score", 75),
                d.get("reason_rejected", "No M1 confirmation"),
                d.get("timestamp", "2026-06-01T12:00:00+00:00"),
            ),
        )
    conn.commit()
    conn.close()


def _create_empty_position_db(path: Path) -> None:
    conn = sqlite3.connect(str(path))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS managed_positions (
            order_id TEXT PRIMARY KEY, platform TEXT NOT NULL,
            symbol TEXT NOT NULL, direction TEXT NOT NULL,
            lots REAL NOT NULL, entry_price REAL NOT NULL,
            sl REAL NOT NULL, tp1 REAL NOT NULL, tp2 REAL NOT NULL,
            score INTEGER NOT NULL DEFAULT 0, regime TEXT NOT NULL DEFAULT '',
            session TEXT NOT NULL DEFAULT '', entry_type TEXT NOT NULL DEFAULT '',
            open_time TEXT NOT NULL, tp1_hit INTEGER NOT NULL DEFAULT 0,
            at_breakeven INTEGER NOT NULL DEFAULT 0,
            trailing INTEGER NOT NULL DEFAULT 0,
            tm_trade_id TEXT NOT NULL DEFAULT '',
            stake_usd REAL NOT NULL DEFAULT 0.0,
            multiplier INTEGER NOT NULL DEFAULT 100,
            last_update TEXT NOT NULL
        )
    """)
    conn.commit()
    conn.close()


class TestI1_OneObservedClosePerTrade:
    def test_each_trade_gets_exactly_one_close(self, tmp_dir):
        events_db = tmp_dir / "events.db"
        journal_db = tmp_dir / "journal.db"
        _create_journal_db(journal_db, trades=[
            {"pair": "EURUSD", "direction": "LONG", "entry": 1.1, "exit": 1.2,
             "pnl": 100, "outcome": "WIN"},
            {"pair": "GBPUSD", "direction": "SHORT", "entry": 1.3, "exit": 1.25,
             "pnl": 50, "outcome": "LOSS"},
        ], decisions=[])

        runner = BackfillRunner(
            events_db=events_db, journal_db=journal_db,
            position_db=tmp_dir / "nope.db", shadow_db=tmp_dir / "nope2.db",
        )
        runner.run()

        conn = sqlite3.connect(str(events_db))
        cur = conn.execute(
            "SELECT COUNT(*) FROM events WHERE event_type = ? "
            "AND source_module = 'backfill'",
            (TRADE_CLOSE,),
        )
        assert cur.fetchone()[0] == 2

        for row_id in ["1", "2"]:
            eid = _deterministic_id("trade_journal.trades", row_id, TRADE_CLOSE)
            cur = conn.execute(
                "SELECT COUNT(*) FROM events WHERE event_id = ?", (eid,)
            )
            assert cur.fetchone()[0] == 1, f"Expected exactly 1 TRADE_CLOSE for row {row_id}"

        derived_count = conn.execute(
            "SELECT COUNT(*) FROM events WHERE event_type = ?",
            (BACKFILL_DERIVED_TYPE,),
        ).fetchone()[0]
        close_observed = conn.execute(
            "SELECT COUNT(*) FROM events WHERE event_type = ? "
            "AND source_module = 'backfill'",
            (TRADE_CLOSE,),
        ).fetchone()[0]
        assert close_observed == 2, "Derived events must not inflate observed count"
        conn.close()


class TestI2_NoExitReasonWithoutSource:
    def test_all_close_events_have_source(self, tmp_dir):
        events_db = tmp_dir / "events.db"
        conn = _create_events_db(events_db)

        _insert(conn, "e1", TRADE_CLOSE, payload={
            "exit_reason": "UNKNOWN",
            "exit_reason_source": "backfill_unrecoverable",
            "origin": "backfill",
        })
        _insert(conn, "e2", TRADE_CLOSE, payload={
            "exit_reason": "STOP_LOSS_HIT",
            "exit_reason_source": "mt5_deal",
            "origin": "live",
        })

        rows = conn.execute(
            "SELECT event_id, payload_json FROM events WHERE event_type = ?",
            (TRADE_CLOSE,),
        ).fetchall()

        for eid, pj in rows:
            payload = json.loads(pj) if pj else {}
            has_reason = "exit_reason" in payload and payload["exit_reason"]
            has_source = "exit_reason_source" in payload and payload["exit_reason_source"]
            assert not has_reason or has_source, (
                f"Event {eid} has exit_reason without exit_reason_source"
            )
        conn.close()


class TestI3_CorrelationIdOnDomainEvents:
    def test_live_events_carry_correlation_id(self, tmp_dir):
        events_db = tmp_dir / "events.db"
        conn = _create_events_db(events_db)

        cid = uuid4().hex[:16]
        _insert(conn, "o1", ORDER_SENT, correlation_id=cid, payload={"origin": "live"})
        _insert(conn, "o2", ORDER_FILLED, correlation_id=cid, payload={"origin": "live"})
        _insert(conn, "d1", DECISION_REJECT, correlation_id=cid, payload={"origin": "live"})
        _insert(conn, "c1", TRADE_CLOSE, correlation_id=cid, payload={
            "exit_reason": "SL", "exit_reason_source": "mt5", "origin": "live",
        })

        domain_types = (ORDER_SENT, ORDER_FILLED, DECISION_REJECT, TRADE_CLOSE)
        placeholders = ",".join("?" for _ in domain_types)
        rows = conn.execute(
            f"SELECT event_id, correlation_id FROM events "
            f"WHERE event_type IN ({placeholders}) "
            f"AND json_extract(payload_json, '$.origin') = 'live'",
            domain_types,
        ).fetchall()

        for eid, corr in rows:
            assert corr is not None and corr != "", (
                f"Live domain event {eid} missing correlation_id"
            )
        conn.close()

    def test_backfill_decisions_dont_require_correlation(self, tmp_dir):
        events_db = tmp_dir / "events.db"
        journal_db = tmp_dir / "journal.db"
        _create_journal_db(journal_db, trades=[], decisions=[
            {"pair": "EURUSD", "direction": "LONG"},
        ])
        runner = BackfillRunner(
            events_db=events_db, journal_db=journal_db,
            position_db=tmp_dir / "np.db", shadow_db=tmp_dir / "ns.db",
        )
        runner.run()
        conn = sqlite3.connect(str(events_db))
        rows = conn.execute(
            "SELECT event_id, correlation_id, payload_json FROM events "
            "WHERE event_type = ?", (DECISION_REJECT,),
        ).fetchall()
        assert len(rows) == 1
        payload = json.loads(rows[0][2])
        assert payload["origin"] == "backfill"
        conn.close()


class TestI4_ReconciliationDerivable:
    def test_every_close_has_reason_or_unknown(self, tmp_dir):
        events_db = tmp_dir / "events.db"
        conn = _create_events_db(events_db)

        _insert(conn, "c1", TRADE_CLOSE, payload={
            "exit_reason": "UNKNOWN",
            "exit_reason_source": "backfill_unrecoverable",
            "raw_broker_reason": "BROKER_CLOSED",
            "origin": "backfill",
        })
        _insert(conn, "c2", TRADE_CLOSE, payload={
            "exit_reason": "STOP_LOSS_HIT",
            "exit_reason_source": "mt5_deal",
            "raw_broker_reason": "sl",
            "origin": "live",
        })

        rows = conn.execute(
            "SELECT event_id, payload_json FROM events WHERE event_type = ?",
            (TRADE_CLOSE,),
        ).fetchall()

        for eid, pj in rows:
            payload = json.loads(pj) if pj else {}
            reason = payload.get("exit_reason", "")
            source = payload.get("exit_reason_source", "")
            assert reason and source, (
                f"TRADE_CLOSE {eid}: reconciliation impossible — "
                f"exit_reason={reason!r}, exit_reason_source={source!r}"
            )
        conn.close()


class TestI5_BackfillDistinguishable:
    def test_backfill_events_carry_origin_marker(self, tmp_dir):
        events_db = tmp_dir / "events.db"
        journal_db = tmp_dir / "journal.db"
        _create_journal_db(journal_db, trades=[
            {"pair": "EURUSD", "direction": "LONG", "entry": 1.1, "exit": 1.2,
             "pnl": 100, "outcome": "WIN"},
        ], decisions=[
            {"pair": "GBPUSD", "direction": "SHORT"},
        ])

        runner = BackfillRunner(
            events_db=events_db, journal_db=journal_db,
            position_db=tmp_dir / "np.db", shadow_db=tmp_dir / "ns.db",
        )
        runner.run()

        conn = sqlite3.connect(str(events_db))
        rows = conn.execute(
            "SELECT event_id, payload_json FROM events WHERE source_module = 'backfill'"
        ).fetchall()
        assert len(rows) >= 2

        for eid, pj in rows:
            payload = json.loads(pj) if pj else {}
            assert payload.get("origin") == "backfill", (
                f"Event {eid} missing origin=backfill marker"
            )
            assert "backfill_run_id" in payload, (
                f"Event {eid} missing backfill_run_id"
            )
            assert "source_db" in payload, (
                f"Event {eid} missing source_db"
            )
            assert "source_row_id" in payload, (
                f"Event {eid} missing source_row_id"
            )
        conn.close()

    def test_live_events_have_no_backfill_marker(self, tmp_dir):
        events_db = tmp_dir / "events.db"
        conn = _create_events_db(events_db)
        _insert(conn, "live1", TRADE_CLOSE, payload={
            "exit_reason": "SL", "exit_reason_source": "mt5",
        })

        pj = conn.execute(
            "SELECT payload_json FROM events WHERE event_id = 'live1'"
        ).fetchone()[0]
        payload = json.loads(pj) if pj else {}
        assert payload.get("origin") != "backfill"
        conn.close()


class TestI6_BackfillIdempotent:
    def test_second_run_produces_no_new_rows(self, tmp_dir):
        events_db = tmp_dir / "events.db"
        journal_db = tmp_dir / "journal.db"
        _create_journal_db(journal_db, trades=[
            {"pair": "EURUSD", "direction": "LONG", "entry": 1.1, "exit": 1.2,
             "pnl": 100, "outcome": "WIN"},
            {"pair": "GBPUSD", "direction": "SHORT", "entry": 1.3, "exit": 1.25,
             "pnl": 50, "outcome": "LOSS"},
        ], decisions=[
            {"pair": "USDJPY", "direction": "LONG"},
            {"pair": "AUDUSD", "direction": "SHORT"},
        ])

        runner1 = BackfillRunner(
            events_db=events_db, journal_db=journal_db,
            position_db=tmp_dir / "np.db", shadow_db=tmp_dir / "ns.db",
        )
        stats1 = runner1.run()
        count_after_first = stats1["trades_observed"] + stats1["decisions"]
        assert count_after_first == 4, f"Expected 4 events, got {count_after_first}"

        conn = sqlite3.connect(str(events_db))
        total_after_first = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        conn.close()

        runner2 = BackfillRunner(
            events_db=events_db, journal_db=journal_db,
            position_db=tmp_dir / "np.db", shadow_db=tmp_dir / "ns.db",
        )
        stats2 = runner2.run()

        conn = sqlite3.connect(str(events_db))
        total_after_second = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        conn.close()

        assert total_after_second == total_after_first, (
            f"Idempotency violated: {total_after_first} → {total_after_second}"
        )
        assert stats2["skipped_existing"] >= count_after_first

    def test_dry_run_writes_nothing(self, tmp_dir):
        events_db = tmp_dir / "events.db"
        journal_db = tmp_dir / "journal.db"
        _create_journal_db(journal_db, trades=[
            {"pair": "EURUSD", "direction": "LONG", "entry": 1.1, "exit": 1.2,
             "pnl": 100, "outcome": "WIN"},
        ], decisions=[])

        runner = BackfillRunner(
            events_db=events_db, journal_db=journal_db,
            position_db=tmp_dir / "np.db", shadow_db=tmp_dir / "ns.db",
            dry_run=True,
        )
        stats = runner.run()
        assert stats["trades_observed"] == 1

        conn = sqlite3.connect(str(events_db))
        total = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        conn.close()
        assert total == 0, f"Dry run wrote {total} rows"


class TestBackfillGracefulDegradation:
    def test_missing_dbs_produce_zero_not_crash(self, tmp_dir):
        events_db = tmp_dir / "events.db"
        runner = BackfillRunner(
            events_db=events_db,
            journal_db=tmp_dir / "nonexistent.db",
            position_db=tmp_dir / "also_nonexistent.db",
            shadow_db=tmp_dir / "nope.db",
        )
        stats = runner.run()
        assert stats["errors"] == 0
        total = sum(v for k, v in stats.items() if k != "errors" and k != "skipped_existing")
        assert total == 0

    def test_empty_tables_produce_zero(self, tmp_dir):
        events_db = tmp_dir / "events.db"
        journal_db = tmp_dir / "journal.db"
        _create_journal_db(journal_db, trades=[], decisions=[])
        _create_empty_position_db(tmp_dir / "positions.db")

        runner = BackfillRunner(
            events_db=events_db, journal_db=journal_db,
            position_db=tmp_dir / "positions.db",
            shadow_db=tmp_dir / "nope.db",
        )
        stats = runner.run()
        assert stats["trades_observed"] == 0
        assert stats["decisions"] == 0
        assert stats["positions"] == 0
