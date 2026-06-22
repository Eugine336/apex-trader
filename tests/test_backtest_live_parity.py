"""Backtest ↔ live parity tests.

Verifies that ``brain.backtest_engine`` adjudicates, manages and sizes trades
through the SAME engines the live event-driven plane uses, and that
``legacy_mode`` preserves the original geometry-only behaviour.
"""

from datetime import datetime, timezone
from types import SimpleNamespace

import pandas as pd
import pytest

from brain.backtest_engine import BacktestEngine, BacktestSetup
from decision.actions import Action, ManagementDecision
from decision.situation import SituationAssessment
from entry.models import EntryZone, ZoneType


# ── Construction ─────────────────────────────────────────────────────────


def test_default_mode_wires_live_engines():
    eng = BacktestEngine()
    assert eng.legacy_mode is False
    assert eng.situation_engine is not None
    assert eng.decision_engine is not None
    assert eng.risk_governor is not None
    assert eng.position_sizer is not None
    assert eng.portfolio is not None
    assert eng.entry_gate is not None


def test_legacy_mode_leaves_engines_unwired():
    eng = BacktestEngine(legacy_mode=True)
    assert eng.legacy_mode is True
    assert eng.decision_engine is None
    assert eng.portfolio is None
    assert eng.entry_gate is None


# ── Zone-geometry score selection (Phase 4) ──────────────────────────────


def _zone(direction, conviction, zt=ZoneType.FVG_OB_OVERLAP):
    now = datetime.now(timezone.utc)
    return EntryZone(
        symbol="EURUSD", direction=direction, zone_type=zt,
        top=1.105, bottom=1.104, midpoint=1.1045, invalidation_level=1.103,
        conviction=conviction, created_at=now, expires_at=now, timeframe="M5",
    )


def test_select_zone_picks_highest_conviction_matching_direction():
    eng = BacktestEngine()
    wm = SimpleNamespace(entry_zones_list=lambda: [
        _zone("LONG", 70),
        _zone("LONG", 100),
        _zone("SHORT", 100),
    ])
    zone = eng._select_zone(wm, "LONG")
    assert zone is not None
    assert zone.conviction == 100
    assert zone.direction == "LONG"


def test_select_zone_returns_none_when_no_direction_match():
    eng = BacktestEngine()
    wm = SimpleNamespace(entry_zones_list=lambda: [_zone("SHORT", 100)])
    assert eng._select_zone(wm, "LONG") is None


# ── Position sizing parity (Phase 3) ─────────────────────────────────────


def test_size_trade_returns_real_lots_via_portfolio():
    eng = BacktestEngine()
    signal = SimpleNamespace(entry_price=1.1000, stop_loss=1.0950, tp2=1.1150)
    sized = eng._size_trade("EURUSD", "LONG", signal, de_size_mult=1.0,
                            conviction=0.8, balance=10_000.0)
    assert sized is not None
    lots, max_loss, risk_amount = sized
    assert lots > 0
    assert max_loss > 0


# ── Fast-opposition sign correction (Phase 2) ────────────────────────────


def test_fast_opposition_increments_on_real_opposition():
    eng = BacktestEngine()
    trade = {"fast_opp": 0}
    # tf_alignment is direction-normalized: negative = opposes the open trade.
    sa = SituationAssessment(tf_alignment=-0.4, momentum=0.0)
    eng._update_fast_opposition(trade, sa)
    assert trade["fast_opp"] == 1


def test_fast_opposition_resets_when_supported_for_short():
    """A short with HTF *support* (direction-normalized +) must NOT increment —
    the bug the live short-side sign inversion caused."""
    eng = BacktestEngine()
    trade = {"fast_opp": 5}
    sa = SituationAssessment(tf_alignment=0.4, momentum=0.2)
    eng._update_fast_opposition(trade, sa)
    assert trade["fast_opp"] == 0


def test_fast_opposition_increments_on_momentum_opposition():
    eng = BacktestEngine()
    trade = {"fast_opp": 2}
    sa = SituationAssessment(tf_alignment=0.0, momentum=-0.5)
    eng._update_fast_opposition(trade, sa)
    assert trade["fast_opp"] == 3


# ── TradeContext build: scan_direction from live bias, not self ──────────


def test_trade_context_scan_direction_from_live_bias():
    eng = BacktestEngine()
    setup = BacktestSetup(direction="LONG", entry_price=1.10, stop_loss=1.095,
                          tp1=1.105, tp2=1.115, score=100, zone_type="FVG_OB_OVERLAP")
    trade = {
        "setup": setup, "symbol": "EURUSD", "order_id": "bt-1",
        "entry_time": datetime(2024, 1, 1, tzinfo=timezone.utc),
        "entry_price": 1.10, "stop_loss": 1.095, "tp1": 1.105, "tp2": 1.115,
        "risk": 0.0005, "tp1_hit": False, "at_breakeven": False,
        "realized_r": 0.0, "lots": 0.05, "fast_opp": 0, "entry_type": "FVG_OB_OVERLAP",
    }
    candle = pd.Series({"time": pd.Timestamp("2024-01-01 01:00", tz="UTC"),
                        "open": 1.10, "high": 1.101, "low": 1.099, "close": 1.1005})
    wm = SimpleNamespace(
        structure_by_tf=lambda: {},
        bias_dict=lambda: {"direction": "SHORT"},  # live bias opposes the LONG
        regime_by_tf=lambda: {},
    )
    m1 = pd.DataFrame({
        "time": pd.date_range("2024-01-01", periods=6, freq="min", tz="UTC"),
        "open": [1.10] * 6, "high": [1.101] * 6, "low": [1.099] * 6,
        "close": [1.1005] * 6, "volume": [100] * 6,
    })
    ctx = eng._build_trade_context(trade, candle, wm, {"M1": m1}, "EURUSD")
    assert ctx.direction == "BUY"
    assert ctx.scan_direction == "SHORT"  # live bias, not the trade's own LONG
    assert ctx.symbol == "EURUSD"
    assert ctx.pnl_pips == pytest.approx((1.1005 - 1.10) / eng.pip_size)


