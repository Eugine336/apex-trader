"""Backtest ↔ live parity — opportunistic features (Phase 3 Feature B).

Verifies that ``brain.backtest_engine`` mirrors the live opportunistic modules so
replays reflect live behaviour: the compression detector is fed each bar and its
conviction boost shapes risk, the session context scales risk per session, the
stop-out flip opens the opposite direction on a stop-out (with its whipsaw
guards), pre-staged LIMIT fills capture wick-touch entries, and the DXY / news
risk shaping degrades gracefully when the backtest carries no such data. Each
feature is independently flag-gated and ``legacy_mode`` stays unaffected.
"""

from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from brain.backtest_engine import BacktestEngine, BacktestSetup
from brain.compression_detector import MarketState


# ── Construction / flag gating ─────────────────────────────────────────────


def test_non_legacy_wires_opportunistic_modules():
    eng = BacktestEngine()
    assert eng.compression_detector is not None
    assert eng.session_context is not None


def test_legacy_mode_leaves_opportunistic_modules_none():
    eng = BacktestEngine(legacy_mode=True)
    assert eng.compression_detector is None
    assert eng.session_context is None


def test_flags_toggle_each_module_independently():
    eng = BacktestEngine(
        backtest_compression_enabled=False,
        backtest_session_enabled=False,
    )
    assert eng.compression_detector is None
    assert eng.session_context is None


# ── Compression in the backtest ────────────────────────────────────────────


def _ohlc(n: int, base: float = 2000.0, step: float = 1.0) -> pd.DataFrame:
    times = pd.date_range("2026-01-01", periods=n, freq="5min", tz="UTC")
    close = [base + i * step for i in range(n)]
    return pd.DataFrame({
        "time": times,
        "open": close,
        "high": [c + 0.5 for c in close],
        "low": [c - 0.5 for c in close],
        "close": close,
        "volume": [100] * n,
    })


def test_compression_detector_fed_and_readable():
    eng = BacktestEngine()
    slices = {"M5": _ohlc(130), "M15": _ohlc(130, step=2.0)}
    eng._update_opportunistic_state("XAUUSD", slices)
    state = eng.get_market_state("XAUUSD")
    assert isinstance(state, MarketState)


def test_get_market_state_ranging_when_disabled():
    eng = BacktestEngine(backtest_compression_enabled=False)
    assert eng.get_market_state("XAUUSD") == MarketState.RANGING


def test_compression_boost_applied_to_risk(monkeypatch):
    eng = BacktestEngine()
    monkeypatch.setattr(
        eng.compression_detector, "get_market_state",
        lambda _s: MarketState.COMPRESSING,
    )
    prof_boost = eng._compression_risk_mult("XAUUSD")
    # Gold profile compression_conviction_boost default is 1.2.
    assert prof_boost == pytest.approx(1.2)


def test_expansion_boost_applied_to_risk(monkeypatch):
    eng = BacktestEngine()
    monkeypatch.setattr(
        eng.compression_detector, "get_market_state",
        lambda _s: MarketState.EXPANDING,
    )
    assert eng._compression_risk_mult("XAUUSD") == pytest.approx(1.5)


def test_ranging_state_is_neutral(monkeypatch):
    eng = BacktestEngine()
    monkeypatch.setattr(
        eng.compression_detector, "get_market_state",
        lambda _s: MarketState.RANGING,
    )
    assert eng._compression_risk_mult("XAUUSD") == pytest.approx(1.0)


# ── Session multipliers in the backtest ────────────────────────────────────


def _at(hour: int) -> datetime:
    return datetime(2026, 8, 3, hour, 0, tzinfo=timezone.utc)


def test_session_multiplier_asian_sizes_down():
    eng = BacktestEngine()
    # 23:00 UTC → ASIAN → Gold default 0.5×. No compression/DXY/news → mult==session.
    mult = eng._opportunistic_risk_mult("XAUUSD", "LONG", {}, _at(23))
    assert mult == pytest.approx(0.5)


def test_session_multiplier_london_sizes_up():
    eng = BacktestEngine()
    mult = eng._opportunistic_risk_mult("XAUUSD", "LONG", {}, _at(9))
    assert mult == pytest.approx(1.2)  # LONDON


def test_session_multiplier_overlap_sizes_up_most():
    eng = BacktestEngine()
    mult = eng._opportunistic_risk_mult("XAUUSD", "LONG", {}, _at(14))
    assert mult == pytest.approx(1.3)  # LONDON_NY_OVERLAP


