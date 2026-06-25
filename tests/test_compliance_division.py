"""Tests for the Compliance Division (Department 3 — pure permit layer)."""

from __future__ import annotations

from types import SimpleNamespace

from compliance import (
    ComplianceAccount,
    ComplianceBook,
    ComplianceCandidate,
    ComplianceDivision,
)


# ── Test doubles ──────────────────────────────────────────────────────────


class _DrawdownGuard:
    def __init__(self, mode: str = "NORMAL"):
        self._mode = mode

    def get_status(self):
        return SimpleNamespace(mode=self._mode)


class _AccountRisk:
    def __init__(self, halted=False, heat_blocked=False, heat=0.0, pct=0.0):
        self._halted = halted
        self._heat_blocked = heat_blocked
        self._heat = heat
        self._pct = pct

    def daily_loss_halted(self, account):  # noqa: ARG002
        return self._halted

    def combined_pnl_pct(self, account):  # noqa: ARG002
        return self._pct

    def heat_blocked(self, account):  # noqa: ARG002
        return self._heat_blocked

    def heat(self, account):  # noqa: ARG002
        return self._heat


class _NewsGuard:
    def __init__(self, clear=True):
        self._clear = clear

    def check(self, _pairs):
        return SimpleNamespace(
            is_clear=self._clear,
            warning_message="" if self._clear else "NEWS BLOCK: FOMC",
        )


class _SpreadMonitor:
    def __init__(self, safe=True):
        self._safe = safe

    def is_spread_safe(self, _pair, _spread):
        return (self._safe, "spread ok" if self._safe else "spread 5x too wide")


class _RiskSM:
    def __init__(self, state):
        self._state = state

    @property
    def state(self):
        return self._state


def _candidate(symbol="EURUSD", direction="LONG"):
    return ComplianceCandidate(symbol=symbol, direction=direction)


def _account(key="mt5:123", balance=10_000.0):
    return ComplianceAccount(account_key=key, balance=balance)


def _full_division(**overrides):
    """A division with every subsystem healthy/permissive by default."""
    base = dict(
        drawdown_guard=_DrawdownGuard("NORMAL"),
        account_risk=_AccountRisk(),
        news_guard=_NewsGuard(clear=True),
        spread_monitor=_SpreadMonitor(safe=True),
        is_market_open=lambda _s: True,
        is_broker_available=lambda _s: True,
        get_spread_pips=lambda _s: 1.0,
        max_open_positions=8,
    )
    base.update(overrides)
    return ComplianceDivision(**base)


# ── Happy path ─────────────────────────────────────────────────────────────


class TestApproved:
    def test_all_clear_approves(self):
        div = _full_division()
        verdict = div.permit(_candidate(), ComplianceBook([]), _account())
        assert verdict.approved is True
        assert verdict.reasons == []
        # Every check ran.
        assert len(verdict.checks_run) == 10

    def test_disabled_subsystems_fail_closed(self):
        # Bug #31: a division wired with nothing must REJECT — the safety-critical
        # checks (market_open, broker_available, spread_ok) fail CLOSED when their
        # source is unbound rather than silently permitting the trade.
        div = ComplianceDivision()
        verdict = div.permit(_candidate(), ComplianceBook([]), _account())
        assert verdict.rejected
        assert any("market_open" in r for r in verdict.reasons)
        assert any("broker_available" in r for r in verdict.reasons)
        assert any("spread_ok" in r for r in verdict.reasons)


# ── Individual vetoes ────────────────────────────────────────────────────


