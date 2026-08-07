"""
Phase 5 — Dashboard surface tests.
Verifies:
1. /api/events returns persisted events with severity filter + DEBUG toggle
2. /api/history rows carry exit attribution fields
3. /api/shadow returns outcomes grouped by gate
4. /api/reconciliation flags seeded discrepancies
5. /api/activity reads from event store (not just in-memory)
6. No silent except: pass in new code
"""

import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from persistence.event_store import EventStore
from persistence.shadow_store import ShadowStore, ShadowContract, ShadowResolution


# ── Fixtures ────────────────────────────────────────────────────────────────

@pytest.fixture
def tmp_events_db(tmp_path):
    db = tmp_path / "test_events.db"
    store = EventStore(db_path=str(db))
    yield store
    store.close()


@pytest.fixture
def tmp_shadow_db(tmp_path):
    db = tmp_path / "test_shadow.db"
    store = ShadowStore(db_path=db)
    yield store
    store.close()


def _seed_events(store: EventStore):
    """Insert a mix of events at different severity + types."""
    store.emit("LOG", "DEBUG", symbol="BTCUSD", payload={"message": "debug msg"})
    store.emit("LOG", "INFO", symbol="EURUSD", payload={"message": "info msg"})
    store.emit("LOG", "WARNING", symbol="GBPUSD", payload={"message": "warn msg"})
    store.emit("LOG", "ERROR", symbol="XAUUSD", payload={"message": "error msg"})
    store.emit(
        "TRADE_CLOSE", "INFO", symbol="EURUSD",
        correlation_id="cycle-abc123",
        payload={
            "order_id": "12345",
            "exit_reason": "STOP_LOSS_HIT",
            "exit_reason_source": "mt5_deal",
            "raw_broker_reason": "sl",
            "raw_broker_comment": "sl",
            "pnl_dollars": -50.0,
        },
    )
    store.emit(
        "TRADE_CLOSE", "INFO", symbol="GBPUSD",
        correlation_id="cycle-def456",
        payload={
            "order_id": "67890",
            "exit_reason": "TAKE_PROFIT_HIT",
            "exit_reason_source": "mt5_deal",
            "raw_broker_reason": "BROKER_CLOSED",
            "raw_broker_comment": "",
            "pnl_dollars": 120.0,
        },
    )
    store.emit("DECISION_REJECT", "INFO", symbol="USDJPY", payload={"reason": "M1 no confirm"})
    store.emit("SETUP_SKIPPED", "INFO", symbol="NZDUSD", payload={"reason": "score too low"})
    store.flush(timeout=3.0)


def _seed_shadow(store: ShadowStore):
    """Insert shadow contracts with different outcomes and gates."""
    for i, (gate, outcome, r) in enumerate([
        ("M1_confirmation", "WIN", 2.1),
        ("M1_confirmation", "LOSS", -1.0),
        ("M1_confirmation", "EXPIRED", 0.0),
        ("spread_check", "WIN", 1.5),
        ("spread_check", "WIN", 3.0),
        ("ev_check", "LOSS", -1.0),
    ]):
        cid = f"sc_test{i:04d}"
        c = ShadowContract(
            contract_id=cid,
            symbol="EURUSD",
            direction="BUY",
            entry_price=1.1000,
            stop_loss=1.0950,
            tp1=1.1100,
            tp2=1.1200,
            pip_size=0.0001,
            rejecting_gate=gate,
            ts_utc_ms=int(time.time() * 1000) - i * 60000,
        )
        store.insert_contract(c)
        if outcome != "EXPIRED":
            store.resolve_contract(
                cid,
                ShadowResolution(
                    outcome=outcome,
                    r_multiple=r,
                    exit_reason="TP1" if outcome == "WIN" else "SL",
                    exit_price=1.1100 if outcome == "WIN" else 1.0950,
                    resolution_ts=int(time.time() * 1000),
                    resolution_granularity="M1",
                    bars_replayed=50,
                ),
            )
        else:
            store.mark_expired(cid, bars_replayed=100)


# ── EventStore read API tests ───────────────────────────────────────────────