def test_session_disabled_is_neutral():
    eng = BacktestEngine(backtest_session_enabled=False)
    assert eng._opportunistic_risk_mult("XAUUSD", "LONG", {}, _at(23)) == pytest.approx(1.0)


# ── DXY correlation (graceful when no USD-strength source) ──────────────────


def test_dxy_skips_gracefully_without_source():
    eng = BacktestEngine()
    assert eng._backtest_dxy_mult("XAUUSD", "LONG", _at(9)) == pytest.approx(1.0)


def test_dxy_penalises_opposing_long_when_usd_strengthening():
    eng = BacktestEngine()
    eng.backtest_usd_strength = lambda _now: 1.0  # USD strengthening
    # A strengthening USD opposes a LONG on a USD-quoted pair → (1 - 0.15).
    assert eng._backtest_dxy_mult("XAUUSD", "LONG", _at(9)) == pytest.approx(0.85)


def test_dxy_neutral_when_supporting():
    eng = BacktestEngine()
    eng.backtest_usd_strength = lambda _now: 1.0
    assert eng._backtest_dxy_mult("XAUUSD", "SHORT", _at(9)) == pytest.approx(1.0)


# ── News window sizing (graceful when no events) ───────────────────────────


def test_news_skips_gracefully_without_events():
    eng = BacktestEngine()
    assert eng._backtest_news_mult("XAUUSD", _at(9)) == pytest.approx(1.0)


def test_news_window_sizes_down():
    eng = BacktestEngine()
    ev = _at(12)
    eng.backtest_news_events = [ev]
    # 3 minutes before the event → inside the pre-stage window → Gold 0.5×.
    assert eng._backtest_news_mult("XAUUSD", ev - timedelta(minutes=3)) == pytest.approx(0.5)


def test_news_outside_window_is_neutral():
    eng = BacktestEngine()
    ev = _at(12)
    eng.backtest_news_events = [ev]
    assert eng._backtest_news_mult("XAUUSD", ev - timedelta(hours=2)) == pytest.approx(1.0)


# ── Pre-staged limit fill simulation ───────────────────────────────────────


def _candle(low: float, high: float) -> pd.Series:
    return pd.Series({
        "time": pd.Timestamp("2026-01-01 10:00", tz="UTC"),
        "open": (low + high) / 2, "high": high, "low": low,
        "close": (low + high) / 2,
    })


def test_prestage_fill_when_wick_touches_boundary():
    # Bar wicks down to 1998 (< boundary 2000) and closes at 2000 → the resting
    # LIMIT at the 2000 boundary would have filled AT 2000.
    fill = BacktestEngine.simulate_prestage_fill("LONG", 2000.0, _candle(1998.0, 2001.0))
    assert fill == pytest.approx(2000.0)


def test_prestage_no_fill_when_bar_never_reaches_boundary():
    fill = BacktestEngine.simulate_prestage_fill("LONG", 2000.0, _candle(2001.0, 2003.0))
    assert fill is None


def test_prestage_fill_used_in_open_trade_without_slippage():
    eng = BacktestEngine(slippage_pips=1.0, pip_size=0.01)
    setup = BacktestSetup(
        direction="LONG", entry_price=2000.0, stop_loss=1990.0,
        tp1=2015.0, tp2=2030.0, score=90, source="zone",
    )
    candle = _candle(1998.0, 2001.0)  # wick straddles the 2000 boundary
    trade = eng._open_trade(setup, _at(9), entry_candle=candle)
    # Limit fill AT the boundary — no adverse slippage.
    assert trade["entry_price"] == pytest.approx(2000.0)
    assert trade["slippage_cost"] == pytest.approx(0.0)


def test_open_trade_keeps_slippage_without_prestage_touch():
    eng = BacktestEngine(slippage_pips=1.0, pip_size=0.01)
    setup = BacktestSetup(
        direction="LONG", entry_price=2000.0, stop_loss=1990.0,
        tp1=2015.0, tp2=2030.0, score=90, source="zone",
    )
    candle = _candle(2001.0, 2003.0)  # never reaches the boundary
    trade = eng._open_trade(setup, _at(9), entry_candle=candle)
    assert trade["entry_price"] == pytest.approx(2000.0 + 1.0 * 0.01)  # market slippage
    assert trade["slippage_cost"] == pytest.approx(1.0 * 0.01)


