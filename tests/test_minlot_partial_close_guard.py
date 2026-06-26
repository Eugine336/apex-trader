"""Tests for the micro-lot partial-close guard.

A position already at the broker minimum lot (typically 0.01) cannot be split:
``0.01 × 0.5 = 0.005`` rounds to ``0.0`` at the volume step. Before the guard
this produced an infinite retry loop — the executor returned a hard failure,
``_handle_manage_result`` rolled back the optimistic ``partial_closed`` /
``tp1_hit`` flags, and the doomed ``PARTIAL_CLOSE`` re-fired every eval cycle.

The fix has two layers, both validated here:

1. **Executor fallback** (``action_executor.py``): when the computed close
   volume is zero, return ``success=True`` with a ``min_lot_skip`` marker. A
   success result is NOT rolled back by ``_handle_manage_result`` (which only
   rolls back on failure), so the flag sticks and the loop ends. The position
   then relies on SL-based protection (breakeven / trailing).

2. **Worker guard** (``position_worker.py`` ``_check_tp1``): when the position
   is at the broker minimum lot, emit a ``MODIFY_SL`` to breakeven instead of
   the impossible ``PARTIAL_CLOSE`` — so the doomed intent is never created.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

from execution.action_executor import ActionExecutor, ExecutorConfig
from execution.intents import Intent, IntentType
from execution.position_snapshot import PositionSnapshot
from execution.position_worker import PositionWorker, WorkerConfig
from execution.risk_gate import GateConfig

# ── Fake broker ──────────────────────────────────────────────────────


@dataclass
class _FakeCloseResult:
    success: bool = True
    error: str = ""
    pnl: float = 0.0


class _FakeBroker:
    def __init__(self):
        self.close_calls: list[dict] = []
        self.modify_calls: list[dict] = []

    def modify_trade(self, order_id, platform, new_sl=None, new_tp=None) -> bool:
        self.modify_calls.append({"order_id": order_id, "new_sl": new_sl})
        return True

    def close_trade(self, order_id, platform, lots=None) -> _FakeCloseResult:
        self.close_calls.append({"order_id": order_id, "lots": lots})
        return _FakeCloseResult()

    def execute_entry(self, *a, **k) -> Any:  # pragma: no cover - unused
        return _FakeCloseResult()


def _fast_cfg() -> ExecutorConfig:
    return ExecutorConfig(
        max_retries=1,
        retry_base_delay_s=0.001,
        circuit_failure_threshold=10,
        circuit_cooldown_s=0.1,
        gate_config=GateConfig(max_calls_per_second=1000),
    )


def _positions(remaining_lots: float, volume_step: float = 0.01) -> dict[str, dict]:
    return {
        "T1": {
            "symbol": "GBPUSD",
            "direction": "SELL",
            "sl": 1.32300,
            "platform": "mt5",
            "lots": remaining_lots,
            "remaining_lots": remaining_lots,
            "volume_step": volume_step,
        },
    }


def _partial_intent(fraction: float = 0.5) -> Intent:
    return Intent.partial_close(
        symbol="GBPUSD",
        ticket="T1",
        fraction=fraction,
        source="test",
        reason="test",
    )


# ── Worker snapshot helper ───────────────────────────────────────────

NOW = datetime(2025, 6, 15, 14, 0, tzinfo=timezone.utc)


def _snap(
    *,
    remaining_lots: float,
    volume_min: float = 0.01,
    platform: str = "mt5",
    at_breakeven: bool = False,
    **kw,
) -> PositionSnapshot:
    # LONG hitting TP1: entry 1.10, tp1 1.105, price past it.
    return PositionSnapshot(
        order_id="T1",
        platform=platform,
        symbol="EURUSD",
        direction="BUY",
        entry_price=1.10000,
        lots=remaining_lots,
        remaining_lots=remaining_lots,
        open_time=NOW,
        score=85,
        sl=1.09900,
        sl_original=1.09900,
        tp1=1.10500,
        tp2=1.12000,
        tp2_original=1.12000,
        at_breakeven=at_breakeven,
        partial_closed=False,
        current_price=1.10600,
        pnl_pips=60.0,
        pip_size=0.0001,
        pip_value_per_lot=10.0,
        volume_min=volume_min,
        broker_lots=remaining_lots,
        **kw,
    )


# ── Executor-level fallback ──────────────────────────────────────────


class TestExecutorMinLotFallback:
    def test_minlot_partial_returns_success_skip(self):
        broker = _FakeBroker()
        executor = ActionExecutor(broker, _fast_cfg())
        result = executor.execute(_partial_intent(), _positions(0.01))
        assert result.success is True
        assert "min_lot_skip" in (result.error or "")
        # The impossible split is never sent to the broker.
        assert broker.close_calls == []

    def test_zero_remaining_returns_success_skip(self):
        broker = _FakeBroker()
        executor = ActionExecutor(broker, _fast_cfg())
        result = executor.execute(_partial_intent(), _positions(0.0))
        assert result.success is True
        assert "min_lot_skip" in (result.error or "")
        assert broker.close_calls == []

    def test_success_result_does_not_trigger_rollback(self):
        """A success result must NOT roll back the optimistic flags.

        ``_handle_manage_result`` only rolls back when ``result.success`` is
        False. By returning success, the min-lot skip keeps ``partial_closed``
        set so the doomed PARTIAL_CLOSE never re-fires — proven here by the
        contract that the executor reports success.
        """
        broker = _FakeBroker()
        executor = ActionExecutor(broker, _fast_cfg())
        result = executor.execute(_partial_intent(), _positions(0.01))
        # The rollback guard keys off `not result.success`; success here means
        # no rollback, so the loop ends.
        assert result.success is True

    def test_normal_lot_still_partial_closes(self):
        """Regression: a normal-sized position is still split and executed."""
        broker = _FakeBroker()
        executor = ActionExecutor(broker, _fast_cfg())
        result = executor.execute(_partial_intent(), _positions(0.10))
        assert result.success is True
        assert "min_lot_skip" not in (result.error or "")
        assert broker.close_calls[0]["lots"] == 0.05

    def test_higher_minimum_still_partial_closes_when_splittable(self):
        """A 0.2-lot position with 0.1 step splits to 0.1 — executes normally."""
        broker = _FakeBroker()
        executor = ActionExecutor(broker, _fast_cfg())
        result = executor.execute(
            _partial_intent(),
            _positions(0.20, volume_step=0.10),
        )
        assert result.success is True
        assert broker.close_calls[0]["lots"] == 0.10


# ── Worker-level guard (_check_tp1) ──────────────────────────────────


class TestWorkerTp1MinLotGuard:
    def test_minlot_emits_breakeven_not_partial(self):
        snap = _snap(remaining_lots=0.01, volume_min=0.01)
        worker = PositionWorker()
        out = worker.evaluate(snap, NOW)
        assert not any(i.intent_type == IntentType.PARTIAL_CLOSE for i in out)
        be = [i for i in out if i.source == "tp1_minlot_be"]
        assert len(be) == 1
        assert be[0].intent_type == IntentType.MODIFY_SL

    def test_minlot_breakeven_only_moves_stop_forward(self):
        # SL already at breakeven → no thrash, no intent.
        snap = _snap(remaining_lots=0.01, volume_min=0.01, at_breakeven=True)
        worker = PositionWorker()
        out = worker.evaluate(snap, NOW)
        assert not any(i.source == "tp1_minlot_be" for i in out)

    def test_normal_lot_still_emits_partial(self):
        snap = _snap(remaining_lots=0.10, volume_min=0.01)
        worker = PositionWorker()
        out = worker.evaluate(snap, NOW)
        partials = [i for i in out if i.source == "tp1_partial"]
        assert len(partials) == 1
        assert partials[0].intent_type == IntentType.PARTIAL_CLOSE
        assert not any(i.source == "tp1_minlot_be" for i in out)

    def test_higher_broker_minimum_triggers_guard(self):
        # A 0.10-lot position on an instrument whose minimum is 0.10 cannot be
        # split — guard fires even though 0.10 > the FX default 0.01.
        snap = _snap(remaining_lots=0.10, volume_min=0.10)
        worker = PositionWorker()
        out = worker.evaluate(snap, NOW)
        assert not any(i.intent_type == IntentType.PARTIAL_CLOSE for i in out)
        assert any(i.source == "tp1_minlot_be" for i in out)

    def test_deriv_path_unchanged(self):
        # Deriv still routes to its own breakeven source (atomic contract).
        snap = _snap(remaining_lots=0.10, volume_min=0.01, platform="deriv")
        worker = PositionWorker()
        out = worker.evaluate(snap, NOW)
        assert not any(i.intent_type == IntentType.PARTIAL_CLOSE for i in out)
        assert any(i.source == "tp1_deriv_be" for i in out)


# ── Snapshot field ───────────────────────────────────────────────────


class TestSnapshotVolumeMin:
    def test_default_volume_min(self):
        from types import SimpleNamespace

        from execution.position_snapshot import build_position_snapshot

        pos = SimpleNamespace(
            order_id="T1",
            platform="mt5",
            symbol="EURUSD",
            direction="BUY",
            open_price=1.10,
            lots=0.01,
            sl=1.099,
            tp=1.11,
            tp2=1.12,
        )
        snap = build_position_snapshot(pos, current_price=1.105)
        assert snap.volume_min == 0.01

    def test_volume_min_from_pos(self):
        from types import SimpleNamespace

        from execution.position_snapshot import build_position_snapshot

        pos = SimpleNamespace(
            order_id="T1",
            platform="mt5",
            symbol="DE40",
            direction="BUY",
            open_price=24000.0,
            lots=0.1,
            sl=23900.0,
            tp=24100.0,
            tp2=24200.0,
            volume_min=0.1,
        )
        snap = build_position_snapshot(pos, current_price=24050.0)
        assert snap.volume_min == 0.1
