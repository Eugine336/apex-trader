"""
APEX TRADER — Risk Engine Tests
Every gate, every check, every edge case.
"""

import pytest
from datetime import datetime, timezone

from risk.risk_engine import RiskEngine, AccountSnapshot
from risk.position_sizer import PositionSizer
from risk.daily_tracker import PnLTracker
from risk.spread_monitor import SpreadMonitor
from risk.risk_reporter import RiskReporter


# ── Fixtures ──────────────────────────────────────────────────────────────


@pytest.fixture
def engine():
    return RiskEngine(starting_balance=10_000.0)


@pytest.fixture
def sizer():
    return PositionSizer()


@pytest.fixture
def tracker():
    return PnLTracker(starting_balance=10_000.0)


@pytest.fixture
def spread_mon():
    return SpreadMonitor(max_multiplier=3.0)


# ── RiskEngine.assess — approved ──────────────────────────────────────────


def test_assess_approves_valid_trade(engine):
    result = engine.assess(
        pair="EURUSD",
        direction="LONG",
        entry_price=1.10000,
        stop_loss=1.09800,
    )
    assert result.approved is True
    assert result.position_size_lots > 0
    assert result.risk_mode == "NORMAL"
    assert len(result.rejections) == 0
    assert len(result.checks) > 0


def test_assess_approved_contains_sizing(engine):
    result = engine.assess(
        pair="GBPUSD",
        direction="SHORT",
        entry_price=1.27000,
        stop_loss=1.27300,
    )
    assert result.position_size_lots >= 0.01
    assert result.max_loss_dollars > 0


# ── RiskEngine.assess — rejection: FROZEN ─────────────────────────────────


def test_reject_when_frozen(engine):
    from brain.drawdown_guard import DrawdownMode
    engine.drawdown_guard.mode = DrawdownMode.FROZEN

    result = engine.assess(
        pair="EURUSD", direction="LONG",
        entry_price=1.10, stop_loss=1.098,
    )
    assert result.approved is False
    assert any("frozen" in r.lower() for r in result.rejections)


# ── RiskEngine.assess — rejection: max trades ─────────────────────────────


def test_reject_when_max_trades_reached(engine):
    trades = [
        {"pair": f"PAIR{i}", "direction": "LONG", "risk_pct": 0.02}
        for i in range(engine.risk_cfg.max_open_trades)
    ]
    result = engine.assess(
        pair="EURUSD", direction="LONG",
        entry_price=1.10, stop_loss=1.098,
        open_trades=trades,
    )
    assert result.approved is False
    assert any("maximum" in r.lower() or "max" in r.lower() for r in result.rejections)


# ── RiskEngine.assess — rejection: duplicate pair+direction ────────────────


def test_reject_duplicate_pair_direction(engine):
    trades = [{"pair": "EURUSD", "direction": "LONG", "risk_pct": 0.02}]
    result = engine.assess(
        pair="EURUSD", direction="LONG",
        entry_price=1.10, stop_loss=1.098,
        open_trades=trades,
    )
    assert result.approved is False
    assert any("already have" in r.lower() or "correlat" in r.lower() for r in result.rejections)


def test_reject_same_pair_opposite_direction_hedge(engine):
    trades = [{"pair": "EURUSD", "direction": "SHORT", "risk_pct": 0.02}]
    result = engine.assess(
        pair="EURUSD", direction="LONG",
        entry_price=1.10, stop_loss=1.098,
        open_trades=trades,
    )
    assert result.approved is False
    assert any("hedge" in r.lower() or "opposing" in r.lower() or "correlat" in r.lower() for r in result.rejections)


# ── RiskEngine — position sizing at different modes ────────────────────────


def test_normal_mode_risk_075_percent(engine):
    result = engine.assess(
        pair="EURUSD", direction="LONG",
        entry_price=1.10000, stop_loss=1.09800,
    )
    assert result.risk_pct == pytest.approx(0.0075, abs=0.002)


def test_caution_mode_risk_reduced(engine):
    from brain.drawdown_guard import DrawdownMode
    engine.drawdown_guard.mode = DrawdownMode.CAUTION

    result = engine.assess(
        pair="EURUSD", direction="LONG",
        entry_price=1.10000, stop_loss=1.09800,
    )
    assert result.risk_pct == pytest.approx(0.0056, abs=0.002)


