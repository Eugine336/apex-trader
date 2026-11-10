"""Regression tests for the management/execution optimistic-rollback pipeline.

Covers two bugs found in the full management/execution audit:

1. CRITICAL — ``PositionEvaluator._record_inflight_manage`` was missing (it
   lived only on ``EventDrivenSystem``), so the worker-path optimistic SL write
   raised ``AttributeError`` (swallowed by the broad per-position try/except)
   BEFORE the line that arms the ``sl_modify_pending_until`` phantom-stop guard.
   The guard was therefore never armed and a broker-rejected SL could fire a
   phantom stop-hit CLOSE.

2. HIGH — ``EventDrivenSystem._handle_manage_result`` cleared the pending guard
   BEFORE rolling back the optimistic SL, opening a race window where a
   concurrent snapshot paired the cleared guard with the still-rejected SL.
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timezone
from types import SimpleNamespace

from brain.world_model import WorldModelStore
from execution.intent_aggregator import IntentAggregator
from execution.intents import Intent, IntentType
from execution.management_state import ManagementStateStore
from tick import TickStore, Tick


def _tick(symbol: str = "EURUSD", bid: float = 1.1000, ask: float = 1.1002) -> Tick:
    return Tick(
        symbol=symbol, bid=bid, ask=ask,
        timestamp=datetime.now(timezone.utc), source="test",
    )


def _position(order_id: str = "T1", direction: str = "BUY", sl: float = 1.0950):
    return SimpleNamespace(
        order_id=order_id, platform="mt5", symbol="EURUSD",
        direction=direction, entry_price=1.1000, open_price=1.1000, lots=0.1,
        remaining_lots=0.1, open_time=datetime.now(timezone.utc),
        score=85, sl=sl, tp1=1.1050, tp2=1.1100,
        at_breakeven=False, trailing=False, tp1_hit=False,
        broker_pnl=0.0, broker_lots=0.1,
        confluences=[], scale_in_count=0, stake_usd=0.0, multiplier=100,
    )


def _pm(positions=None):
    pm = SimpleNamespace()
    pm.get_all_open_positions = lambda: positions or []
    pm.fetch_market_data = lambda *a, **k: {}
    pm.get_symbol_spec = lambda s: {"pip_size": 0.0001}
    return pm


def _evaluator(positions=None):
    from event_driven_bootstrap import PositionEvaluator

    return PositionEvaluator(
        _pm(positions), TickStore(), WorldModelStore(), IntentAggregator(),
    )


# ── Bug 1: the missing _record_inflight_manage / unarmed guard ───────


class TestInflightManageWiring:
    def test_evaluator_exposes_record_inflight_manage(self):
        ev = _evaluator()
        assert hasattr(ev, "_record_inflight_manage")
        ev._record_inflight_manage("T1", IntentType.MODIFY_SL, {"stop_loss": 1.0})
        assert ("T1", int(IntentType.MODIFY_SL)) in ev._inflight_manage

    def test_worker_sl_move_arms_pending_guard(self):
        """A worker MODIFY_SL must arm the pending-confirmation guard.

        Before the fix the ``_record_inflight_manage`` call (one line above the
        guard assignment) raised ``AttributeError``, so the guard was never set
        and the optimistic SL was exposed to a phantom stop-hit CLOSE.
        """
        pos = _position(sl=1.0950, direction="BUY")
        store = TickStore()
        store.put(_tick("EURUSD", bid=1.1020, ask=1.1022))
        ev = _evaluator([pos])
        ev._tick_store = store
        ev._worker.evaluate = lambda *a, **k: [Intent.modify_sl(
            symbol="EURUSD", ticket="T1", new_sl=1.1000,
            source="breakeven", reason="test",
        )]

        ev._evaluate_position(pos, 1.1021, datetime.now(timezone.utc), time.monotonic())

        mgmt = ev._mgmt_store.get("T1")
        assert mgmt is not None
        assert mgmt.stop_loss == 1.1000                  # optimistic write happened
        assert mgmt.sl_modify_pending_until > 0.0        # guard ARMED (was the bug)
        assert ("T1", int(IntentType.MODIFY_SL)) in ev._inflight_manage


# ── Bug 2: rollback ordering in _handle_manage_result ────────────────


def _fake_system(store: ManagementStateStore, inflight: dict):
    return SimpleNamespace(
        _mgmt_store=store,
        _inflight_manage=inflight,
        _inflight_manage_lock=threading.Lock(),
    )


class TestHandleManageResultRollback:
    def test_successful_partial_close_finishes_pending_state(self):
        from event_driven_bootstrap import EventDrivenSystem

        store = ManagementStateStore()
        mgmt = store.get_or_create(
            "T1", remaining_size_lots=0.2, partial_closed=True,
            tp1_hit=True, status="PARTIAL_CLOSE_PENDING",
        )
        fake = _fake_system(
            store,
            {("T1", int(IntentType.PARTIAL_CLOSE)): {
                "partial_closed": False, "tp1_hit": False, "status": "OPEN",
            }},
        )
        intent = Intent.partial_close(
            symbol="EURUSD", ticket="T1", fraction=0.5,
            source="tp1", reason="test",
        )
        EventDrivenSystem._handle_manage_result(
            fake, intent, SimpleNamespace(success=True, error=None),
        )
        assert mgmt.partial_closed is True
        assert mgmt.status == "OPEN"
        store.close()

    def test_restart_promotes_unacknowledged_partial_to_reconciliation(self):
        from event_driven_bootstrap import EventDrivenSystem

        store = ManagementStateStore()
        store.get_or_create(
            "T1", remaining_size_lots=0.2, partial_closed=True,
            tp1_hit=True, status="PARTIAL_CLOSE_PENDING",
        )
        fake = SimpleNamespace(
            _mgmt_store=store,
            _pm=SimpleNamespace(get_all_open_positions=lambda: [_position()]),
        )
        EventDrivenSystem._recover_open_positions(fake)
        recovered = store.get("T1")
        assert recovered.status == "RECONCILIATION_REQUIRED"
        assert recovered.partial_closed is True
        assert recovered.tp1_hit is True
        store.close()

    def test_ambiguous_partial_close_is_durable_and_not_rolled_back(self, tmp_path):
        from event_driven_bootstrap import EventDrivenSystem

        db_path = str(tmp_path / "management.sqlite")
        store = ManagementStateStore(db_path)
        mgmt = store.get_or_create(
            "T1", remaining_size_lots=0.2, partial_closed=False, status="OPEN",
        )
        mgmt.partial_closed = True
        mgmt.tp1_hit = True
        inflight = {
            ("T1", int(IntentType.PARTIAL_CLOSE)): {
                "partial_closed": False, "tp1_hit": False,
            },
        }
        fake = _fake_system(store, inflight)
        intent = Intent.partial_close(
            symbol="EURUSD", ticket="T1", fraction=0.5,
            source="tp1", reason="test",
        )
        EventDrivenSystem._handle_manage_result(
            fake, intent, SimpleNamespace(success=False, error="AMBIGUOUS_ACK_LOST"),
        )

        current = store.get("T1")
        assert current.partial_closed is True
        assert current.tp1_hit is True
        assert current.status == "RECONCILIATION_REQUIRED"
        assert ("T1", int(IntentType.PARTIAL_CLOSE)) not in inflight
        store.close()

        recovered = ManagementStateStore(db_path)
        restored = recovered.get("T1")
        assert restored is not None
        assert restored.partial_closed is True
        assert restored.tp1_hit is True
        assert restored.status == "RECONCILIATION_REQUIRED"
        assert recovered.reconcile_partial_close("T1", 0.2) is False
        assert recovered.get("T1").status == "RECONCILIATION_REQUIRED"
        assert recovered.reconcile_partial_close("T1", 0.1) is True
        assert recovered.get("T1").status == "OPEN"
        assert recovered.get("T1").remaining_size_lots == 0.1
        recovered.close()

    def test_failed_sl_rolls_back_and_clears_guard(self):
        from event_driven_bootstrap import EventDrivenSystem

        store = ManagementStateStore()
        mgmt = store.get_or_create("T1", original_stop_loss=1.0950, stop_loss=1.0950)
        # Simulate the optimistic write + armed guard + recorded rollback.
        mgmt.stop_loss = 1.1000
        mgmt.at_breakeven = True
        mgmt.sl_modify_pending_until = time.monotonic() + 5.0
        mgmt.sl_pending_confirmation = True
        inflight = {
            ("T1", int(IntentType.MODIFY_SL)): {"stop_loss": 1.0950, "at_breakeven": False},
        }
        fake = _fake_system(store, inflight)

        intent = Intent.modify_sl(
            symbol="EURUSD", ticket="T1", new_sl=1.1000, source="x", reason="x",
        )
        result = SimpleNamespace(success=False, error="Invalid stops")
        EventDrivenSystem._handle_manage_result(fake, intent, result)

        m = store.get("T1")
        assert m.stop_loss == 1.0950             # SL rolled back to confirmed level
        assert m.at_breakeven is False           # breakeven flag rolled back
        assert m.sl_modify_pending_until == 0.0  # guard cleared (after rollback)
        assert m.sl_pending_confirmation is False
        assert ("T1", int(IntentType.MODIFY_SL)) not in inflight

    def test_successful_sl_keeps_level_and_clears_guard(self):
        from event_driven_bootstrap import EventDrivenSystem

        store = ManagementStateStore()
        mgmt = store.get_or_create("T1", original_stop_loss=1.0950, stop_loss=1.0950)
        mgmt.stop_loss = 1.1000
        mgmt.sl_modify_pending_until = time.monotonic() + 5.0
        mgmt.sl_pending_confirmation = True
        inflight = {
            ("T1", int(IntentType.MODIFY_SL)): {"stop_loss": 1.0950, "at_breakeven": False},
        }
        fake = _fake_system(store, inflight)

        intent = Intent.modify_sl(
            symbol="EURUSD", ticket="T1", new_sl=1.1000, source="x", reason="x",
        )
        result = SimpleNamespace(success=True, error=None)
        EventDrivenSystem._handle_manage_result(fake, intent, result)

        m = store.get("T1")
        assert m.stop_loss == 1.1000             # confirmed level stands
        assert m.sl_modify_pending_until == 0.0  # guard cleared
        assert m.sl_pending_confirmation is False
