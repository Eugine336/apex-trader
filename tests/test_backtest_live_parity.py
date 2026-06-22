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


def test_trade_context_scan_score_is_opposing_not_own():
    """Bug #2 — scan_score must be the OPPOSING (scan-direction) zone
    conviction, never the trade's own-direction zone. A LONG trade with a
    SHORT bias must report the SHORT zone's conviction as scan_score, while
    score_history still tracks the trade's own LONG zone."""
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
        bias_dict=lambda: {"direction": "SHORT", "score": 80},
        regime_by_tf=lambda: {},
        votes_list=lambda: [],
        candidates_list=lambda: [],
        entry_zones_list=lambda: [_zone("LONG", 100), _zone("SHORT", 90)],
    )
    m1 = pd.DataFrame({
        "time": pd.date_range("2024-01-01", periods=6, freq="min", tz="UTC"),
        "open": [1.10] * 6, "high": [1.101] * 6, "low": [1.099] * 6,
        "close": [1.1005] * 6, "volume": [100] * 6,
    })
    ctx = eng._build_trade_context(trade, candle, wm, {"M1": m1}, "EURUSD")
    # scan_score = opposing SHORT zone (90), NOT the own LONG zone (100).
    assert ctx.scan_score == 90
    # score_history still tracks the trade's own LONG zone conviction (100).
    assert trade["score_history"][-1] == 100


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


# ── Management context completeness: live-parity fields (audit) ──────────


def _struct(trend, conf, event="NONE", swing_high=None, swing_low=None):
    return SimpleNamespace(
        trend=SimpleNamespace(value=trend),
        confidence=conf,
        last_event=SimpleNamespace(value=event),
        swing_high=swing_high,
        swing_low=swing_low,
    )


def _rich_wm():
    """A WorldModel stub exposing the full surface live management reads."""
    return SimpleNamespace(
        structure_by_tf=lambda: {
            "D1": _struct("BULLISH", 0.8),
            "H4": _struct("BULLISH", 0.7),
            "H1": _struct("BEARISH", 0.6, "CHOCH_BEARISH"),
            "M5": _struct("BEARISH", 0.5, "BOS_BEARISH"),
        },
        bias_dict=lambda: {"direction": "SHORT"},
        regime_by_tf=lambda: {"H1": "TREND"},
        votes_list=lambda: [
            SimpleNamespace(module="structure", direction="SHORT", confidence=0.8, weight=1.0),
            SimpleNamespace(module="momentum", direction="SHORT", confidence=0.6, weight=1.0),
        ],
        candidates_list=lambda: [
            SimpleNamespace(direction="LONG", confidence=0.4, coherence=0.5),
        ],
        # Shared quality layer carries OQ/EQ directly on the WorldModel now
        # (read identically by live + backtest management).
        opportunity_quality=4.0,
        entry_quality_long=6.0,
        entry_quality_short=3.0,
        entry_zones_list=lambda: [
            SimpleNamespace(direction="LONG", conviction=90),
        ],
    )


def _trade_with_entry_quality():
    setup = BacktestSetup(direction="LONG", entry_price=1.10, stop_loss=1.095,
                          tp1=1.105, tp2=1.115, score=100, zone_type="FVG_OB_OVERLAP",
                          entry_oq=8.0, entry_eq=7.0, pip_value_per_lot=10.0)
    return {
        "setup": setup, "symbol": "EURUSD", "order_id": "bt-1",
        "entry_time": datetime(2024, 1, 1, tzinfo=timezone.utc),
        "entry_price": 1.10, "stop_loss": 1.095, "tp1": 1.105, "tp2": 1.115,
        "risk": 0.0005, "tp1_hit": False, "at_breakeven": False,
        "realized_r": 0.0, "lots": 0.10, "fast_opp": 0, "entry_type": "FVG_OB_OVERLAP",
        "score_history": [], "entry_oq": 8.0, "entry_eq": 7.0,
        "partial_closed": False, "trailing": False, "remaining_fraction": 1.0,
    }


def _h1_slice():
    return pd.DataFrame({
        "time": pd.date_range("2024-01-01", periods=3, freq="h", tz="UTC"),
        "open": [1.101, 1.101, 1.101], "high": [1.102, 1.102, 1.102],
        "low": [1.099, 1.099, 1.099], "close": [1.0995, 1.0995, 1.0995],
        "volume": [100, 100, 100],
    })


def test_trade_context_carries_m5_structure():
    eng = BacktestEngine()
    trade = _trade_with_entry_quality()
    candle = pd.Series({"time": pd.Timestamp("2024-01-01 01:00", tz="UTC"),
                        "open": 1.10, "high": 1.101, "low": 1.099, "close": 1.0995})
    ctx = eng._build_trade_context(trade, candle, _rich_wm(),
                                   {"M1": _h1_slice(), "H1": _h1_slice()}, "EURUSD")
    assert ctx.m5_trend == "BEARISH"
    assert ctx.m5_confidence == pytest.approx(0.5)
    assert ctx.m5_event == "BOS_BEARISH"


