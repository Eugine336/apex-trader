"""
Tests for the event-driven retrain wiring (verification gap #3, caller hookup).

``AdaptiveOptimizer`` already exposed three notification hooks
(``notify_loss_streak`` / ``notify_drawdown_escalation`` / ``notify_regime_change``)
that ARM an out-of-band retrain. These tests verify the *callers* are now wired:

* DrawdownGuard fires an ``on_mode_change(old, new)`` callback on every live mode
  transition, and wiring it to ``notify_drawdown_escalation`` arms a retrain only
  on a tightening escalation (NORMAL→RECOVERY), not a de-escalation.
* RegimeDetector fires an ``on_regime_change(old, new)`` callback when a pair's
  committed regime flips, and wiring it to ``notify_regime_change`` arms a retrain.
* The trade-close path's consecutive-loss counter increments on a loss, resets on
  a win/scratch, and notifies the optimiser with the running count — arming a
  retrain once the streak reaches the threshold.

The retrain-arming assertions drive a REAL ``AdaptiveOptimizer`` (its
``should_retrain`` consumes the armed flag). ``last_train_time`` is pinned to
"now" so the periodic 50-trade / 7-day triggers stay quiet and only the event
flag can flip ``should_retrain`` to True.
"""

import os
import tempfile
from datetime import datetime, timezone

import pytest

from adaptive.optimizer import AdaptiveOptimizer
from brain.drawdown_guard import DrawdownGuard, DrawdownMode
from adaptive.regime_detector import (
    RegimeDetector,
    REGIME_TRENDING_UP,
    REGIME_UNKNOWN,
)


# ── Helpers ──────────────────────────────────────────────────────────────────


def _fresh_optimizer():
    """A real optimiser with the periodic retrain triggers pinned quiet so only
    the event-driven flag can flip ``should_retrain``."""
    opt = AdaptiveOptimizer()
    opt._last_train_time = datetime.now(timezone.utc)
    opt._trades_since_train = 0
    return opt


def _armed(opt) -> bool:
    """True iff an out-of-band retrain is currently armed, WITHOUT consuming it
    (should_retrain would clear the flag)."""
    return bool(opt._event_retrain_pending)


@pytest.fixture
def tmp_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    yield path
    for p in (path, path + "-wal", path + "-shm"):
        try:
            os.remove(p)
        except OSError:
            pass


# Deterministic 20-bar uptrend → classifies TRENDING_UP.
_UPTREND = [100.0 + i for i in range(20)]


# ── DrawdownGuard → notify_drawdown_escalation ───────────────────────────────


class TestDrawdownEscalationRetrain:
    def test_transition_fires_callback(self):
        seen: list[tuple[str, str]] = []
        guard = DrawdownGuard(on_mode_change=lambda o, n: seen.append((o, n)))
        # One -4% trade escalates NORMAL → RECOVERY (daily_pnl <= -0.03).
        guard.register_trade_result(-0.04)
        assert seen, "expected a mode-change callback on escalation"
        old, new = seen[-1]
        assert old == DrawdownMode.NORMAL.value
        assert new == DrawdownMode.RECOVERY.value

    def test_no_transition_no_callback(self):
        seen: list[tuple[str, str]] = []
        guard = DrawdownGuard(on_mode_change=lambda o, n: seen.append((o, n)))
        # A tiny win keeps the guard in NORMAL — no transition, no callback.
        guard.register_trade_result(0.001)
        assert seen == []

    def test_setter_wires_callback(self):
        seen: list[tuple[str, str]] = []
        guard = DrawdownGuard()
        guard.set_mode_change_callback(lambda o, n: seen.append((o, n)))
        guard.register_trade_result(-0.04)
        assert seen and seen[-1][1] == DrawdownMode.RECOVERY.value

    def test_escalation_arms_optimizer_retrain(self):
        opt = _fresh_optimizer()
        guard = DrawdownGuard()
        guard.set_mode_change_callback(opt.notify_drawdown_escalation)
        assert not _armed(opt)
        guard.register_trade_result(-0.04)  # NORMAL → RECOVERY (tightening)
        assert _armed(opt)
        # The armed flag flips should_retrain True, then clears (one per event).
        assert opt.should_retrain() is True
        assert not _armed(opt)

    def test_deescalation_does_not_arm(self):
        opt = _fresh_optimizer()
        # Directly exercise the hook with a loosening transition.
        opt.notify_drawdown_escalation("RECOVERY", "NORMAL")
        assert not _armed(opt)
        assert opt.should_retrain() is False

    def test_callback_failure_never_breaks_trade_result(self):
        def _boom(_o, _n):
            raise RuntimeError("callback blew up")

        guard = DrawdownGuard(on_mode_change=_boom)
        # Must not raise — the result is still returned and the mode still moved.
        status = guard.register_trade_result(-0.04)
        assert status.mode == DrawdownMode.RECOVERY.value