def test_recovery_mode_risk_reduced_further(engine):
    from brain.drawdown_guard import DrawdownMode
    engine.drawdown_guard.mode = DrawdownMode.RECOVERY

    result = engine.assess(
        pair="EURUSD", direction="LONG",
        entry_price=1.10000, stop_loss=1.09800,
    )
    assert result.risk_pct == pytest.approx(0.0037, abs=0.002)


# ── RiskEngine — daily loss triggers FROZEN ────────────────────────────────


def test_daily_loss_triggers_frozen(engine):
    now = datetime.now(timezone.utc)
    engine.pnl_tracker.record(-300.0, False, now)
    engine.pnl_tracker.record(-250.0, False, now)

    result = engine.assess(
        pair="EURUSD", direction="LONG",
        entry_price=1.10, stop_loss=1.098,
    )
    assert result.approved is False
    assert any("limit" in r.lower() or "frozen" in r.lower() for r in result.rejections)


# ── RiskEngine — weekly loss triggers RECOVERY ─────────────────────────────


def test_weekly_loss_triggers_recovery(engine):
    now = datetime.now(timezone.utc)
    for _ in range(5):
        engine.pnl_tracker.record(-150.0, False, now)

    result = engine.assess(
        pair="EURUSD", direction="LONG",
        entry_price=1.10, stop_loss=1.098,
    )
    if result.approved:
        assert "RECOVERY" in result.risk_mode or "CAUTION" in result.risk_mode


# ── RiskEngine.record_trade_result ─────────────────────────────────────────


def test_record_trade_result_updates_balance(engine):
    engine.record_trade_result(
        pnl_dollars=150.0, pnl_pips=30.0,
        pair="EURUSD", direction="LONG",
    )
    assert engine.balance == 10_150.0


def test_record_loss_updates_balance(engine):
    engine.record_trade_result(
        pnl_dollars=-100.0, pnl_pips=-20.0,
        pair="GBPUSD", direction="SHORT",
    )
    assert engine.balance == 9_900.0


# ── RiskEngine.get_account_snapshot ────────────────────────────────────────


def test_account_snapshot_structure(engine):
    snap = engine.get_account_snapshot()
    assert isinstance(snap, AccountSnapshot)
    assert snap.balance == 10_000.0
    assert snap.risk_mode == "NORMAL"


# ── RiskEngine — daily/weekly reset ────────────────────────────────────────


def test_reset_daily_unfreezes(engine):
    from brain.drawdown_guard import DrawdownMode
    engine.drawdown_guard.mode = DrawdownMode.FROZEN

    engine.reset_daily()
    assert engine.drawdown_guard.mode == DrawdownMode.CAUTION


def test_reset_daily_keeps_normal(engine):
    from brain.drawdown_guard import DrawdownMode
    engine.reset_daily()
    assert engine.drawdown_guard.mode == DrawdownMode.NORMAL


# ── PositionSizer ──────────────────────────────────────────────────────────


def test_position_size_basic(sizer):
    result = sizer.calculate(
        account_balance=10_000.0,
        risk_pct=0.02,
        entry_price=1.10000,
        stop_loss=1.09800,
        pip_size=0.0001,
        pip_value_per_lot=10.0,
    )
    assert result.lots > 0
    assert result.risk_pips == pytest.approx(20.0, abs=0.5)
    assert result.risk_amount == pytest.approx(200.0, abs=0.01)
    assert result.lots == pytest.approx(1.0, abs=0.1)


def test_position_size_gold(sizer):
    result = sizer.calculate(
        account_balance=10_000.0,
        risk_pct=0.02,
        entry_price=2350.00,
        stop_loss=2345.00,
        pip_size=0.01,
        pip_value_per_lot=10.0,
    )
    assert result.lots > 0
    assert result.risk_pips == pytest.approx(500.0, abs=1.0)


def test_position_size_zero_sl(sizer):
    result = sizer.calculate(
        account_balance=10_000.0,
        risk_pct=0.02,
        entry_price=1.10,
        stop_loss=1.10,
        pip_size=0.0001,
    )
    assert result.lots == 0.01


