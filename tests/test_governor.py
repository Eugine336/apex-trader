"""Tests for the Portfolio Governor and its plan/re-entry integration."""

from governor import GovernorConfig, PortfolioGovernor
from planning import PlannerConfig, TradePlanContext, TradePlanner


def _pos(symbol: str, direction: str) -> dict:
    return {"symbol": symbol, "direction": direction}


# ── Max open positions ────────────────────────────────────────────────────


def test_max_positions_blocks_at_limit():
    gov = PortfolioGovernor(
        GovernorConfig(
            max_open_positions=3, max_currency_exposure=99, max_sector_exposure=99, max_correlated_positions=99
        )
    )
    book = [_pos("EURUSD", "BUY"), _pos("GBPJPY", "SELL"), _pos("AUDCAD", "BUY")]
    v = gov.check("USDCHF", "SELL", book, account_balance=1000.0)
    assert not v.allowed
    assert v.blocked_by == "max_positions"


def test_allows_when_under_all_limits():
    gov = PortfolioGovernor(GovernorConfig())
    v = gov.check("EURUSD", "BUY", [], account_balance=1000.0)
    assert v.allowed
    assert v.blocked_by is None


# ── Currency exposure ──────────────────────────────────────────────────────


def test_currency_exposure_blocks_at_limit():
    gov = PortfolioGovernor(
        GovernorConfig(max_currency_exposure=2, max_correlated_positions=99, max_sector_exposure=99)
    )
    # Two open positions already involve JPY.
    book = [_pos("USDJPY", "BUY"), _pos("GBPJPY", "SELL")]
    v = gov.check("EURJPY", "BUY", book, account_balance=1000.0)
    assert not v.allowed
    assert v.blocked_by == "currency_exposure"


def test_currency_exposure_allows_under_limit():
    gov = PortfolioGovernor(
        GovernorConfig(max_currency_exposure=3, max_correlated_positions=99, max_sector_exposure=99)
    )
    book = [_pos("USDJPY", "BUY")]
    v = gov.check("EURJPY", "BUY", book, account_balance=1000.0)
    assert v.allowed


# ── Sector exposure ────────────────────────────────────────────────────────


def test_sector_exposure_blocks_at_limit():
    gov = PortfolioGovernor(
        GovernorConfig(max_sector_exposure=2, max_currency_exposure=99, max_correlated_positions=99)
    )
    # Three forex would breach a sector cap of 2 (2 existing + 1 proposed).
    book = [_pos("EURUSD", "BUY"), _pos("AUDCAD", "SELL")]
    v = gov.check("NZDCHF", "BUY", book, account_balance=1000.0)
    assert not v.allowed
    assert v.blocked_by == "sector_exposure"


# ── Correlated positions ────────────────────────────────────────────────────


def test_correlated_positions_block():
    gov = PortfolioGovernor(
        GovernorConfig(max_correlated_positions=1, max_currency_exposure=99, max_sector_exposure=99)
    )
    # EURUSD long and GBPUSD long both carry short-USD exposure (same sign).
    book = [_pos("EURUSD", "BUY")]
    v = gov.check("GBPUSD", "BUY", book, account_balance=1000.0)
    assert not v.allowed
    assert v.blocked_by == "correlated_positions"


def test_opposite_usd_exposure_not_correlated():
    gov = PortfolioGovernor(
        GovernorConfig(max_correlated_positions=1, max_currency_exposure=99, max_sector_exposure=99)
    )
    # EURUSD long (short USD) vs USDCHF long (long USD) — opposite USD sign,
    # and they share no other leg, so not correlated.
    book = [_pos("EURUSD", "BUY")]
    v = gov.check("USDCHF", "BUY", book, account_balance=1000.0)
    assert v.allowed


# ── Daily loss cap ──────────────────────────────────────────────────────────


def test_daily_loss_cap_halts_trading():
    gov = PortfolioGovernor(GovernorConfig(daily_loss_cap_pct=3.0, daily_loss_recovery_pct=1.5))
    gov.set_reference_balance(1000.0)
    gov.update_daily_pnl(-35.0)  # −3.5% > −3% cap
    v = gov.check("EURUSD", "BUY", [], account_balance=1000.0)
    assert not v.allowed
    assert v.blocked_by == "daily_loss_cap"
    assert gov.daily_trading_halted


def test_daily_loss_recovery_resumes_trading():
    gov = PortfolioGovernor(GovernorConfig(daily_loss_cap_pct=3.0, daily_loss_recovery_pct=1.5))
    gov.set_reference_balance(1000.0)
    gov.update_daily_pnl(-35.0)  # halt
    assert gov.daily_trading_halted
    gov.update_daily_pnl(+25.0)  # net −1.0% → above −1.5% recovery
    assert not gov.daily_trading_halted
    v = gov.check("EURUSD", "BUY", [], account_balance=1000.0)
    assert v.allowed


def test_daily_reset_clears_state():
    gov = PortfolioGovernor(GovernorConfig(daily_loss_cap_pct=3.0))
    gov.set_reference_balance(1000.0)
    gov.update_daily_pnl(-50.0)
    assert gov.daily_trading_halted
    gov.reset_daily()
    assert gov.daily_pnl == 0.0
    assert not gov.daily_trading_halted


# ── Mixed: one check blocks, correct reason wins ───────────────────────────


def test_daily_cap_takes_precedence_over_exposure():
    gov = PortfolioGovernor(GovernorConfig(daily_loss_cap_pct=3.0, max_open_positions=1))
    gov.set_reference_balance(1000.0)
    gov.update_daily_pnl(-40.0)
    # Both daily-cap and max-positions would block; daily cap is checked first.
    v = gov.check("EURUSD", "BUY", [_pos("GBPUSD", "BUY")], account_balance=1000.0)
    assert not v.allowed
    assert v.blocked_by == "daily_loss_cap"