# ── RegimeDetector → notify_regime_change ────────────────────────────────────


class TestRegimeChangeRetrain:
    def _detector(self, tmp_db, **kw):
        params = dict(
            db_path=tmp_db,
            enabled=True,
            lookback_bars=20,
            hysteresis_bars=2,
            volatility_short_window=3,
            volatility_long_window=10,
        )
        params.update(kw)
        return RegimeDetector(**params)

    def test_flip_fires_callback(self, tmp_db):
        seen: list[tuple[str, str]] = []
        det = self._detector(tmp_db, on_regime_change=lambda o, n: seen.append((o, n)))
        # hysteresis_bars=2 → two agreeing observations commit the flip.
        det.update("EURUSD", _UPTREND)
        det.update("EURUSD", _UPTREND)
        det.close()
        assert seen, "expected a regime-change callback on the committed flip"
        old, new = seen[-1]
        assert old == REGIME_UNKNOWN
        assert new == REGIME_TRENDING_UP

    def test_no_flip_no_repeat_callback(self, tmp_db):
        seen: list[tuple[str, str]] = []
        det = self._detector(tmp_db, on_regime_change=lambda o, n: seen.append((o, n)))
        # Commit the flip, then keep feeding the SAME regime — no new transition.
        det.update("EURUSD", _UPTREND)
        det.update("EURUSD", _UPTREND)
        det.update("EURUSD", _UPTREND)
        det.update("EURUSD", _UPTREND)
        det.close()
        assert len(seen) == 1, f"expected exactly one flip, got {seen}"

    def test_setter_wires_callback(self, tmp_db):
        seen: list[tuple[str, str]] = []
        det = self._detector(tmp_db)
        det.set_regime_change_callback(lambda o, n: seen.append((o, n)))
        det.update("EURUSD", _UPTREND)
        det.update("EURUSD", _UPTREND)
        det.close()
        assert seen and seen[-1][1] == REGIME_TRENDING_UP

    def test_flip_arms_optimizer_retrain(self, tmp_db):
        opt = _fresh_optimizer()
        det = self._detector(tmp_db, on_regime_change=opt.notify_regime_change)
        assert not _armed(opt)
        det.update("EURUSD", _UPTREND)
        det.update("EURUSD", _UPTREND)
        det.close()
        assert _armed(opt)
        assert opt.should_retrain() is True

    def test_callback_failure_never_breaks_update(self, tmp_db):
        def _boom(_o, _n):
            raise RuntimeError("callback blew up")

        det = self._detector(tmp_db, on_regime_change=_boom)
        # Must not raise — classification continues and the flip still commits.
        det.update("EURUSD", _UPTREND)
        st = det.update("EURUSD", _UPTREND)
        det.close()
        assert st.regime == REGIME_TRENDING_UP


# ── Optimizer-side loss-streak threshold ─────────────────────────────────────


class TestLossStreakThreshold:
    def test_arms_only_at_threshold(self):
        opt = _fresh_optimizer()
        # Default threshold is 3 consecutive losses.
        assert opt.loss_streak_retrain_threshold == 3
        assert opt.notify_loss_streak(1) is False
        assert not _armed(opt)
        assert opt.notify_loss_streak(2) is False
        assert not _armed(opt)
        assert opt.notify_loss_streak(3) is True
        assert _armed(opt)

    def test_streak_semantics_increment_and_reset(self):
        """Drive a real optimiser through the EXACT trade-close counter rule
        (loss → increment + notify; win/scratch → reset) and assert the retrain
        arms only once the streak reaches the threshold, and a win clears it."""
        opt = _fresh_optimizer()
        streak = 0

        def close(pnl_dollars: float) -> None:
            nonlocal streak
            if pnl_dollars < 0.0:
                streak += 1
                opt.notify_loss_streak(streak)
            else:
                streak = 0

        close(-10.0)   # streak 1
        close(-10.0)   # streak 2
        assert not _armed(opt)
        close(-10.0)   # streak 3 → arms
        assert streak == 3
        assert _armed(opt)

        # Consume the armed retrain, then a win resets the streak.
        assert opt.should_retrain() is True
        close(+25.0)
        assert streak == 0
        close(0.0)     # scratch — still reset, no increment
        assert streak == 0