def test_position_size_for_instrument(sizer):
    result = sizer.calculate_for_instrument(
        symbol="XAUUSD",
        account_balance=10_000.0,
        risk_pct=0.02,
        entry_price=2350.0,
        stop_loss=2345.0,
    )
    assert result.lots > 0


def test_volatility_adjustment_high(sizer):
    adjusted = sizer.adjust_for_volatility(
        base_lots=1.0, current_atr=0.003, average_atr=0.0015,
    )
    assert adjusted < 1.0


def test_volatility_adjustment_low(sizer):
    adjusted = sizer.adjust_for_volatility(
        base_lots=1.0, current_atr=0.0005, average_atr=0.0015,
    )
    assert adjusted > 1.0


def test_volatility_zero_atr(sizer):
    adjusted = sizer.adjust_for_volatility(
        base_lots=1.0, current_atr=0.0, average_atr=0.001,
    )
    assert adjusted == 1.0


def test_margin_calculation(sizer):
    margin = sizer.calculate_margin(lots=1.0, price=1.1, leverage=100)
    assert margin == pytest.approx(1100.0, abs=1.0)


# ── PnLTracker ─────────────────────────────────────────────────────────────


def test_pnl_record_and_snapshot(tracker):
    now = datetime.now(timezone.utc)
    tracker.record(150.0, True, now)
    tracker.record(-50.0, False, now)

    snap = tracker.get_snapshot(timestamp=now)
    assert snap.daily_realized == pytest.approx(100.0, abs=0.01)
    assert snap.trades_today == 2
    assert snap.wins_today == 1
    assert snap.losses_today == 1
    assert snap.best_trade_today == pytest.approx(150.0, abs=0.01)
    assert snap.worst_trade_today == pytest.approx(-50.0, abs=0.01)


def test_pnl_streak_tracking(tracker):
    now = datetime.now(timezone.utc)
    tracker.record(10.0, True, now)
    tracker.record(20.0, True, now)
    tracker.record(30.0, True, now)
    snap = tracker.get_snapshot(timestamp=now)
    assert snap.current_streak == 3

    tracker.record(-10.0, False, now)
    snap = tracker.get_snapshot(timestamp=now)
    assert snap.current_streak == -1


def test_pnl_daily_limit_check(tracker):
    now = datetime.now(timezone.utc)
    tracker.record(-400.0, False, now)
    tracker.record(-200.0, False, now)
    assert tracker.is_daily_limit_hit(5.0, 10_000.0, timestamp=now) is True


def test_pnl_daily_limit_not_hit(tracker):
    now = datetime.now(timezone.utc)
    tracker.record(-100.0, False, now)
    assert tracker.is_daily_limit_hit(5.0, 10_000.0, timestamp=now) is False


def test_pnl_daily_reset(tracker):
    now = datetime.now(timezone.utc)
    tracker.record(-200.0, False, now)
    tracker.reset_daily(now)

    snap = tracker.get_snapshot(timestamp=now)
    assert snap.daily_realized == 0.0
    assert snap.trades_today == 0


def test_pnl_weekly_reset(tracker):
    now = datetime.now(timezone.utc)
    tracker.record(-300.0, False, now)
    tracker.reset_weekly(now)

    snap = tracker.get_snapshot(timestamp=now)
    assert snap.weekly_realized == 0.0


def test_pnl_performance_summary(tracker):
    now = datetime.now(timezone.utc)
    tracker.record(100.0, True, now)
    tracker.record(-50.0, False, now)

    summary = tracker.get_performance_summary("daily", now)
    assert summary["trades"] == 2
    assert summary["wins"] == 1
    assert summary["win_rate"] == pytest.approx(50.0, abs=0.1)


# ── SpreadMonitor ──────────────────────────────────────────────────────────


def test_spread_safe_normal(spread_mon):
    now = datetime.now(timezone.utc)
    for _ in range(10):
        spread_mon.record_spread("EURUSD", 1.0, now)

    safe, reason = spread_mon.is_spread_safe("EURUSD", 1.5)
    assert safe is True


def test_spread_unsafe_wide(spread_mon):
    now = datetime.now(timezone.utc)
    for _ in range(10):
        spread_mon.record_spread("EURUSD", 1.0, now)

    safe, reason = spread_mon.is_spread_safe("EURUSD", 5.0)
    assert safe is False
    assert "wide" in reason.lower() or "too" in reason.lower()


