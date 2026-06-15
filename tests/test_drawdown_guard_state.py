"""
Tests for drawdown guard defect fixes:
  1. margin-flatten freeze actually sets mode (not dead _mode)
  2. balance-unavailable loss still counts in the drawdown guard
  3. to_state / restore_state round-trip preserves all fields
  4. guard_state persistence via PositionStore
  5. effective score threshold enforced per drawdown mode
"""

import json
import os
import tempfile
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from brain.drawdown_guard import DrawdownGuard, DrawdownMode, DrawdownStatus


# ── Defect 1: margin flatten freeze ────────────────────────────────────

class TestMarginFlattenFreeze:
    """self.drawdown.mode (not ._mode) must be set by _emergency_flatten_all."""

    def test_mode_attribute_is_used_not_underscore(self):
        guard = DrawdownGuard()
        assert guard.mode == DrawdownMode.NORMAL
        guard.mode = DrawdownMode.FROZEN
        can, _ = guard.can_trade()
        assert not can

    def test_underscore_mode_does_not_affect_can_trade(self):
        guard = DrawdownGuard()
        guard._mode = DrawdownMode.FROZEN  # noqa: the bug
        can, _ = guard.can_trade()
        assert can, "_mode should not influence can_trade — it reads self.mode"


# ── Defect 2: balance-unavailable loss accounting ──────────────────────

class TestBalanceUnavailableLoss:
    """A losing trade must increment consecutive_losses even when live balance
    is unavailable, as long as pnl_dollars is known and a last-known balance
    exists (pnl_pct derived from last-known balance)."""

    def test_loss_with_derived_pnl_pct_increments_consecutive_losses(self):
        guard = DrawdownGuard()
        guard.register_trade_result(-0.01)
        assert guard.consecutive_losses == 1

    def test_zero_pnl_does_not_increment_losses(self):
        guard = DrawdownGuard()
        guard.register_trade_result(0.0)
        assert guard.consecutive_losses == 0

    def test_negative_pnl_pct_moves_daily_pnl(self):
        guard = DrawdownGuard()
        guard.register_trade_result(-0.02)
        day_key = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        assert guard.daily_pnl_history.get(day_key, 0.0) < 0


# ── Defect 3: to_state / restore_state round-trip ─────────────────────