# ── Real trade-close path: counter increment / reset (production method) ─────


class _Recorder:
    """Stand-in ml_adapter capturing notify_loss_streak calls."""

    def __init__(self):
        self.streaks: list[int] = []

    def register_new_trade(self, exit_cause=None):  # noqa: D401 - test stub
        pass

    def notify_loss_streak(self, n):
        self.streaks.append(int(n))
        return True


class _Stub:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def _fake_ctx(ml_adapter):
    from types import SimpleNamespace

    # Every subsystem the close path touches set to None (each is guarded by an
    # ``is not None`` check) except the ml_adapter recorder.
    return SimpleNamespace(
        adaptive_weight_provider=None,
        drawdown_guard=None,
        risk_engine=None,
        portfolio_governor=None,
        account_risk=None,
        outcome_feedback=None,
        counterfactual_engine=None,
        signal_ledger=None,
        ml_adapter=ml_adapter,
        tuner_agent=None,
        post_close_tracker=None,
        trade_journal=None,
        capital_allocator=None,
        execution_profiles=None,
        behavior_discovery=None,
        param_evolver=None,
        outcome_logger=None,
        regime_detector=None,
        re_entry_manager=None,
    )


def _fake_self(rec):
    """Minimal fake ``self`` carrying only the attributes ``_on_trade_closed``
    reads before/after the loss-streak block (all other sections are guarded by
    None ctx subsystems or wrapped in try/except)."""
    return _Stub(
        _entry_context={},
        _candidate_positions={},
        _zone_edge=_Stub(record_trade=lambda *a, **k: None),
        _calibration_engine=None,
        _ctx=_fake_ctx(rec),
        _pm=_Stub(get_platform_balance=lambda _s: 10000.0),
        _consecutive_losses=0,
        _tick_store=_Stub(get_latest=lambda _s: None),
        _closed_trade_count=0,
        _last_close_time={},
        _be_stop_cooldown={},
        _be_cooldown_seconds=0,
    )


class TestTradeCloseLossCounter:
    def test_loss_increments_and_notifies_running_count(self):
        from event_driven_bootstrap import EventDrivenSystem

        rec = _Recorder()
        slf = _fake_self(rec)
        for _ in range(3):
            EventDrivenSystem._on_trade_closed(
                slf, "EURUSD", "LONG", -50.0, -5.0, "t",
            )
        assert slf._consecutive_losses == 3
        assert rec.streaks == [1, 2, 3]

    def test_win_resets_counter(self):
        from event_driven_bootstrap import EventDrivenSystem

        rec = _Recorder()
        slf = _fake_self(rec)
        EventDrivenSystem._on_trade_closed(slf, "EURUSD", "LONG", -50.0, -5.0, "t")
        EventDrivenSystem._on_trade_closed(slf, "EURUSD", "LONG", -50.0, -5.0, "t")
        assert slf._consecutive_losses == 2
        # A win resets the streak; no further notify on the win.
        EventDrivenSystem._on_trade_closed(slf, "EURUSD", "LONG", 75.0, 7.0, "t")
        assert slf._consecutive_losses == 0
        assert rec.streaks == [1, 2]

    def test_scratch_resets_without_notifying(self):
        from event_driven_bootstrap import EventDrivenSystem

        rec = _Recorder()
        slf = _fake_self(rec)
        EventDrivenSystem._on_trade_closed(slf, "EURUSD", "LONG", -50.0, -5.0, "t")
        # Exactly-zero P&L is not a loss — resets the streak, no notify.
        EventDrivenSystem._on_trade_closed(slf, "EURUSD", "LONG", 0.0, 0.0, "t")
        assert slf._consecutive_losses == 0
        assert rec.streaks == [1]
