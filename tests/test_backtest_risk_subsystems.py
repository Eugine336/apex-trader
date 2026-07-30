"""Backtest ↔ live parity — stateful risk / compliance subsystems (Phase 1).

Verifies that ``brain.backtest_engine`` threads the SAME stateful gates the live
event-driven plane enforces — the ComplianceDivision permit layer, the
PortfolioRisk state machine, the DrawdownGuard risk cap, the per-account risk
silo and the stateful ThesisEngine — and that ``legacy_mode`` is completely
unaffected (the subsystems stay ``None`` and none of the new gates run).
"""

from datetime import datetime, timezone

import pandas as pd

from brain.backtest_engine import BacktestEngine, BacktestSetup


# ── Construction ─────────────────────────────────────────────────────────


def test_non_legacy_wires_stateful_subsystems():
    eng = BacktestEngine()
    assert eng.legacy_mode is False
    assert eng.compliance is not None
    assert eng.portfolio_risk_sm is not None
    assert eng.drawdown_guard is not None
    assert eng.account_risk is not None
    assert eng.thesis_engine is not None


def test_legacy_mode_leaves_subsystems_none():
    eng = BacktestEngine(legacy_mode=True)
    assert eng.legacy_mode is True
    assert eng.compliance is None
    assert eng.portfolio_risk_sm is None
    assert eng.drawdown_guard is None
    assert eng.account_risk is None
    assert eng.thesis_engine is None


# ── Compliance permit (daily-loss halt) ──────────────────────────────────


def test_compliance_permits_fresh_account():
    eng = BacktestEngine(starting_balance=200.0)
    assert eng._compliance_permits("EURUSD", "LONG", 200.0) is True


def test_compliance_blocks_after_daily_loss_halt():
    eng = BacktestEngine(starting_balance=200.0)
    # A realised loss beyond the per-account daily-loss cap trips the halt, so
    # the single authoritative permit layer must REJECT the next entry.
    eng.account_risk.update_balance(eng._bt_account_key, 200.0)
    eng.account_risk.register_realized(eng._bt_account_key, -20.0)  # -10% ≫ cap
    assert eng.account_risk.daily_loss_halted(eng._bt_account_key) is True
    assert eng._compliance_permits("EURUSD", "LONG", 200.0) is False


# ── DrawdownGuard risk cap ────────────────────────────────────────────────


def test_drawdown_guard_normal_cap_is_no_op_at_base_risk():
    # The guard is anchored to the backtest's base risk, so NORMAL mode never
    # caps below the intended per-trade risk (it only reduces during drawdown).
    eng = BacktestEngine(risk_per_trade=0.02)
    status = eng.drawdown_guard.get_status()
    assert status.current_risk_pct == 0.02


# ── PortfolioRisk state machine + EMERGENCY force-close ───────────────────


def _open_trade(lots: float = 1.0):
    setup = BacktestSetup(
        direction="LONG", entry_price=1.1000, stop_loss=1.0900,
        tp1=1.1100, tp2=1.1200, score=90,
    )
    return {
        "setup": setup, "symbol": "EURUSD", "order_id": "t1",
        "entry_price": 1.1000, "stop_loss": 1.0900, "lots": lots,
        "at_breakeven": False, "tp1_hit": False, "partial_closed": False,
        "remaining_fraction": 1.0, "realized_r": 0.0, "risk": 0.0100,
        "entry_time": pd.Timestamp("2024-01-01").to_pydatetime(),
    }


def test_extreme_heat_escalates_emergency_and_force_closes():
    from risk.portfolio_risk_state import PortfolioRiskState

    eng = BacktestEngine(starting_balance=200.0)
    trade = _open_trade(lots=5.0)  # extreme capital-at-risk on a tiny account
    now = datetime(2024, 1, 1, tzinfo=timezone.utc)
    eng._update_portfolio_risk_state(trade, 200.0, now)
    assert eng.portfolio_risk_sm.state == PortfolioRiskState.EMERGENCY

    candle = pd.Series({
        "time": pd.Timestamp("2024-01-01 01:00"), "open": 1.10,
        "high": 1.101, "low": 1.099, "close": 1.1005,
    })
    close_event = eng._maybe_emergency_close(trade, candle)
    assert close_event is not None
    assert close_event["exit_reason"] == "portfolio_heat_emergency"


def test_flat_book_keeps_portfolio_state_normal():
    from risk.portfolio_risk_state import PortfolioRiskState

    eng = BacktestEngine(starting_balance=10_000.0)
    eng._update_portfolio_risk_state(None, 10_000.0, datetime(2024, 1, 1))
    assert eng.portfolio_risk_sm.state == PortfolioRiskState.NORMAL
    assert eng._maybe_emergency_close(_open_trade(), pd.Series({
        "time": pd.Timestamp("2024-01-01 01:00"), "open": 1.10,
        "high": 1.101, "low": 1.099, "close": 1.1005,
    })) is None


# ── Stateful ThesisEngine gate ────────────────────────────────────────────


def test_thesis_gate_allows_on_cold_start():
    eng = BacktestEngine()
    # No thesis tracked yet → the gate must never block on absent evidence.
    assert eng._thesis_gate_allows("EURUSD", "LONG") is True


def test_thesis_gate_blocks_direction_disagreement():
    eng = BacktestEngine()
    # Seed a strong LONG thesis (EV well over the flat baseline).
    eng.thesis_engine.update(
        symbol="EURUSD", votes=[],
        long_probability=0.8, short_probability=0.1,
        entry_ev_long=1.0, entry_ev_short=-1.0,
    )
    assert eng._thesis_gate_allows("EURUSD", "LONG") is True
    assert eng._thesis_gate_allows("EURUSD", "SHORT") is False


# ── Reset per run (no state leak between backtests) ───────────────────────


def test_run_resets_subsystem_state():
    eng = BacktestEngine(starting_balance=200.0)
    # Trip the daily-loss halt, then start a fresh run — the reset must clear it.
    eng.account_risk.update_balance(eng._bt_account_key, 200.0)
    eng.account_risk.register_realized(eng._bt_account_key, -50.0)
    assert eng.account_risk.daily_loss_halted(eng._bt_account_key) is True

    t = pd.date_range("2024-01-01", periods=200, freq="min")
    flat = pd.DataFrame({
        "time": t, "open": 1.10, "high": 1.1002,
        "low": 1.0998, "close": 1.10,
    })
    data = {tf: flat.copy() for tf in ("M1", "M5", "M15", "H1", "H4")}
    eng.run("EURUSD", data, start_index=120, end_index=199)

    # After the run the subsystems are rebuilt fresh — the halt is cleared.
    assert eng.account_risk.daily_loss_halted(eng._bt_account_key) is False