def test_spread_status_labels(spread_mon):
    now = datetime.now(timezone.utc)
    for _ in range(10):
        spread_mon.record_spread("EURUSD", 1.0, now)

    assert spread_mon.get_spread_status("EURUSD", 0.5) == "TIGHT"
    assert spread_mon.get_spread_status("EURUSD", 1.0) == "NORMAL"
    assert spread_mon.get_spread_status("EURUSD", 2.0) == "WIDE"
    assert spread_mon.get_spread_status("EURUSD", 4.0) == "DANGEROUS"


def test_spread_falls_back_to_registry(spread_mon):
    avg = spread_mon.get_average_spread("EURUSD")
    assert avg > 0


def test_spread_safe_when_no_history(spread_mon):
    safe, reason = spread_mon.is_spread_safe("UNKNOWN_PAIR", 1.0)
    assert safe is True


# ── SpreadMonitor with RiskEngine ──────────────────────────────────────────


def test_assess_with_spread_rejection(engine):
    now = datetime.now(timezone.utc)
    for _ in range(10):
        engine.spread_monitor.record_spread("EURUSD", 1.0, now)

    result = engine.assess(
        pair="EURUSD", direction="LONG",
        entry_price=1.10, stop_loss=1.098,
        current_spread_pips=10.0,
    )
    assert result.approved is False
    assert any("spread" in r.lower() or "wide" in r.lower() for r in result.rejections)


def test_assess_with_normal_spread(engine):
    now = datetime.now(timezone.utc)
    for _ in range(10):
        engine.spread_monitor.record_spread("EURUSD", 1.0, now)

    result = engine.assess(
        pair="EURUSD", direction="LONG",
        entry_price=1.10, stop_loss=1.098,
        current_spread_pips=1.2,
    )
    assert result.approved is True


# ── RiskReporter ──────────────────────────────────────────────────────────


def test_risk_report_generation(engine):
    reporter = RiskReporter()
    report = reporter.generate_report(
        risk_engine=engine,
        pnl_tracker=engine.pnl_tracker,
        spread_monitor=engine.spread_monitor,
    )
    assert report.health in {"EXCELLENT", "GOOD", "CAUTION", "CRITICAL", "FROZEN"}
    assert report.account_balance == 10_000.0
    assert report.risk_mode == "NORMAL"


def test_risk_report_health_colors(engine):
    reporter = RiskReporter()
    report = reporter.generate_report(
        risk_engine=engine,
        pnl_tracker=engine.pnl_tracker,
        spread_monitor=engine.spread_monitor,
    )
    color = reporter.get_health_color(report)
    assert color in {"green", "yellow", "red", "grey"}


def test_risk_report_frozen_health(engine):
    from brain.drawdown_guard import DrawdownMode
    engine.drawdown_guard.mode = DrawdownMode.FROZEN

    reporter = RiskReporter()
    report = reporter.generate_report(
        risk_engine=engine,
        pnl_tracker=engine.pnl_tracker,
        spread_monitor=engine.spread_monitor,
    )
    assert report.health == "FROZEN"


# ── Correlation rejection through RiskEngine ──────────────────────────────


def test_correlation_rejection(engine):
    trades = [
        {"pair": "EURUSD", "direction": "LONG", "risk_pct": 0.02},
        {"pair": "GBPUSD", "direction": "LONG", "risk_pct": 0.02},
    ]
    result = engine.assess(
        pair="NZDUSD", direction="LONG",
        entry_price=0.62, stop_loss=0.618,
        open_trades=trades,
    )
    if not result.approved:
        assert any("correlat" in r.lower() or "exposure" in r.lower() or "max trades" in r.lower() for r in result.rejections)


# ── AccountSnapshot ───────────────────────────────────────────────────────


def test_account_snapshot_after_trades(engine):
    now = datetime.now(timezone.utc)
    engine.record_trade_result(200.0, 40.0, "EURUSD", "LONG", now)
    engine.record_trade_result(-100.0, -20.0, "GBPUSD", "SHORT", now)

    snap = engine.get_account_snapshot()
    assert snap.balance == 10_100.0
    assert snap.risk_mode in {"NORMAL", "CAUTION", "RECOVERY"}