class TestEventStoreQueryEvents:
    def test_severity_filter_info_excludes_debug(self, tmp_events_db):
        _seed_events(tmp_events_db)
        rows = tmp_events_db.query_events(severity_min="INFO", limit=100)
        severities = {r["severity"] for r in rows}
        assert "DEBUG" not in severities
        assert "INFO" in severities
        assert "WARNING" in severities
        assert "ERROR" in severities

    def test_severity_filter_debug_includes_all(self, tmp_events_db):
        _seed_events(tmp_events_db)
        rows = tmp_events_db.query_events(severity_min="DEBUG", limit=100)
        severities = {r["severity"] for r in rows}
        assert "DEBUG" in severities

    def test_event_type_filter(self, tmp_events_db):
        _seed_events(tmp_events_db)
        rows = tmp_events_db.query_events(
            severity_min="DEBUG",
            event_types=["TRADE_CLOSE"],
            limit=100,
        )
        assert all(r["event_type"] == "TRADE_CLOSE" for r in rows)
        assert len(rows) == 2

    def test_trade_close_map(self, tmp_events_db):
        _seed_events(tmp_events_db)
        m = tmp_events_db.get_trade_close_map()
        assert "12345" in m
        assert m["12345"]["exit_reason"] == "STOP_LOSS_HIT"
        assert "67890" in m
        assert m["67890"]["exit_reason"] == "TAKE_PROFIT_HIT"

    def test_reconciliation_flags_mismatch(self, tmp_events_db):
        _seed_events(tmp_events_db)
        anomalies = tmp_events_db.get_reconciliation()
        mismatched = [a for a in anomalies if a.get("_payload", {}).get("order_id") == "67890"]
        assert len(mismatched) == 1
        assert mismatched[0]["_payload"]["exit_reason"] == "TAKE_PROFIT_HIT"
        assert mismatched[0]["_payload"]["raw_broker_reason"] == "BROKER_CLOSED"

    def test_pagination(self, tmp_events_db):
        _seed_events(tmp_events_db)
        page1 = tmp_events_db.query_events(severity_min="DEBUG", limit=3, offset=0)
        page2 = tmp_events_db.query_events(severity_min="DEBUG", limit=3, offset=3)
        ids1 = {r["event_id"] for r in page1}
        ids2 = {r["event_id"] for r in page2}
        assert ids1.isdisjoint(ids2)


# ── ShadowStore read API tests ─────────────────────────────────────────────

class TestShadowStoreQueryOutcomes:
    def test_outcomes_by_gate(self, tmp_shadow_db):
        _seed_shadow(tmp_shadow_db)
        rows = tmp_shadow_db.get_outcomes_by_gate()
        gate_map = {}
        for r in rows:
            gate = r["rejecting_gate"]
            if gate not in gate_map:
                gate_map[gate] = {}
            gate_map[gate][r["outcome"]] = r["cnt"]
        assert gate_map["M1_confirmation"]["WIN"] == 1
        assert gate_map["M1_confirmation"]["LOSS"] == 1
        assert gate_map["M1_confirmation"]["EXPIRED"] == 1
        assert gate_map["spread_check"]["WIN"] == 2

    def test_get_all_contracts(self, tmp_shadow_db):
        _seed_shadow(tmp_shadow_db)
        all_c = tmp_shadow_db.get_all_contracts(limit=50)
        assert len(all_c) == 6
        resolved = tmp_shadow_db.get_all_contracts(status="RESOLVED")
        assert all(c.status == "RESOLVED" for c in resolved)

    def test_get_all_by_gate(self, tmp_shadow_db):
        _seed_shadow(tmp_shadow_db)
        m1 = tmp_shadow_db.get_all_contracts(rejecting_gate="M1_confirmation")
        assert all(c.rejecting_gate == "M1_confirmation" for c in m1)
        assert len(m1) == 3


# ── Dashboard mixin tests ──────────────────────────────────────────────────