def test_trade_context_carries_consensus_panel():
    eng = BacktestEngine()
    trade = _trade_with_entry_quality()
    candle = pd.Series({"time": pd.Timestamp("2024-01-01 01:00", tz="UTC"),
                        "open": 1.10, "high": 1.101, "low": 1.099, "close": 1.0995})
    ctx = eng._build_trade_context(trade, candle, _rich_wm(),
                                   {"M1": _h1_slice(), "H1": _h1_slice()}, "EURUSD")
    assert len(ctx.consensus_votes) == 2
    assert {v.module for v in ctx.consensus_votes} == {"structure", "momentum"}


def test_trade_context_computes_oq_decay_and_dollars():
    eng = BacktestEngine()
    trade = _trade_with_entry_quality()
    candle = pd.Series({"time": pd.Timestamp("2024-01-01 01:00", tz="UTC"),
                        "open": 1.10, "high": 1.101, "low": 1.099, "close": 1.1005})
    ctx = eng._build_trade_context(trade, candle, _rich_wm(),
                                   {"M1": _h1_slice(), "H1": _h1_slice()}, "EURUSD")
    # live_oq = wm.opportunity_quality = 4.0; entry_oq = 8.0 → decay = +4.0
    assert ctx.live_oq == pytest.approx(4.0)
    assert ctx.oq_decay == pytest.approx(4.0)
    # pnl_dollars = pnl_pips × pip_value × lots × open fraction (all > 0 here)
    assert ctx.pnl_dollars > 0


def test_trade_context_appends_score_history():
    eng = BacktestEngine()
    trade = _trade_with_entry_quality()
    candle = pd.Series({"time": pd.Timestamp("2024-01-01 01:00", tz="UTC"),
                        "open": 1.10, "high": 1.101, "low": 1.099, "close": 1.0995})
    wm = _rich_wm()
    eng._build_trade_context(trade, candle, wm, {"M1": _h1_slice(), "H1": _h1_slice()}, "EURUSD")
    eng._build_trade_context(trade, candle, wm, {"M1": _h1_slice(), "H1": _h1_slice()}, "EURUSD")
    # Matching-direction zone conviction (90) appended each cycle.
    assert trade["score_history"] == [90, 90]


def test_trade_context_carries_h1_candle_context():
    eng = BacktestEngine()
    trade = _trade_with_entry_quality()  # LONG
    candle = pd.Series({"time": pd.Timestamp("2024-01-01 01:00", tz="UTC"),
                        "open": 1.10, "high": 1.101, "low": 1.099, "close": 1.0995})
    ctx = eng._build_trade_context(trade, candle, _rich_wm(),
                                   {"M1": _h1_slice(), "H1": _h1_slice()}, "EURUSD")
    # H1 last candle closes below open → bearish (opposes the LONG).
    assert ctx.h1_last_candle_bearish is True


# ── Tier 1–3 data-path wiring (swing levels, session_name, entry M1/regime/
#    spread/session, micro-confirmation) ───────────────────────────────────


def _rich_wm_with_swings():
    wm = _rich_wm()
    wm.structure_by_tf = lambda: {
        "D1": _struct("BULLISH", 0.8, swing_high=1.20, swing_low=1.05),
        "H4": _struct("BULLISH", 0.7, swing_high=1.18, swing_low=1.06),
        "H1": _struct("BEARISH", 0.6, "CHOCH_BEARISH", swing_high=1.15, swing_low=1.08),
        "M5": _struct("BEARISH", 0.5, "BOS_BEARISH"),
    }
    return wm


def test_trade_context_carries_swing_levels():
    eng = BacktestEngine()
    trade = _trade_with_entry_quality()
    candle = pd.Series({"time": pd.Timestamp("2024-01-01 01:00", tz="UTC"),
                        "open": 1.10, "high": 1.101, "low": 1.099, "close": 1.0995})
    ctx = eng._build_trade_context(trade, candle, _rich_wm_with_swings(),
                                   {"M1": _h1_slice(), "H1": _h1_slice()}, "EURUSD")
    assert ctx.d1_swing_high == 1.20 and ctx.d1_swing_low == 1.05
    assert ctx.h4_swing_high == 1.18 and ctx.h4_swing_low == 1.06
    assert ctx.h1_swing_high == 1.15 and ctx.h1_swing_low == 1.08