def test_prestage_disabled_keeps_market_slippage():
    eng = BacktestEngine(slippage_pips=1.0, pip_size=0.01, backtest_prestaging_enabled=False)
    setup = BacktestSetup(
        direction="LONG", entry_price=2000.0, stop_loss=1990.0,
        tp1=2015.0, tp2=2030.0, score=90, source="zone",
    )
    trade = eng._open_trade(setup, _at(9), entry_candle=_candle(1998.0, 2001.0))
    assert trade["entry_price"] == pytest.approx(2000.0 + 1.0 * 0.01)


# ── Stop-out flip in the backtest ──────────────────────────────────────────


def _closed_long(entry=2000.0, stop=1990.0, risk=10.0, lots=0.1):
    setup = BacktestSetup(
        direction="LONG", entry_price=entry, stop_loss=stop,
        tp1=entry + 15, tp2=entry + 30, score=90, zone_type="FVG_MIDPOINT",
    )
    return {
        "setup": setup, "symbol": "XAUUSD", "entry_price": entry,
        "stop_loss": stop, "risk": risk, "lots": lots,
    }


def _m5_falling() -> pd.DataFrame:
    # Falling closes → M5 SHORT trend → confirms a LONG→SHORT flip.
    return pd.DataFrame({"close": [2000.0 - i for i in range(12)]})


def _m5_rising() -> pd.DataFrame:
    return pd.DataFrame({"close": [2000.0 + i for i in range(12)]})


def test_stopout_flip_opens_opposite_direction():
    eng = BacktestEngine()
    now = _at(9)
    flip = eng._maybe_stopout_flip(
        _closed_long(), {"outcome": "LOSS"}, {"M5": _m5_falling()}, "XAUUSD", now, 10_000.0,
    )
    assert flip is not None
    assert flip["setup"].direction == "SHORT"
    assert flip["symbol"] == "XAUUSD"
    # Flip enters near the stopped-out level with a mirrored (positive) risk.
    assert flip["entry_price"] == pytest.approx(1990.0, abs=0.5)
    assert flip["risk"] > 0


def test_stopout_flip_blocked_when_m5_opposes():
    eng = BacktestEngine()
    flip = eng._maybe_stopout_flip(
        _closed_long(), {"outcome": "LOSS"}, {"M5": _m5_rising()}, "XAUUSD", _at(9), 10_000.0,
    )
    # M5 trending LONG opposes flipping to SHORT → no flip.
    assert flip is None


def test_stopout_flip_cooldown_blocks_rapid_reflip():
    eng = BacktestEngine()
    now = _at(9)
    first = eng._maybe_stopout_flip(
        _closed_long(), {"outcome": "LOSS"}, {"M5": _m5_falling()}, "XAUUSD", now, 10_000.0,
    )
    assert first is not None
    # 5 seconds later — inside the 30s cooldown → blocked.
    second = eng._maybe_stopout_flip(
        _closed_long(), {"outcome": "LOSS"}, {"M5": _m5_falling()},
        "XAUUSD", now + timedelta(seconds=5), 10_000.0,
    )
    assert second is None


def test_stopout_flip_per_zone_cap_exhausts():
    eng = BacktestEngine()
    now = _at(9)
    # Same zone (same entry level); space calls beyond the cooldown so only the
    # per-zone whipsaw cap (default 2) can block the third flip.
    f1 = eng._maybe_stopout_flip(
        _closed_long(), {"outcome": "LOSS"}, {"M5": _m5_falling()}, "XAUUSD", now, 10_000.0,
    )
    f2 = eng._maybe_stopout_flip(
        _closed_long(), {"outcome": "LOSS"}, {"M5": _m5_falling()},
        "XAUUSD", now + timedelta(seconds=60), 10_000.0,
    )
    f3 = eng._maybe_stopout_flip(
        _closed_long(), {"outcome": "LOSS"}, {"M5": _m5_falling()},
        "XAUUSD", now + timedelta(seconds=120), 10_000.0,
    )
    assert f1 is not None and f2 is not None
    assert f3 is None  # zone exhausted after 2 flips


def test_stopout_flip_respects_profile_disable(monkeypatch):
    from types import SimpleNamespace
    eng = BacktestEngine()
    monkeypatch.setattr(
        "brain.instrument_profile.get_profile",
        lambda _s: SimpleNamespace(stopout_flip_enabled=False),
    )
    flip = eng._maybe_stopout_flip(
        _closed_long(), {"outcome": "LOSS"}, {"M5": _m5_falling()}, "XAUUSD", _at(9), 10_000.0,
    )
    assert flip is None
