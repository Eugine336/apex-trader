"""Tests for the SL-modify minimum-room guard and the data-path kill-switch.

Fix 1 — a breakeven / trailing / profit-protection SL move that lands within
the broker minimum stop distance of the current price must be DEFERRED (dropped)
rather than emitted, otherwise the connector clamps it to a forced level that
pins the stop near breakeven and chokes a still-running trade.

Fix 2 — ``AppConfig.features.data_path_fixes_enabled`` defaults OFF so the live
entry builder keeps its original pre-fix behaviour until explicitly enabled.
"""

from datetime import datetime, timezone

from execution.intents import Intent, IntentType
from execution.position_snapshot import PositionSnapshot
from execution.position_worker import (
    PositionWorker,
    WorkerConfig,
    sl_within_min_room,
)


def _snap(direction="BUY", current_price=1.10500, pip_size=0.0001, symbol="EURUSD"):
    return PositionSnapshot(
        order_id="T1",
        platform="mt5",
        symbol=symbol,
        direction=direction,
        entry_price=1.10000,
        lots=0.1,
        remaining_lots=0.1,
        open_time=datetime(2025, 1, 1, 12, 0, tzinfo=timezone.utc),
        score=85,
        sl=1.09900,
        sl_original=1.09900,
        tp1=1.11000,
        tp2=1.12000,
        tp2_original=1.12000,
        current_price=current_price,
        pip_size=pip_size,
    )


# ── sl_within_min_room ──────────────────────────────────────────────────────

class TestSlWithinMinRoom:
    def test_too_close_is_flagged(self):
        # SL 1 pip from price, 2-pip floor → too close
        assert sl_within_min_room(1.10500, 1.10510, 0.0001, 2.0) is True

    def test_far_enough_is_allowed(self):
        # SL 5 pips from price, 2-pip floor → fine
        assert sl_within_min_room(1.10500, 1.10000, 0.0001, 2.0) is False

    def test_exactly_at_price_is_too_close(self):
        assert sl_within_min_room(1.10500, 1.10500, 0.0001, 2.0) is True

    def test_jpy_pip_scaling(self):
        # JPY pip_size is 0.01; 1-pip gap with a 2-pip floor → too close
        assert sl_within_min_room(161.500, 161.510, 0.01, 2.0) is True
        # 3-pip gap → fine
        assert sl_within_min_room(161.500, 161.530, 0.01, 2.0) is False

    def test_zero_room_disables_guard(self):
        assert sl_within_min_room(1.10500, 1.10500, 0.0001, 0.0) is False

    def test_invalid_inputs_never_block(self):
        assert sl_within_min_room(0.0, 1.1, 0.0001, 2.0) is False
        assert sl_within_min_room(1.1, 0.0, 0.0001, 2.0) is False
        assert sl_within_min_room(1.1, 1.1, 0.0, 2.0) is False


# ── PositionWorker._drop_too_close_sl_moves ─────────────────────────────────

class TestDropTooCloseSlMoves:
    def _worker(self, room=2.0):
        return PositionWorker(WorkerConfig(min_sl_modify_room_pips=room))

    def test_drops_too_close_modify_sl(self):
        worker = self._worker()
        snap = _snap(current_price=1.10500)
        intents = [Intent.modify_sl(
            symbol="EURUSD", ticket="T1", new_sl=1.10501,
            source="breakeven", reason="BE",
        )]
        kept = worker._drop_too_close_sl_moves(snap, intents)
        assert kept == []

    def test_keeps_far_modify_sl(self):
        worker = self._worker()
        snap = _snap(current_price=1.10500)
        intents = [Intent.modify_sl(
            symbol="EURUSD", ticket="T1", new_sl=1.10000,
            source="trailing", reason="trail",
        )]
        kept = worker._drop_too_close_sl_moves(snap, intents)
        assert len(kept) == 1

    def test_never_drops_close_intents(self):
        worker = self._worker()
        snap = _snap(current_price=1.10500)
        close = Intent.close(symbol="EURUSD", ticket="T1", source="tp2", reason="tp2")
        kept = worker._drop_too_close_sl_moves(snap, [close])
        assert len(kept) == 1
        assert kept[0].intent_type == IntentType.CLOSE

    def test_room_zero_keeps_everything(self):
        worker = self._worker(room=0.0)
        snap = _snap(current_price=1.10500)
        intents = [Intent.modify_sl(
            symbol="EURUSD", ticket="T1", new_sl=1.10500,
            source="breakeven", reason="BE",
        )]
        kept = worker._drop_too_close_sl_moves(snap, intents)
        assert len(kept) == 1


# ── WorkerConfig default ────────────────────────────────────────────────────

def test_worker_config_default_room():
    assert WorkerConfig().min_sl_modify_room_pips == 2.0


# ── FeatureFlags kill-switch ────────────────────────────────────────────────

def test_feature_flag_defaults_off():
    from config import AppConfig, FeatureFlags

    assert FeatureFlags().data_path_fixes_enabled is False
    assert AppConfig().features.data_path_fixes_enabled is False