def test_trade_context_carries_session_name():
    eng = BacktestEngine()
    trade = _trade_with_entry_quality()
    candle = pd.Series({"time": pd.Timestamp("2024-01-01 13:00", tz="UTC"),
                        "open": 1.10, "high": 1.101, "low": 1.099, "close": 1.0995})
    ctx = eng._build_trade_context(trade, candle, _rich_wm(),
                                   {"M1": _h1_slice(), "H1": _h1_slice()}, "EURUSD")
    # session_name is now passed through (was dropped before) — a real label,
    # not the "UNKNOWN" default.
    assert isinstance(ctx.session_name, str) and ctx.session_name != "UNKNOWN"


def _entry_signal():
    return SimpleNamespace(
        entry_price=1.10, stop_loss=1.095, tp1=1.105, tp2=1.115, confluences=[],
    )


def test_entry_context_populates_regime_spread_session():
    eng = BacktestEngine()
    ctx = eng._build_entry_context(
        "EURUSD", "LONG", _entry_signal(), 100, _zone("LONG", 100),
        {"M1": _h1_slice()}, _rich_wm(), 10_000.0, [],
        datetime(2024, 1, 1, 13, 0, tzinfo=timezone.utc),
    )
    # Regime feeds regime-specific DecisionWeights (was "" → base weights only).
    assert ctx.regime == "TREND"
    # Spread dimension now engages (typical_spread > 0), using the backtest cfg.
    assert ctx.typical_spread == pytest.approx(eng.config.backtest.default_spread_pips)
    assert ctx.current_spread == ctx.typical_spread
    # Session state is wired from the candle timestamp.
    assert isinstance(ctx.session_tradeable, bool)
    # M1 evidence flows from the live micro reader (not a constant default).
    assert ctx.m1_trend in ("BULLISH", "BEARISH", "RANGING", "UNKNOWN")
    assert isinstance(ctx.m1_aligned_count, int)


def test_struct_swings_helper():
    from brain.backtest_engine import _struct_swings
    structure = {"H4": _struct("BULLISH", 0.7, swing_high=1.18, swing_low=1.06)}
    assert _struct_swings(structure, "H4") == (1.18, 1.06)
    assert _struct_swings(structure, "D1") == (None, None)


def test_micro_confirmation_from_event_helper():
    from brain.backtest_engine import _micro_confirmation_from_event
    assert _micro_confirmation_from_event("BOS_BULLISH", "LONG") == ("choch_bos", "MARKET")
    assert _micro_confirmation_from_event("CHOCH_BEARISH", "SHORT") == ("choch_bos", "MARKET")
    # Opposing / absent events leave the PENDING default unchanged.
    assert _micro_confirmation_from_event("BOS_BEARISH", "LONG") == ("", "PENDING")
    assert _micro_confirmation_from_event("NONE", "LONG") == ("", "PENDING")


def test_management_partial_close_banks_fraction(monkeypatch):
    eng = _managed_engine(Action.PARTIAL_CLOSE, monkeypatch=monkeypatch)
    eng.decision_engine = SimpleNamespace(
        decide_management=lambda ctx, sa: ManagementDecision(
            action=Action.PARTIAL_CLOSE, reason="stub",
        )
    )
    trade = _open_trade_dict()
    trade["remaining_fraction"] = 1.0
    trade["realized_r"] = 0.0
    # LONG in profit: close above entry.
    candle = pd.Series({"time": pd.Timestamp("2024-01-01 01:00", tz="UTC"),
                        "open": 1.10, "high": 1.1100, "low": 1.0999, "close": 1.1050})
    out = eng._run_management(trade, candle, _slices(), "EURUSD",
                              datetime(2024, 1, 1, 1, tzinfo=timezone.utc))
    assert out is None  # partial close is not a full exit
    assert trade["partial_closed"] is True
    assert trade["remaining_fraction"] == pytest.approx(0.5)
    assert trade["realized_r"] > 0  # banked some profit


def test_management_scale_in_is_safe_noop(monkeypatch):
    eng = _managed_engine(Action.SCALE_IN, monkeypatch=monkeypatch)
    eng.decision_engine = SimpleNamespace(
        decide_management=lambda ctx, sa: ManagementDecision(
            action=Action.SCALE_IN, reason="stub",
        )
    )
    trade = _open_trade_dict()
    original_sl = trade["stop_loss"]
    candle = pd.Series({"time": pd.Timestamp("2024-01-01 01:00", tz="UTC"),
                        "open": 1.10, "high": 1.1005, "low": 1.0998, "close": 1.1003})
    out = eng._run_management(trade, candle, _slices(), "EURUSD",
                              datetime(2024, 1, 1, 1, tzinfo=timezone.utc))
    assert out is None  # not modelled, base position held
    assert trade["stop_loss"] == original_sl