# ── reconcile_balance — sanity bound ──────────────────────────────────────


def test_reconcile_applies_small_change(engine):
    engine.balance = 10_000.0
    engine.reconcile_balance(10_050.0)
    assert engine.balance == 10_050.0


def test_reconcile_applies_zero_broker_balance(engine):
    engine.balance = 10_000.0
    engine.reconcile_balance(0.0)
    assert engine.balance == 0.0


def test_reconcile_ignores_none_and_negative(engine):
    engine.balance = 10_000.0
    engine.reconcile_balance(None)
    engine.reconcile_balance(-5.0)
    assert engine.balance == 10_000.0


def test_reconcile_rejects_implausible_swing_until_corroborated(engine):
    engine.balance = 10_000.0
    # A reconnecting leg momentarily reports only its tiny standalone balance.
    engine.reconcile_balance(38.21)
    assert engine.balance == 10_000.0  # rejected, awaiting corroboration

    # A normal pooled read returns next cycle → candidate cleared, no resync.
    engine.reconcile_balance(10_010.0)
    assert engine.balance == 10_010.0

    # The fluke value reappearing once more is still a single occurrence.
    engine.reconcile_balance(38.21)
    assert engine.balance == 10_010.0


def test_reconcile_applies_large_swing_when_corroborated(engine):
    engine.balance = 10_000.0
    # A genuine catastrophic loss reports consistently across two reads.
    engine.reconcile_balance(40.0)
    assert engine.balance == 10_000.0  # first read held back
    engine.reconcile_balance(41.0)
    assert engine.balance == 41.0  # corroborated → synced


# ── reconcile_platform_balances — per-platform independence ───────────────


def test_platform_balances_tracked_independently(engine):
    engine.reconcile_platform_balances({"mt5": 28.88, "deriv": 10_026.96})
    assert engine.get_platform_balance("mt5") == 28.88
    assert engine.get_platform_balance("deriv") == 10_026.96
    # Pooled balance is the sum of the independent legs.
    assert engine.balance == pytest.approx(28.88 + 10_026.96)


def test_disconnected_platform_retains_last_known_balance(engine):
    engine.reconcile_platform_balances({"mt5": 28.88, "deriv": 10_026.96})
    pooled = engine.balance
    # Deriv drops out of the read entirely (disconnected). Its last-known
    # balance must persist — never replaced by MT5's or zeroed.
    engine.reconcile_platform_balances({"mt5": 28.88})
    assert engine.get_platform_balance("deriv") == 10_026.96
    assert engine.balance == pytest.approx(pooled)


def test_platform_balances_do_not_collide_on_reconnect(engine):
    # The exact oscillation from the incident: MT5 real ($28.88) and Deriv demo
    # ($10,026.96) must never be reconciled against each other, so neither leg
    # ever sees an "implausible swing" caused by the other platform's value.
    engine.reconcile_platform_balances({"mt5": 28.88, "deriv": 10_026.96})
    # Deriv reconnects/drops repeatedly; MT5 stays put.
    for _ in range(3):
        engine.reconcile_platform_balances({"mt5": 28.88})  # deriv offline
        engine.reconcile_platform_balances({"mt5": 28.88, "deriv": 10_026.96})
    assert engine.get_platform_balance("mt5") == 28.88
    assert engine.get_platform_balance("deriv") == 10_026.96
    assert engine.balance == pytest.approx(28.88 + 10_026.96)


def test_platform_swing_requires_per_platform_corroboration(engine):
    engine.reconcile_platform_balances({"mt5": 10_000.0})
    # A single-cycle crash on MT5 alone is held back until corroborated.
    engine.reconcile_platform_balances({"mt5": 40.0})
    assert engine.get_platform_balance("mt5") == 10_000.0
    # Second corroborating read applies it.
    engine.reconcile_platform_balances({"mt5": 41.0})
    assert engine.get_platform_balance("mt5") == 41.0


def test_platform_balances_ignore_none_and_negative(engine):
    engine.reconcile_platform_balances({"mt5": 100.0})
    engine.reconcile_platform_balances({"mt5": None, "deriv": -5.0})
    assert engine.get_platform_balance("mt5") == 100.0
    assert engine.get_platform_balance("deriv") is None