# ── Management routing: CLOSE / SECURE (Phase 2) ─────────────────────────


def _managed_engine(action, new_sl=None, *, monkeypatch):
    """A default engine with stubbed decision engines + analyze_window."""
    import brain.decision_core as dc

    eng = BacktestEngine()
    eng.situation_engine = SimpleNamespace(
        assess_open_trade=lambda ctx: SituationAssessment()
    )
    eng.decision_engine = SimpleNamespace(
        decide_management=lambda ctx, sa: ManagementDecision(
            action=action, reason="stub", new_sl=new_sl,
        )
    )
    eng.risk_governor = SimpleNamespace(review=lambda d, ctx, sa: d)

    wm = SimpleNamespace(
        structure_by_tf=lambda: {},
        bias_dict=lambda: {"direction": ""},
        regime_by_tf=lambda: {},
    )
    # Scoped patch (auto-restored) so other tests still see the real function.
    monkeypatch.setattr(dc, "analyze_window", lambda *a, **k: wm)
    return eng


def _open_trade_dict():
    setup = BacktestSetup(direction="LONG", entry_price=1.10, stop_loss=1.095,
                          tp1=1.105, tp2=1.115, score=100)
    return {
        "setup": setup, "symbol": "EURUSD", "order_id": "bt-1",
        "entry_time": datetime(2024, 1, 1, tzinfo=timezone.utc),
        "entry_price": 1.10, "stop_loss": 1.095, "tp1": 1.105, "tp2": 1.115,
        "risk": 0.005, "tp1_hit": False, "at_breakeven": False,
        "realized_r": 0.0, "lots": 0.05, "fast_opp": 0, "entry_type": "",
    }


def _slices():
    df = pd.DataFrame({
        "time": pd.date_range("2024-01-01", periods=30, freq="min", tz="UTC"),
        "open": [1.10] * 30, "high": [1.101] * 30, "low": [1.099] * 30,
        "close": [1.10] * 30, "volume": [100] * 30,
    })
    return {"H4": df, "H1": df, "M15": df, "M5": df, "M1": df}


def test_management_close_routes_to_close_event(monkeypatch):
    eng = _managed_engine(Action.CLOSE, monkeypatch=monkeypatch)
    trade = _open_trade_dict()
    candle = pd.Series({"time": pd.Timestamp("2024-01-01 01:00", tz="UTC"),
                        "open": 1.10, "high": 1.1005, "low": 1.0995, "close": 1.099})
    out = eng._run_management(trade, candle, _slices(), "EURUSD",
                              datetime(2024, 1, 1, 1, tzinfo=timezone.utc))
    assert out is not None
    assert out["outcome"] in ("WIN", "LOSS", "BREAKEVEN")
    assert "exit_reason" in out


def test_management_breakeven_moves_stop_to_entry(monkeypatch):
    eng = _managed_engine(Action.MOVE_TO_BREAKEVEN, monkeypatch=monkeypatch)
    trade = _open_trade_dict()
    candle = pd.Series({"time": pd.Timestamp("2024-01-01 01:00", tz="UTC"),
                        "open": 1.10, "high": 1.1005, "low": 1.0998, "close": 1.1003})
    out = eng._run_management(trade, candle, _slices(), "EURUSD",
                              datetime(2024, 1, 1, 1, tzinfo=timezone.utc))
    assert out is None  # not a close
    assert trade["stop_loss"] == pytest.approx(trade["entry_price"])
    assert trade["at_breakeven"] is True


def test_management_hold_is_noop(monkeypatch):
    eng = _managed_engine(Action.HOLD, monkeypatch=monkeypatch)
    trade = _open_trade_dict()
    original_sl = trade["stop_loss"]
    candle = pd.Series({"time": pd.Timestamp("2024-01-01 01:00", tz="UTC"),
                        "open": 1.10, "high": 1.1005, "low": 1.0998, "close": 1.1003})
    out = eng._run_management(trade, candle, _slices(), "EURUSD",
                              datetime(2024, 1, 1, 1, tzinfo=timezone.utc))
    assert out is None
    assert trade["stop_loss"] == original_sl


# ── Equity accounting (Phase 3) ──────────────────────────────────────────


def test_apply_pnl_lot_based_in_live_mode():
    eng = BacktestEngine()
    trade = {"lots": 0.10, "max_loss": 200.0, "risk_amount": 200.0}
    new_balance, commission = eng._apply_pnl(10_000.0, 2.0, trade)
    # +2R on a $200 risk = +$400 gross, minus commission per lot.
    assert commission == pytest.approx(eng.commission_per_lot * 0.10)
    assert new_balance == pytest.approx(10_000.0 + 400.0 - commission)


def test_apply_pnl_legacy_is_multiplicative():
    eng = BacktestEngine(legacy_mode=True)
    trade = {"lots": 0.10, "max_loss": 200.0}
    new_balance, commission = eng._apply_pnl(10_000.0, 1.0, trade)
    assert commission == eng.commission_per_lot
    # legacy compounds by pnl_r * risk_per_trade (minus commission fraction).
    assert new_balance < 10_000.0 * (1.0 + eng.risk_per_trade)
    assert new_balance > 10_000.0
