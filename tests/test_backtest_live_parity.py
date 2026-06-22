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


# ── Management TradeContext: full live-parity input set ───────────────────


def _trend(value, conf=0.7, event="NONE"):
    return SimpleNamespace(
        trend=SimpleNamespace(value=value),
        confidence=conf,
        last_event=SimpleNamespace(value=event),
    )


def _vote(module, direction):
    return SimpleNamespace(module=module, direction=direction, confidence=0.6,
                           weight=1.0, timeframe="M5", evidence="")


def _rich_wm(*, bias_dir="LONG", m5="BULLISH", m5_event="BOS_BULLISH",
             cand_dir="LONG", conf=0.8, coh=0.6):
    cand = SimpleNamespace(direction=cand_dir, confidence=conf, coherence=coh)
    zone = _zone("LONG", 90)
    return SimpleNamespace(
        structure_by_tf=lambda: {
            "D1": _trend("BULLISH"), "H4": _trend("BULLISH"),
            "H1": _trend("BULLISH"), "M5": _trend(m5, event=m5_event),
        },
        bias_dict=lambda: {"direction": bias_dir},
        regime_by_tf=lambda: {"H1": "TRENDING"},
        votes_list=lambda: [_vote("structure", "LONG"), _vote("momentum", "SHORT")],
        candidates_list=lambda: [cand],
        entry_zones_list=lambda: [zone],
    )


def _live_trade():
    setup = BacktestSetup(direction="LONG", entry_price=1.10, stop_loss=1.095,
                          tp1=1.105, tp2=1.115, score=100, zone_type="FVG_OB_OVERLAP")
    return {
        "setup": setup, "symbol": "EURUSD", "order_id": "bt-1",
        "entry_time": datetime(2024, 1, 1, 8, tzinfo=timezone.utc),
        "entry_price": 1.10, "stop_loss": 1.095, "tp1": 1.105, "tp2": 1.115,
        "risk": 0.005, "tp1_hit": False, "at_breakeven": False,
        "realized_r": 0.0, "lots": 0.10, "fast_opp": 0, "entry_type": "FVG_OB_OVERLAP",
        "remaining_fraction": 1.0, "partial_closed": False, "trailing": False,
        "score_history": [100], "entry_oq": 9.0, "entry_eq": 8.0,
    }


def _candle(close=1.1005):
    return pd.Series({"time": pd.Timestamp("2024-01-01 13:30", tz="UTC"),
                      "open": 1.10, "high": 1.101, "low": 1.099, "close": close})


def _m1_only():
    return {"M1": pd.DataFrame({
        "time": pd.date_range("2024-01-01", periods=6, freq="min", tz="UTC"),
        "open": [1.10] * 6, "high": [1.101] * 6, "low": [1.099] * 6,
        "close": [1.1005] * 6, "volume": [100] * 6,
    })}


def test_trade_context_carries_m5_feed():
    eng = BacktestEngine()
    ctx = eng._build_trade_context(_live_trade(), _candle(), _rich_wm(),
                                   _m1_only(), "EURUSD")
    assert ctx.m5_trend == "BULLISH"
    assert ctx.m5_event == "BOS_BULLISH"
    assert ctx.m5_confidence == pytest.approx(0.7)


def test_trade_context_carries_consensus_panel():
    eng = BacktestEngine()
    ctx = eng._build_trade_context(_live_trade(), _candle(), _rich_wm(),
                                   _m1_only(), "EURUSD")
    assert len(ctx.consensus_votes) == 2
    modules = {getattr(v, "module", "") for v in ctx.consensus_votes}
    assert modules == {"structure", "momentum"}


def test_trade_context_oq_eq_decay():
    eng = BacktestEngine()
    # entry_oq=9.0, live oq = conf(0.8)*10 = 8.0 → decay 1.0; entry_eq=8.0, eq=6.0 → 2.0
    ctx = eng._build_trade_context(_live_trade(), _candle(), _rich_wm(),
                                   _m1_only(), "EURUSD")
    assert ctx.live_oq == pytest.approx(8.0)
    assert ctx.live_eq == pytest.approx(6.0)
    assert ctx.oq_decay == pytest.approx(1.0)
    assert ctx.eq_decay == pytest.approx(2.0)


def test_trade_context_session_and_news_and_dollars():
    eng = BacktestEngine()
    trade = _live_trade()
    ctx = eng._build_trade_context(trade, _candle(close=1.1010), _rich_wm(),
                                   _m1_only(), "EURUSD")
    # No news calendar in backtest → always clear.
    assert ctx.minutes_to_high_impact_news == 999.0
    assert ctx.session_name != ""
    # pnl_dollars = pnl_pips * pip_value_per_lot * lots (10 pips * 10 * 0.10).
    assert ctx.pnl_dollars == pytest.approx(ctx.pnl_pips * eng.pip_value_per_lot * 0.10)
    assert ctx.open_trade_count == 1


