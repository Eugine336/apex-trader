"""
F4 — Opportunity-cost exit.

Validates:
  1. Mode "off" → method returns immediately, never inspects positions
  2. Shadow mode, all gates pass → logs WOULD-fire, never closes
  3. Each gate failing independently → no WOULD-fire log
  4. "active" mode closes the position via the soft-close idiom
  5. "active" mode on close failure → position retained
  6. "shadow" mode with all gates passing → never calls close_trade
"""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest


def _make_risk_cfg(**overrides):
    defaults = dict(
        opportunity_cost_exit_mode="off",
        opportunity_cost_score_margin=10.0,
        opportunity_cost_max_pnl_pips=5.0,
        opportunity_cost_min_hold_minutes=20.0,
        max_open_trades=3,
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _make_pos(score=70, direction="BUY", symbol="EURUSD",
              open_time=None, tm_trade_id="tm_001", platform="mt5"):
    if open_time is None:
        open_time = datetime.now(timezone.utc) - timedelta(minutes=30)
    return SimpleNamespace(
        score=score,
        direction=direction,
        symbol=symbol,
        open_time=open_time,
        tm_trade_id=tm_trade_id,
        platform=platform,
    )


def _make_tm_trade(pnl_pips=1.0, partial_closed=False):
    return SimpleNamespace(pnl_pips=pnl_pips, partial_closed=partial_closed)


def _make_blocked_candidate(pair="GBPUSD", direction="SHORT", score=85):
    return {"pair": pair, "direction": direction, "score": score}


class _FakeMixin:
    """Minimal stand-in providing the attributes _check_opportunity_cost_exit reads."""

    def __init__(self, risk_cfg, managed_count, blocked, tm_trade,
                 close_result=None):
        self.config = SimpleNamespace(risk=risk_cfg)
        self._managed_count = managed_count
        self.managed_positions = MagicMock()
        self.managed_positions.__len__ = MagicMock(return_value=managed_count)
        self._last_slot_blocked_candidate = blocked
        self.trade_manager = MagicMock()
        self.trade_manager.get_trade = MagicMock(return_value=tm_trade)
        self.platforms = MagicMock()
        if close_result is not None:
            self.platforms.close_trade = MagicMock(return_value=close_result)
        else:
            self.platforms.close_trade = MagicMock(
                side_effect=AssertionError("close_trade must NOT be called"),
            )
        self.position_store = MagicMock()
        self._record_closed_trade = MagicMock()
        self._position_scores = {}


class AssertionError(Exception):
    pass


def _run_check(risk_cfg, *, managed_count=3, blocked=None, tm_trade=None,
               pos=None, now=None, close_result=None):
    from platforms.trading_loop.exit_checks_mixin import ExitChecksMixin

    fake = _FakeMixin(risk_cfg, managed_count, blocked, tm_trade,
                      close_result=close_result)
    if pos is None:
        pos = _make_pos()
    if now is None:
        now = datetime.now(timezone.utc)

    ExitChecksMixin._check_opportunity_cost_exit(fake, "oid_1", pos, now)
    return fake


class TestModeOff:
    """When mode is 'off', the method must be a no-op."""

    def test_off_returns_immediately(self):
        cfg = _make_risk_cfg(opportunity_cost_exit_mode="off")
        fake = _run_check(cfg)
        fake.trade_manager.get_trade.assert_not_called()
        fake.platforms.close_trade.assert_not_called()

    def test_off_ignores_all_gates_passing(self):
        cfg = _make_risk_cfg(opportunity_cost_exit_mode="off")
        fake = _run_check(
            cfg,
            managed_count=3,
            blocked=_make_blocked_candidate(score=90),
            tm_trade=_make_tm_trade(pnl_pips=0.5),
            pos=_make_pos(score=70),
        )
        fake.trade_manager.get_trade.assert_not_called()
        fake.platforms.close_trade.assert_not_called()


class TestShadowAllGatesPass:
    """Shadow mode with all gates passing → log only, never close."""

    def test_shadow_logs_would_fire(self):
        cfg = _make_risk_cfg(opportunity_cost_exit_mode="shadow")
        blocked = _make_blocked_candidate(score=85)
        pos = _make_pos(score=70, open_time=datetime.now(timezone.utc) - timedelta(minutes=30))
        tm_trade = _make_tm_trade(pnl_pips=1.0)

        with patch("platforms.trading_loop.exit_checks_mixin.logger") as mock_logger:
            fake = _run_check(cfg, blocked=blocked, tm_trade=tm_trade, pos=pos)

        mock_logger.info.assert_called_once()
        call_args = mock_logger.info.call_args
        assert "[F4 SHADOW]" in call_args[0][0]
        assert "WOULD fire" in call_args[0][0]
        fake.platforms.close_trade.assert_not_called()

    def test_shadow_never_pops_managed_positions(self):
        cfg = _make_risk_cfg(opportunity_cost_exit_mode="shadow")
        blocked = _make_blocked_candidate(score=85)
        pos = _make_pos(score=70)
        tm_trade = _make_tm_trade(pnl_pips=1.0)

        with patch("platforms.trading_loop.exit_checks_mixin.logger"):
            fake = _run_check(cfg, blocked=blocked, tm_trade=tm_trade, pos=pos)

        fake.managed_positions.pop.assert_not_called()
        fake.position_store.remove_position.assert_not_called()


class TestGateFailures:
    """Each gate failing independently → no WOULD-fire log."""

    def test_no_contention_slots_available(self):
        cfg = _make_risk_cfg(opportunity_cost_exit_mode="shadow")
        blocked = _make_blocked_candidate(score=85)
        tm_trade = _make_tm_trade(pnl_pips=1.0)

        with patch("platforms.trading_loop.exit_checks_mixin.logger") as mock_logger:
            _run_check(cfg, managed_count=2, blocked=blocked,
                       tm_trade=tm_trade, pos=_make_pos(score=70))

        mock_logger.info.assert_not_called()

    def test_no_blocked_candidate(self):
        cfg = _make_risk_cfg(opportunity_cost_exit_mode="shadow")
        tm_trade = _make_tm_trade(pnl_pips=1.0)

        with patch("platforms.trading_loop.exit_checks_mixin.logger") as mock_logger:
            _run_check(cfg, blocked=None, tm_trade=tm_trade,
                       pos=_make_pos(score=70))

        mock_logger.info.assert_not_called()

    def test_position_too_young(self):
        cfg = _make_risk_cfg(opportunity_cost_exit_mode="shadow")
        blocked = _make_blocked_candidate(score=85)
        young_pos = _make_pos(
            score=70,
            open_time=datetime.now(timezone.utc) - timedelta(minutes=5),
        )
        tm_trade = _make_tm_trade(pnl_pips=1.0)

        with patch("platforms.trading_loop.exit_checks_mixin.logger") as mock_logger:
            _run_check(cfg, blocked=blocked, tm_trade=tm_trade, pos=young_pos)

        mock_logger.info.assert_not_called()

    def test_pnl_above_max(self):
        cfg = _make_risk_cfg(opportunity_cost_exit_mode="shadow")
        blocked = _make_blocked_candidate(score=85)
        tm_trade = _make_tm_trade(pnl_pips=10.0)

        with patch("platforms.trading_loop.exit_checks_mixin.logger") as mock_logger:
            _run_check(cfg, blocked=blocked, tm_trade=tm_trade,
                       pos=_make_pos(score=70))

        mock_logger.info.assert_not_called()

    def test_score_delta_below_margin(self):
        cfg = _make_risk_cfg(opportunity_cost_exit_mode="shadow")
        blocked = _make_blocked_candidate(score=75)
        tm_trade = _make_tm_trade(pnl_pips=1.0)

        with patch("platforms.trading_loop.exit_checks_mixin.logger") as mock_logger:
            _run_check(cfg, blocked=blocked, tm_trade=tm_trade,
                       pos=_make_pos(score=70))

        mock_logger.info.assert_not_called()

    def test_partial_closed_blocks(self):
        cfg = _make_risk_cfg(opportunity_cost_exit_mode="shadow")
        blocked = _make_blocked_candidate(score=85)
        tm_trade = _make_tm_trade(pnl_pips=1.0, partial_closed=True)

        with patch("platforms.trading_loop.exit_checks_mixin.logger") as mock_logger:
            _run_check(cfg, blocked=blocked, tm_trade=tm_trade,
                       pos=_make_pos(score=70))

        mock_logger.info.assert_not_called()

    def test_tm_trade_none_blocks(self):
        cfg = _make_risk_cfg(opportunity_cost_exit_mode="shadow")
        blocked = _make_blocked_candidate(score=85)

        with patch("platforms.trading_loop.exit_checks_mixin.logger") as mock_logger:
            _run_check(cfg, blocked=blocked, tm_trade=None,
                       pos=_make_pos(score=70))

        mock_logger.info.assert_not_called()


class TestActiveCloses:
    """'active' mode closes the position via the soft-close idiom."""

    def _success_result(self):
        return SimpleNamespace(success=True, close_price=1.08500)

    def _failure_result(self):
        return SimpleNamespace(success=False, close_price=0.0, error="broker timeout")

    def test_active_calls_close_trade(self):
        cfg = _make_risk_cfg(opportunity_cost_exit_mode="active")
        blocked = _make_blocked_candidate(score=85)
        pos = _make_pos(score=70)
        tm_trade = _make_tm_trade(pnl_pips=1.0)

        with patch("platforms.trading_loop.exit_checks_mixin.logger"):
            fake = _run_check(
                cfg, blocked=blocked, tm_trade=tm_trade, pos=pos,
                close_result=self._success_result(),
            )

        fake.platforms.close_trade.assert_called_once_with("oid_1", pos.platform)

    def test_active_success_records_and_pops(self):
        cfg = _make_risk_cfg(opportunity_cost_exit_mode="active")
        blocked = _make_blocked_candidate(score=85)
        pos = _make_pos(score=70)
        tm_trade = _make_tm_trade(pnl_pips=1.0)

        with patch("platforms.trading_loop.exit_checks_mixin.logger") as mock_logger:
            fake = _run_check(
                cfg, blocked=blocked, tm_trade=tm_trade, pos=pos,
                close_result=self._success_result(),
            )

        fake._record_closed_trade.assert_called_once()
        call_args = fake._record_closed_trade.call_args
        assert call_args[0][0] is pos
        assert call_args[0][1] == 1.08500
        assert "OPPORTUNITY_COST" in call_args[0][2]
        assert "GBPUSD" in call_args[0][2]

        fake.managed_positions.pop.assert_called_once_with("oid_1", None)
        fake.position_store.remove_position.assert_called_once_with("oid_1")

        warn_calls = mock_logger.warning.call_args_list
        assert len(warn_calls) == 1
        assert "[F4]" in warn_calls[0][0][0]
        assert "OPPORTUNITY-COST EXIT" in warn_calls[0][0][0]

    def test_active_failure_retains_position(self):
        cfg = _make_risk_cfg(opportunity_cost_exit_mode="active")
        blocked = _make_blocked_candidate(score=85)
        pos = _make_pos(score=70)
        tm_trade = _make_tm_trade(pnl_pips=1.0)

        with patch("platforms.trading_loop.exit_checks_mixin.logger") as mock_logger:
            fake = _run_check(
                cfg, blocked=blocked, tm_trade=tm_trade, pos=pos,
                close_result=self._failure_result(),
            )

        fake.platforms.close_trade.assert_called_once()
        fake._record_closed_trade.assert_not_called()
        fake.managed_positions.pop.assert_not_called()
        fake.position_store.remove_position.assert_not_called()

        error_calls = mock_logger.error.call_args_list
        assert len(error_calls) == 1
        assert "FAILED" in error_calls[0][0][0]
        assert "position retained" in error_calls[0][0][0]

    def test_active_still_logs_shadow_line(self):
        cfg = _make_risk_cfg(opportunity_cost_exit_mode="active")
        blocked = _make_blocked_candidate(score=85)
        pos = _make_pos(score=70)
        tm_trade = _make_tm_trade(pnl_pips=1.0)

        with patch("platforms.trading_loop.exit_checks_mixin.logger") as mock_logger:
            _run_check(
                cfg, blocked=blocked, tm_trade=tm_trade, pos=pos,
                close_result=self._success_result(),
            )

        info_calls = mock_logger.info.call_args_list
        assert len(info_calls) == 1
        assert "[F4 SHADOW]" in info_calls[0][0][0]


class TestShadowNeverCloses:
    """Shadow mode with all gates passing must never call close_trade."""

    def test_shadow_does_not_call_close(self):
        cfg = _make_risk_cfg(opportunity_cost_exit_mode="shadow")
        blocked = _make_blocked_candidate(score=85)
        pos = _make_pos(score=70)
        tm_trade = _make_tm_trade(pnl_pips=1.0)

        with patch("platforms.trading_loop.exit_checks_mixin.logger"):
            fake = _run_check(cfg, blocked=blocked, tm_trade=tm_trade, pos=pos)

        fake.platforms.close_trade.assert_not_called()
        fake.managed_positions.pop.assert_not_called()
        fake.position_store.remove_position.assert_not_called()


class TestEdgeCases:
    """Boundary and edge-case coverage."""

    def test_score_delta_exactly_at_margin(self):
        cfg = _make_risk_cfg(
            opportunity_cost_exit_mode="shadow",
            opportunity_cost_score_margin=10.0,
        )
        blocked = _make_blocked_candidate(score=80)
        pos = _make_pos(score=70)
        tm_trade = _make_tm_trade(pnl_pips=1.0)

        with patch("platforms.trading_loop.exit_checks_mixin.logger") as mock_logger:
            _run_check(cfg, blocked=blocked, tm_trade=tm_trade, pos=pos)

        mock_logger.info.assert_called_once()
        assert "[F4 SHADOW]" in mock_logger.info.call_args[0][0]

    def test_hold_exactly_at_minimum(self):
        cfg = _make_risk_cfg(
            opportunity_cost_exit_mode="shadow",
            opportunity_cost_min_hold_minutes=20.0,
        )
        blocked = _make_blocked_candidate(score=85)
        pos = _make_pos(
            score=70,
            open_time=datetime.now(timezone.utc) - timedelta(minutes=20),
        )
        tm_trade = _make_tm_trade(pnl_pips=1.0)

        with patch("platforms.trading_loop.exit_checks_mixin.logger") as mock_logger:
            _run_check(cfg, blocked=blocked, tm_trade=tm_trade, pos=pos)

        mock_logger.info.assert_called_once()

    def test_pnl_exactly_at_max(self):
        cfg = _make_risk_cfg(
            opportunity_cost_exit_mode="shadow",
            opportunity_cost_max_pnl_pips=5.0,
        )
        blocked = _make_blocked_candidate(score=85)
        pos = _make_pos(score=70)
        tm_trade = _make_tm_trade(pnl_pips=5.0)

        with patch("platforms.trading_loop.exit_checks_mixin.logger") as mock_logger:
            _run_check(cfg, blocked=blocked, tm_trade=tm_trade, pos=pos)

        mock_logger.info.assert_called_once()

    def test_negative_pnl_eligible(self):
        cfg = _make_risk_cfg(opportunity_cost_exit_mode="shadow")
        blocked = _make_blocked_candidate(score=85)
        pos = _make_pos(score=70)
        tm_trade = _make_tm_trade(pnl_pips=-3.0)

        with patch("platforms.trading_loop.exit_checks_mixin.logger") as mock_logger:
            _run_check(cfg, blocked=blocked, tm_trade=tm_trade, pos=pos)

        mock_logger.info.assert_called_once()