# ── Fail-open + disabled ────────────────────────────────────────────────────


def test_disabled_governor_always_allows():
    gov = PortfolioGovernor(GovernorConfig(enabled=False, max_open_positions=0))
    v = gov.check("EURUSD", "BUY", [_pos("GBPUSD", "BUY")], account_balance=1000.0)
    assert v.allowed


def test_fail_open_on_bad_input():
    gov = PortfolioGovernor(GovernorConfig())
    # Passing a non-iterable book triggers the internal guard → fail-open.
    v = gov.check("EURUSD", "BUY", 12345, account_balance=1000.0)  # type: ignore[arg-type]
    assert v.allowed


def test_get_state_reports_exposure_and_blocks():
    gov = PortfolioGovernor(GovernorConfig(max_open_positions=1))
    gov.set_reference_balance(1000.0)
    gov.check("EURUSD", "BUY", [_pos("GBPUSD", "BUY")], account_balance=1000.0)  # block
    state = gov.get_state([_pos("GBPUSD", "BUY")])
    assert state["open_positions"] == 1
    assert "USD" in state["currency_exposure"]
    assert len(state["recent_blocks"]) == 1


# ── Planner integration ─────────────────────────────────────────────────────


def _enter_ctx(**overrides) -> TradePlanContext:
    base = dict(
        symbol="EURUSD",
        pip_size=0.0001,
        atr_pips=20.0,
        current_price=1.1000,
        direction="LONG",
        scanner_score=82.0,
        zone_quality=0.85,
        zone_entry_price=1.0998,
        de_confidence=0.75,
        de_tf_alignment=0.80,
        rl_action=1,
        rl_confidence=0.6,
        rl_expected_r=2.2,
        proposed_sl_price=1.0980,
        proposed_sl_pips=20.0,
        structure_sl_available=True,
        brain_entry_mode="MARKET",
        account_balance=1000.0,
        base_risk_pct=0.5,
    )
    base.update(overrides)
    return TradePlanContext(**base)


def test_planner_skips_when_governor_blocks():
    gov = PortfolioGovernor(GovernorConfig(max_open_positions=1))
    planner = TradePlanner(PlannerConfig(), governor=gov)
    ctx = _enter_ctx(open_position_book=[("GBPUSD", "BUY")])
    plan = planner.plan_trade(ctx)
    assert plan.action == "SKIP"
    assert plan.governor_blocked_by == "max_positions"


def test_planner_enters_when_governor_allows():
    gov = PortfolioGovernor(GovernorConfig())
    planner = TradePlanner(PlannerConfig(), governor=gov)
    ctx = _enter_ctx(open_position_book=[])
    plan = planner.plan_trade(ctx)
    assert plan.action == "ENTER"
    assert plan.governor_blocked_by is None


def test_planner_without_governor_still_enters():
    planner = TradePlanner(PlannerConfig())  # no governor
    ctx = _enter_ctx(open_position_book=[("GBPUSD", "BUY")])
    plan = planner.plan_trade(ctx)
    assert plan.action == "ENTER"


# ── Re-entry / scale-in plan-override integration ──────────────────────────


def _make_position(plan_scale_in_allowed=None):
    from platforms.base_connector import OrderResult
    from platforms.trading_loop.positions import ManagedPosition

    order = OrderResult(
        success=True,
        order_id="o1",
        fill_price=1.1,
        requested_price=1.1,
        slippage_pips=0.0,
        lots=0.1,
        symbol="EURUSD",
        direction="BUY",
        sl=1.09,
        tp=1.12,
        platform="mt5",
    )
    pos = ManagedPosition(order, tp1=1.11, tp2=1.12)
    pos.plan_scale_in_allowed = plan_scale_in_allowed
    return pos


def test_managed_position_plan_scale_in_defaults_none():
    pos = _make_position()
    assert pos.plan_scale_in_allowed is None


def test_scale_in_plan_override_blocks_and_allows():
    import pytest

    pytest.importorskip("torch")
    pytest.importorskip("aiosqlite")
    from types import SimpleNamespace

    from platforms.main_loop import TradingLoop

    fake = SimpleNamespace()
    # None → defer to global (True at this per-position gate)
    assert TradingLoop._scale_in_allowed_for(fake, _make_position(None)) is True
    # True → allowed
    assert TradingLoop._scale_in_allowed_for(fake, _make_position(True)) is True
    # False → blocked for this position
    assert TradingLoop._scale_in_allowed_for(fake, _make_position(False)) is False


def test_governor_allows_add_blocks_when_governor_blocks():
    import pytest

    pytest.importorskip("torch")
    pytest.importorskip("aiosqlite")
    from types import SimpleNamespace

    from platforms.main_loop import TradingLoop

    # Governor whose daily cap is already breached → blocks any add.
    gov = PortfolioGovernor(GovernorConfig(daily_loss_cap_pct=3.0))
    gov.set_reference_balance(1000.0)
    gov.update_daily_pnl(-50.0)
    pos = _make_position()
    fake = SimpleNamespace(
        _governor=gov,
        _last_known_balance=1000.0,
        managed_positions={},
    )
    assert TradingLoop._governor_allows_add(fake, pos) is False

    # No governor → always allows.
    fake_no_gov = SimpleNamespace(_governor=None)
    assert TradingLoop._governor_allows_add(fake_no_gov, pos) is True