def test_trade_context_score_history_grows():
    eng = BacktestEngine()
    trade = _live_trade()
    eng._build_trade_context(trade, _candle(), _rich_wm(), _m1_only(), "EURUSD")
    eng._build_trade_context(trade, _candle(), _rich_wm(), _m1_only(), "EURUSD")
    # Initial [100] + two management bars appended (zone conviction 90).
    assert trade["score_history"] == [100, 90, 90]


def test_trade_context_partial_and_trailing_flags():
    eng = BacktestEngine()
    trade = _live_trade()
    trade["partial_closed"] = True
    trade["trailing"] = True
    ctx = eng._build_trade_context(trade, _candle(), _rich_wm(), _m1_only(), "EURUSD")
    assert ctx.partial_closed is True
    assert ctx.trailing is True


# ── PARTIAL_CLOSE / SCALE_IN action handling (Problem 2) ──────────────────


def test_partial_close_banks_and_reduces_lots(monkeypatch):
    eng = _managed_engine(Action.PARTIAL_CLOSE, monkeypatch=monkeypatch)
    eng.decision_engine = SimpleNamespace(
        decide_management=lambda ctx, sa: ManagementDecision(
            action=Action.PARTIAL_CLOSE, reason="bank half", partial_ratio=0.5,
        )
    )
    trade = _live_trade()
    candle = pd.Series({"time": pd.Timestamp("2024-01-01 09:00", tz="UTC"),
                        "open": 1.10, "high": 1.1051, "low": 1.0999, "close": 1.1050})
    out = eng._run_management(trade, candle, _slices(), "EURUSD",
                              datetime(2024, 1, 1, 9, tzinfo=timezone.utc))
    assert out is None  # partial is not a full close
    assert trade["partial_closed"] is True
    assert trade["remaining_fraction"] == pytest.approx(0.5)
    assert trade["lots"] == pytest.approx(0.05)  # 0.10 * (1 - 0.5)
    # Banked +1R on half (entry 1.10, close 1.105, risk 0.005 → +1R) × 0.5.
    assert trade["realized_r"] == pytest.approx(0.5)


def test_scale_in_increases_lots_and_blends_entry(monkeypatch):
    eng = _managed_engine(Action.SCALE_IN, monkeypatch=monkeypatch)
    eng.decision_engine = SimpleNamespace(
        decide_management=lambda ctx, sa: ManagementDecision(
            action=Action.SCALE_IN, reason="add", scale_lots=0.05, confidence=0.7,
        )
    )
    trade = _live_trade()
    start_lots = trade["lots"]
    candle = pd.Series({"time": pd.Timestamp("2024-01-01 09:00", tz="UTC"),
                        "open": 1.10, "high": 1.1051, "low": 1.0999, "close": 1.1020})
    out = eng._run_management(trade, candle, _slices(), "EURUSD",
                              datetime(2024, 1, 1, 9, tzinfo=timezone.utc))
    assert out is None
    assert trade["lots"] > start_lots          # position grew
    assert trade.get("scaled_in") is True
    # Blended entry sits between the original entry and the add-on price.
    assert 1.10 < trade["entry_price"] <= 1.1020


# ── TP1 geometry models PositionWorker tick mechanics (Problem 5) ─────────


def test_tp1_partial_banks_via_remaining_fraction():
    eng = BacktestEngine()
    trade = _live_trade()
    # LONG, price reaches TP1 (1.105) — bank 50%, move to breakeven.
    candle = pd.Series({"time": pd.Timestamp("2024-01-01 09:00", tz="UTC"),
                        "open": 1.10, "high": 1.1051, "low": 1.0999, "close": 1.1050})
    out = eng._evaluate_geometry(trade, candle)
    assert out is None  # runner continues after the partial
    assert trade["tp1_hit"] is True
    assert trade["partial_closed"] is True
    assert trade["at_breakeven"] is True
    assert trade["stop_loss"] == pytest.approx(trade["entry_price"])
    assert trade["remaining_fraction"] == pytest.approx(0.5)
    assert trade["lots"] == pytest.approx(0.05)
    assert trade["realized_r"] == pytest.approx(0.5)  # +1R on TP1 distance × 0.5


# ── Sizing reads the real daily-loss budget (Problem 4) ──────────────────


def test_size_trade_passes_daily_pnl_to_portfolio(monkeypatch):
    eng = BacktestEngine()
    eng._bt_daily_pnl = -123.45
    captured = {}

    def _capture(candidate, existing, account, factors):
        captured["daily_pnl"] = account.daily_pnl
        captured["daily_cap"] = account.daily_loss_cap_pct
        captured["existing"] = existing
        return SimpleNamespace(approved=True, lots=0.05, max_loss=100.0, risk_pct=0.01)

    eng.portfolio = SimpleNamespace(evaluate=_capture)
    signal = SimpleNamespace(entry_price=1.10, stop_loss=1.095, tp2=1.115)
    eng._size_trade("EURUSD", "LONG", signal, de_size_mult=1.0,
                    conviction=0.8, balance=10_000.0)
    assert captured["daily_pnl"] == pytest.approx(-123.45)
    assert captured["daily_cap"] > 0.0  # governor cap wired, not 0.0
    assert captured["existing"] == []