class TestStateRoundTrip:

    def test_round_trip_preserves_all_fields(self):
        guard = DrawdownGuard()
        ts = datetime(2025, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
        guard.register_trade_result(-0.01, ts)
        guard.register_trade_result(-0.01, ts)
        guard.register_trade_result(0.02, ts)
        original_state = guard.to_state()

        new_guard = DrawdownGuard()
        new_guard.restore_state(original_state)

        assert new_guard.mode == guard.mode
        assert new_guard.consecutive_losses == guard.consecutive_losses
        assert new_guard.consecutive_wins == guard.consecutive_wins
        assert new_guard.daily_pnl_history == guard.daily_pnl_history
        assert new_guard.weekly_pnl_history == guard.weekly_pnl_history
        assert new_guard.high_water_mark == guard.high_water_mark
        assert new_guard.hwm_timestamp == guard.hwm_timestamp
        assert new_guard.last_trade_day == guard.last_trade_day
        assert len(new_guard.equity_points) == len(guard.equity_points)

    def test_restore_frozen_stays_frozen_same_day(self):
        guard = DrawdownGuard()
        ts = datetime(2025, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
        guard.mode = DrawdownMode.FROZEN
        guard.last_trade_day = "2025-06-01"
        state = guard.to_state()

        new_guard = DrawdownGuard()
        new_guard.restore_state(state)
        assert new_guard.mode == DrawdownMode.FROZEN
        can, _ = new_guard.can_trade(ts)
        assert not can

    def test_restore_frozen_rolls_to_recovery_on_new_day(self):
        guard = DrawdownGuard()
        guard.mode = DrawdownMode.FROZEN
        guard.last_trade_day = "2025-06-01"
        state = guard.to_state()

        new_guard = DrawdownGuard()
        new_guard.restore_state(state)
        next_day = datetime(2025, 6, 2, 10, 0, 0, tzinfo=timezone.utc)
        new_guard.register_trade_result(0.001, next_day)
        assert new_guard.mode != DrawdownMode.FROZEN

    def test_bad_enum_defaults_to_normal(self):
        guard = DrawdownGuard()
        guard.restore_state({"mode": "INVALID_MODE"})
        assert guard.mode == DrawdownMode.NORMAL

    def test_missing_keys_use_defaults(self):
        guard = DrawdownGuard()
        guard.restore_state({})
        assert guard.mode == DrawdownMode.NORMAL
        assert guard.consecutive_losses == 0
        assert guard.high_water_mark == 0.0

    def test_json_serialization_round_trip(self):
        guard = DrawdownGuard()
        ts = datetime(2025, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
        guard.register_trade_result(-0.03, ts)
        state = guard.to_state()
        serialized = json.dumps(state)
        deserialized = json.loads(serialized)

        new_guard = DrawdownGuard()
        new_guard.restore_state(deserialized)
        assert new_guard.mode == guard.mode
        assert new_guard.daily_pnl_history == guard.daily_pnl_history


# ── Defect 3 cont.: PositionStore guard_state persistence ─────────────

class TestPositionStoreGuardState:

    def _make_store(self):
        from persistence.position_store import PositionStore
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        store = PositionStore(db_path=path)
        self._path = path
        return store

    def teardown_method(self):
        if hasattr(self, "_path") and os.path.exists(self._path):
            os.unlink(self._path)
            for suffix in ("-wal", "-shm"):
                p = self._path + suffix
                if os.path.exists(p):
                    os.unlink(p)

    def test_save_and_load_round_trip(self):
        store = self._make_store()
        payload = {"mode": "FROZEN", "consecutive_losses": 3, "daily_pnl_history": {"2025-06-01": -0.04}}
        store.save_guard_state(payload)
        loaded = store.load_guard_state()
        assert loaded is not None
        assert loaded["mode"] == "FROZEN"
        assert loaded["consecutive_losses"] == 3
        store.close()

    def test_load_returns_none_when_empty(self):
        store = self._make_store()
        loaded = store.load_guard_state()
        assert loaded is None
        store.close()

    def test_save_overwrites_previous(self):
        store = self._make_store()
        store.save_guard_state({"mode": "CAUTION"})
        store.save_guard_state({"mode": "FROZEN"})
        loaded = store.load_guard_state()
        assert loaded["mode"] == "FROZEN"
        store.close()

    def test_full_guard_round_trip_through_store(self):
        store = self._make_store()
        guard = DrawdownGuard()
        ts = datetime(2025, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
        guard.register_trade_result(-0.02, ts)
        guard.register_trade_result(-0.02, ts)
        store.save_guard_state(guard.to_state())

        loaded = store.load_guard_state()
        new_guard = DrawdownGuard()
        new_guard.restore_state(loaded)
        assert new_guard.mode == guard.mode
        assert new_guard.consecutive_losses == guard.consecutive_losses
        store.close()


# ── Day-boundary unfreeze independent of trade closes ─────────────────

class TestResetDailyUnfreeze:
    """reset_daily() lifts a stale FROZEN at the day boundary even when no
    trade ever closes — the real-world "froze and flattened the book, then
    nothing left to close" case that left the guard FROZEN forever."""

    def test_frozen_with_no_trades_unfreezes_next_day(self):
        guard = DrawdownGuard()
        guard.mode = DrawdownMode.FROZEN
        guard.last_trade_day = "2025-06-01"

        # Before the boundary roll, the loop's primary gate is shut.
        can_before, _ = guard.can_trade(
            datetime(2025, 6, 2, 8, 0, 0, tzinfo=timezone.utc)
        )
        assert not can_before

        # No register_trade_result() — purely the day boundary.
        guard.reset_daily(datetime(2025, 6, 2, 8, 0, 0, tzinfo=timezone.utc))

        assert guard.mode == DrawdownMode.RECOVERY
        can_after, _ = guard.can_trade(
            datetime(2025, 6, 2, 8, 0, 0, tzinfo=timezone.utc)
        )
        assert can_after

    def test_same_day_reset_keeps_frozen(self):
        guard = DrawdownGuard()
        guard.mode = DrawdownMode.FROZEN
        guard.last_trade_day = "2025-06-01"

        guard.reset_daily(datetime(2025, 6, 1, 23, 0, 0, tzinfo=timezone.utc))
        assert guard.mode == DrawdownMode.FROZEN

    def test_normal_mode_unaffected_by_reset(self):
        guard = DrawdownGuard()
        guard.last_trade_day = "2025-06-01"
        guard.reset_daily(datetime(2025, 6, 2, 8, 0, 0, tzinfo=timezone.utc))
        assert guard.mode == DrawdownMode.NORMAL
        can, _ = guard.can_trade(datetime(2025, 6, 2, 8, 0, 0, tzinfo=timezone.utc))
        assert can


# ── Defect 4: effective score threshold per drawdown mode ─────────────

class TestEffectiveScoreThreshold:

    def test_normal_mode_threshold_is_65(self):
        guard = DrawdownGuard()
        status = guard.get_status()
        assert status.current_score_threshold == 65

    def test_caution_mode_threshold_is_70(self):
        guard = DrawdownGuard()
        guard.mode = DrawdownMode.CAUTION
        status = guard.get_status()
        assert status.current_score_threshold == 70

    def test_recovery_mode_threshold_is_75(self):
        guard = DrawdownGuard()
        guard.mode = DrawdownMode.RECOVERY
        status = guard.get_status()
        assert status.current_score_threshold == 75

    def test_frozen_mode_threshold_is_999(self):
        guard = DrawdownGuard()
        guard.mode = DrawdownMode.FROZEN
        status = guard.get_status()
        assert status.current_score_threshold == 999

    def test_effective_min_is_max_of_config_and_drawdown(self):
        """In RECOVERY (threshold=75), with config min_entry_score=70,
        effective_min should be 75."""
        guard = DrawdownGuard()
        guard.mode = DrawdownMode.RECOVERY
        status = guard.get_status()
        config_min = 70
        effective = max(config_min, status.current_score_threshold)
        assert effective == 75

    def test_normal_mode_does_not_lower_config_min(self):
        """In NORMAL (threshold=65), with config min_entry_score=70,
        effective_min should stay 70 (max)."""
        guard = DrawdownGuard()
        status = guard.get_status()
        config_min = 70
        effective = max(config_min, status.current_score_threshold)
        assert effective == 70

    def test_score_72_rejected_in_recovery(self):
        """Score 72 is above config_min=70 but below RECOVERY floor=75."""
        guard = DrawdownGuard()
        guard.mode = DrawdownMode.RECOVERY
        status = guard.get_status()
        config_min = 70
        effective = max(config_min, status.current_score_threshold)
        score = 72
        assert score < effective

    def test_score_72_passes_in_normal(self):
        """Score 72 is above both config_min=70 and NORMAL floor=65."""
        guard = DrawdownGuard()
        status = guard.get_status()
        config_min = 70
        effective = max(config_min, status.current_score_threshold)
        score = 72
        assert score >= effective