class TestVetoes:
    def test_market_closed_rejects(self):
        div = _full_division(is_market_open=lambda _s: False)
        verdict = div.permit(_candidate(), ComplianceBook([]), _account())
        assert verdict.rejected
        assert any("market_open" in r for r in verdict.reasons)

    def test_broker_unavailable_rejects(self):
        div = _full_division(is_broker_available=lambda _s: False)
        verdict = div.permit(_candidate(), ComplianceBook([]), _account())
        assert verdict.rejected
        assert any("broker_available" in r for r in verdict.reasons)

    def test_news_block_rejects(self):
        div = _full_division(news_guard=_NewsGuard(clear=False))
        verdict = div.permit(_candidate(), ComplianceBook([]), _account())
        assert verdict.rejected
        assert any("news_clear" in r for r in verdict.reasons)

    def test_wide_spread_rejects(self):
        div = _full_division(spread_monitor=_SpreadMonitor(safe=False))
        verdict = div.permit(_candidate(), ComplianceBook([]), _account())
        assert verdict.rejected
        assert any("spread_ok" in r for r in verdict.reasons)

    def test_duplicate_position_rejects(self):
        book = ComplianceBook([SimpleNamespace(symbol="EURUSD", direction="LONG")])
        div = _full_division()
        verdict = div.permit(_candidate("EURUSD", "LONG"), book, _account())
        assert verdict.rejected
        assert any("duplicate" in r for r in verdict.reasons)

    def test_opposite_direction_is_not_duplicate(self):
        book = ComplianceBook([SimpleNamespace(symbol="EURUSD", direction="SHORT")])
        div = _full_division()
        verdict = div.permit(_candidate("EURUSD", "LONG"), book, _account())
        assert verdict.approved

    def test_daily_loss_halt_rejects(self):
        div = _full_division(account_risk=_AccountRisk(halted=True, pct=-6.5))
        verdict = div.permit(_candidate(), ComplianceBook([]), _account())
        assert verdict.rejected
        assert any("daily_loss" in r for r in verdict.reasons)

    def test_heat_block_rejects(self):
        div = _full_division(account_risk=_AccountRisk(heat_blocked=True, heat=2.5))
        verdict = div.permit(_candidate(), ComplianceBook([]), _account())
        assert verdict.rejected
        assert any("heat_ok" in r for r in verdict.reasons)

    def test_drawdown_frozen_rejects(self):
        div = _full_division(drawdown_guard=_DrawdownGuard("FROZEN"))
        verdict = div.permit(_candidate(), ComplianceBook([]), _account())
        assert verdict.rejected
        assert any("drawdown_not_frozen" in r for r in verdict.reasons)

    def test_max_positions_rejects(self):
        book = ComplianceBook(
            [SimpleNamespace(symbol=f"SYM{i}", direction="LONG") for i in range(8)]
        )
        div = _full_division(max_open_positions=8)
        verdict = div.permit(_candidate(), book, _account())
        assert verdict.rejected
        assert any("max_positions" in r for r in verdict.reasons)

    def test_portfolio_risk_state_rejects(self):
        from risk.portfolio_risk_state import PortfolioRiskState

        div = _full_division(
            portfolio_risk_sm=_RiskSM(PortfolioRiskState.DEFENSIVE),
        )
        verdict = div.permit(_candidate(), ComplianceBook([]), _account())
        assert verdict.rejected
        assert any("portfolio_risk_state" in r for r in verdict.reasons)

    def test_portfolio_risk_normal_passes(self):
        from risk.portfolio_risk_state import PortfolioRiskState

        div = _full_division(portfolio_risk_sm=_RiskSM(PortfolioRiskState.NORMAL))
        verdict = div.permit(_candidate(), ComplianceBook([]), _account())
        assert verdict.approved


# ── Fail-closed semantics (V6) ─────────────────────────────────────────────


class _ExplodingMarketOpen:
    def __call__(self, _symbol):
        raise RuntimeError("broker spec lookup blew up")


class TestFailClosed:
    def test_exception_in_check_fails_closed(self):
        div = _full_division(is_market_open=_ExplodingMarketOpen())
        verdict = div.permit(_candidate(), ComplianceBook([]), _account())
        assert verdict.rejected
        assert any("fail-closed" in r for r in verdict.reasons)

    def test_one_failure_does_not_hide_others(self):
        # No short-circuit: both the market AND the news veto are reported.
        div = _full_division(
            is_market_open=lambda _s: False,
            news_guard=_NewsGuard(clear=False),
        )
        verdict = div.permit(_candidate(), ComplianceBook([]), _account())
        assert verdict.rejected
        assert any("market_open" in r for r in verdict.reasons)
        assert any("news_clear" in r for r in verdict.reasons)


# ── V7: single daily-loss source (governor concentration-only) ─────────────


class TestGovernorExposureOnly:
    def test_check_exposure_only_ignores_daily_loss_halt(self):
        from governor.models import GovernorConfig
        from governor.portfolio_governor import PortfolioGovernor

        gov = PortfolioGovernor(GovernorConfig())
        gov.set_reference_balance(10_000.0)
        # A genuine -5% realised day trips (and holds) the daily-loss halt —
        # well past both the cap and the recovery band, so the hysteresis in
        # ``_evaluate_halt`` keeps it halted.
        gov.update_daily_pnl(-500.0)
        assert gov.daily_trading_halted is True

        # Full check() still blocks on its daily-loss halt …
        full = gov.check("EURUSD", "LONG", [], 10_000.0)
        assert full.allowed is False

        # … but the concentration-only entry point does NOT (daily-loss is now
        # owned exclusively by the Compliance Division — V7).
        exposure = gov.check_exposure_only("EURUSD", "LONG", [], 10_000.0)
        assert exposure.allowed is True

    def test_check_exposure_only_ignores_max_positions(self):
        from governor.models import GovernorConfig
        from governor.portfolio_governor import PortfolioGovernor

        cfg = GovernorConfig()
        gov = PortfolioGovernor(cfg)
        positions = [(f"SYM{i}", "LONG") for i in range(cfg.max_open_positions)]
        # Full check() blocks on the hard position cap …
        full = gov.check("EURUSD", "LONG", positions, 10_000.0)
        assert full.allowed is False
        # … exposure-only skips it (Compliance owns max-positions now).
        exposure = gov.check_exposure_only("EURUSD", "LONG", positions, 10_000.0)
        assert exposure.allowed is True