class TestEventsMixin:
    def test_get_events_returns_formatted(self, tmp_events_db):
        _seed_events(tmp_events_db)
        from dashboard.state_events import EventsMixin

        class FakeState(EventsMixin):
            def _get_event_store(self_inner):
                return tmp_events_db

        state = FakeState()
        result = state.get_events(severity_min="INFO", limit=50)
        assert result["source"] == "event_store"
        events = result["events"]
        assert len(events) > 0
        for e in events:
            assert "event_id" in e
            assert "timestamp" in e
            assert "level" in e
            assert "severity" in e
            assert e["severity"] != "DEBUG"

    def test_get_events_debug_toggle(self, tmp_events_db):
        _seed_events(tmp_events_db)
        from dashboard.state_events import EventsMixin

        class FakeState(EventsMixin):
            def _get_event_store(self_inner):
                return tmp_events_db

        state = FakeState()
        with_debug = state.get_events(severity_min="DEBUG", limit=100)
        without_debug = state.get_events(severity_min="INFO", limit=100)
        assert len(with_debug["events"]) > len(without_debug["events"])

    def test_get_reconciliation_formatted(self, tmp_events_db):
        _seed_events(tmp_events_db)
        from dashboard.state_events import EventsMixin

        class FakeState(EventsMixin):
            def _get_event_store(self_inner):
                return tmp_events_db

        state = FakeState()
        result = state.get_reconciliation()
        assert "anomalies" in result
        assert any(a["raw_broker_reason"] == "BROKER_CLOSED" for a in result["anomalies"])


class TestShadowMixin:
    def test_get_shadow_outcomes_grouped(self, tmp_shadow_db):
        _seed_shadow(tmp_shadow_db)
        from dashboard.state_shadow import ShadowMixin

        class FakeState(ShadowMixin):
            def _get_shadow_store(self_inner):
                return tmp_shadow_db

        state = FakeState()
        result = state.get_shadow_outcomes()
        assert "gates" in result
        assert "summary" in result
        assert "contracts" in result
        gates = result["gates"]
        assert len(gates) > 0
        m1 = [g for g in gates if g["gate"] == "M1_confirmation"]
        assert len(m1) == 1
        assert m1[0]["WIN"] == 1
        assert m1[0]["LOSS"] == 1


# ── History exit attribution test ───────────────────────────────────────────

class TestHistoryExitAttribution:
    def test_exit_attr_helper(self):
        from dashboard.state_helpers import _exit_attr

        close_map = {
            "12345": {
                "exit_reason": "STOP_LOSS_HIT",
                "exit_reason_source": "mt5_deal",
                "raw_broker_reason": "sl",
            }
        }
        record = {"id": "12345"}
        assert _exit_attr(close_map, record, "exit_reason") == "STOP_LOSS_HIT"
        assert _exit_attr(close_map, record, "exit_reason_source") == "mt5_deal"

        record_missing = {"id": "99999"}
        assert _exit_attr(close_map, record_missing, "exit_reason") == ""


# ── Activity fallback test ──────────────────────────────────────────────────

class TestActivityFallback:
    def test_activity_prefers_event_store(self, tmp_events_db):
        _seed_events(tmp_events_db)
        from dashboard.state import LiveState

        state = LiveState()
        with patch("dashboard.state_events.EventsMixin._get_event_store", return_value=tmp_events_db):
            result = state.get_activity()
            assert result.get("source") == "event_store"
            assert len(result["events"]) > 0

    def test_activity_falls_back_to_in_memory(self):
        from dashboard.state import LiveState

        state = LiveState()
        with patch("dashboard.state_events.EventsMixin._get_event_store", return_value=None):
            result = state.get_activity()
            assert result.get("source") == "in_memory"


# ── Silent-swallow check on new files ───────────────────────────────────────

class TestNoSilentSwallow:
    """Verify no bare except:pass in Phase 5 files."""

    def _check_file(self, filepath):
        with open(filepath) as f:
            lines = f.readlines()
        violations = []
        for i, line in enumerate(lines):
            stripped = line.strip()
            if stripped == "pass" and i > 0:
                prev = lines[i - 1].strip()
                if prev.startswith("except") and ":" in prev:
                    violations.append((i + 1, prev, stripped))
        return violations

    def test_state_events_no_silent_pass(self):
        path = Path(__file__).parent.parent / "state_events.py"
        assert self._check_file(path) == [], f"Silent except:pass in {path}"

    def test_state_shadow_no_silent_pass(self):
        path = Path(__file__).parent.parent / "state_shadow.py"
        assert self._check_file(path) == [], f"Silent except:pass in {path}"
