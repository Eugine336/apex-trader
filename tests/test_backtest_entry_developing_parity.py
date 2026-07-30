"""Backtest ↔ live parity — Phase 3 entry timing + developing analysis.

Verifies the final entry-plane parity layer in ``brain.backtest_engine``:

* M1 momentum confirmation gate (mirrors ``entry.m1_confirmation``),
* zone-edge entry pricing on an M1 zone touch (mirrors
  ``entry.tick_entry_detector``) with ``source`` attribution,
* developing-analysis simulation (forming HTF candle → developing structure fed
  to ``compute_bias`` as ×0.70 discounted evidence, mirroring the confirmed live
  path in ``scanner.candle_close_handler``),
* entry-source attribution flowing through to the trade journal.
"""

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

import numpy as np
import pandas as pd

from brain.backtest_engine import BacktestEngine, BacktestSetup
from entry.models import EntryZone, ZoneType


# ── Helpers ──────────────────────────────────────────────────────────────


def _m1(opens, closes, lows=None, highs=None):
    """Build an M1 DataFrame from explicit open/close (and optional low/high)."""
    n = len(opens)
    lows = lows if lows is not None else [min(o, c) - 0.0005 for o, c in zip(opens, closes)]
    highs = highs if highs is not None else [max(o, c) + 0.0005 for o, c in zip(opens, closes)]
    return pd.DataFrame({
        "time": pd.date_range("2024-01-01", periods=n, freq="min", tz="UTC"),
        "open": opens, "high": highs, "low": lows, "close": closes,
        "volume": [100.0] * n,
    })


def _zone(direction, top=1.1010, bottom=1.1000, inval=None):
    now = datetime.now(timezone.utc)
    if inval is None:
        inval = bottom - 0.0010 if direction == "LONG" else top + 0.0010
    return EntryZone(
        symbol="EURUSD", direction=direction, zone_type=ZoneType.FVG_OB_OVERLAP,
        top=top, bottom=bottom, midpoint=(top + bottom) / 2.0,
        invalidation_level=inval, conviction=100,
        created_at=now, expires_at=now, timeframe="M5",
    )


# ── A. M1 momentum confirmation ──────────────────────────────────────────


def test_m1_momentum_confirms_three_of_five_long():
    eng = BacktestEngine()
    # 3/5 bullish closes → confirmed for a LONG.
    m1 = _m1([1.100] * 5, [1.101, 1.099, 1.101, 1.099, 1.101])
    assert eng._m1_momentum_confirmed(m1, "LONG") is True


def test_m1_momentum_rejects_two_of_five_long():
    eng = BacktestEngine()
    # Only 2/5 bullish, not the last two consecutive, no higher-low pattern.
    m1 = _m1(
        [1.100] * 5,
        [1.101, 1.099, 1.101, 1.099, 1.0985],
        lows=[1.0995, 1.0990, 1.0995, 1.0990, 1.0985],
    )
    assert eng._m1_momentum_confirmed(m1, "LONG") is False


def test_m1_momentum_two_consecutive_rising_long():
    eng = BacktestEngine()
    # Only the last two candles are consecutive rising bullish closes.
    m1 = _m1([1.100] * 5, [1.099, 1.099, 1.099, 1.101, 1.102])
    assert eng._m1_momentum_confirmed(m1, "LONG") is True


def test_m1_momentum_higher_low_higher_close_long():
    eng = BacktestEngine()
    # No bullish closes and no two-consecutive rise, but a higher-low +
    # higher-close reversal pattern → confirmed (mirrors the live confirmer).
    m1 = _m1(
        [1.100] * 5,
        [1.0990, 1.0995, 1.0985, 1.0993, 1.0996],
        lows=[1.0980, 1.0975, 1.0970, 1.0985, 1.0990],
    )
    assert eng._m1_momentum_confirmed(m1, "LONG") is True


def test_m1_momentum_confirms_three_of_five_short():
    eng = BacktestEngine()
    m1 = _m1([1.100] * 5, [1.099, 1.101, 1.099, 1.101, 1.099])
    assert eng._m1_momentum_confirmed(m1, "SHORT") is True


def test_m1_momentum_insufficient_bars_rejects():
    eng = BacktestEngine()
    m1 = _m1([1.100] * 3, [1.101] * 3)
    assert eng._m1_momentum_confirmed(m1, "LONG") is False
    assert eng._m1_momentum_confirmed(None, "LONG") is False


# ── B. Zone-edge entry pricing + source attribution ──────────────────────


def test_zone_edge_entry_long_touch_sets_zone_source():
    eng = BacktestEngine()
    zone = _zone("LONG", top=1.1010, bottom=1.1000)
    signal = SimpleNamespace(entry_price=1.1005, stop_loss=1.0990, tp1=1.1030, tp2=1.1060)
    # Candle dips into the demand zone (low below the zone top).
    m1 = _m1([1.1015], [1.1012], lows=[1.1003], highs=[1.1016])
    source = eng._apply_zone_edge_entry(signal, zone, m1, "LONG")
    assert source == "zone"
    # Deeper into the zone → fill at the candle low (clamped to the zone band).
    assert signal.entry_price == 1.1003


def test_zone_edge_entry_short_touch_sets_zone_source():
    eng = BacktestEngine()
    zone = _zone("SHORT", top=1.1010, bottom=1.1000)
    signal = SimpleNamespace(entry_price=1.1005, stop_loss=1.1030, tp1=1.0980, tp2=1.0950)
    # Candle rallies up into the supply zone (high above the zone bottom).
    m1 = _m1([1.0995], [1.0998], lows=[1.0994], highs=[1.1007])
    source = eng._apply_zone_edge_entry(signal, zone, m1, "SHORT")
    assert source == "zone"
    assert signal.entry_price == 1.1007


def test_zone_edge_entry_no_touch_keeps_consensus():
    eng = BacktestEngine()
    zone = _zone("LONG", top=1.1010, bottom=1.1000)
    signal = SimpleNamespace(entry_price=1.1005, stop_loss=1.0990, tp1=1.1030, tp2=1.1060)
    # Candle stays entirely above the zone — no touch this bar.
    m1 = _m1([1.1030], [1.1028], lows=[1.1020], highs=[1.1035])
    source = eng._apply_zone_edge_entry(signal, zone, m1, "LONG")
    assert source == "consensus"
    assert signal.entry_price == 1.1005  # unchanged consensus entry


# ── C. Developing analysis simulation ────────────────────────────────────


def _mk(freq, n, base=1.10, drift=0.0):
    t = pd.date_range("2024-01-01", periods=n, freq=freq, tz="UTC")
    c = base + np.arange(n) * drift
    return pd.DataFrame({
        "time": t, "open": c, "high": c + 0.0005, "low": c - 0.0005,
        "close": c, "volume": np.full(n, 100.0),
    })


def _dev_slices():
    m1 = _mk("min", 300, drift=0.00001)
    return {
        "H4": _mk("4h", 60), "H1": _mk("h", 120), "M15": _mk("15min", 200),
        "M5": _mk("5min", 250), "M1": m1,
    }


def test_forming_htf_candle_aggregates_m1():
    eng = BacktestEngine()
    slices = _dev_slices()
    m1, base = slices["M1"], slices["H1"]
    now = pd.Timestamp(m1["time"].iloc[-1]).to_pydatetime()
    forming = eng._forming_htf_candle(m1, base, now, 3600)
    assert forming is not None and len(forming) == 1
    assert list(forming.columns) == list(base.columns)
    # Window = M1 bars in the current forming hour; OHLC folds first/max/min/last.
    boundary = pd.Timestamp(now).floor("3600s")
    window = m1[(m1["time"] >= boundary) & (m1["time"] <= pd.Timestamp(now))]
    assert forming["open"].iloc[0] == float(window["open"].iloc[0])
    assert forming["high"].iloc[0] == float(window["high"].max())
    assert forming["low"].iloc[0] == float(window["low"].min())
    assert forming["close"].iloc[0] == float(window["close"].iloc[-1])
    assert forming["volume"].iloc[0] == float(window["volume"].sum())


def test_build_developing_struct_returns_struct_per_htf():
    eng = BacktestEngine()
    slices = _dev_slices()
    now = pd.Timestamp(slices["M1"]["time"].iloc[-1]).to_pydatetime()
    dev = eng._build_developing_struct("EURUSD", slices, now)
    assert dev is not None
    # Developing structure is produced for every HTF the replay slice carries.
    assert set(dev.keys()) == {"M5", "M15", "H1", "H4"}
    for sa in dev.values():
        assert hasattr(sa, "trend")


def test_build_developing_struct_none_without_m1():
    eng = BacktestEngine()
    assert eng._build_developing_struct("EURUSD", {"H1": _mk("h", 50)}, datetime.now(timezone.utc)) is None


# ── D. Entry-source attribution reaches the trade journal ────────────────


def test_backtest_setup_source_defaults_empty():
    setup = BacktestSetup(direction="LONG", entry_price=1.10, stop_loss=1.095,
                          tp1=1.105, tp2=1.115, score=100)
    assert setup.source == ""


def test_journal_trade_carries_source():
    class _StubJournal:
        def __init__(self):
            self.captured = None

        async def log_trade(self, record):
            self.captured = record

    eng = BacktestEngine(journal=_StubJournal())
    setup = BacktestSetup(direction="LONG", entry_price=1.10, stop_loss=1.095,
                          tp1=1.105, tp2=1.115, score=100, source="zone")
    trade = {
        "setup": setup, "entry_price": 1.10, "risk": 0.005,
        "session": "LONDON", "entry_type": "FVG_OB_OVERLAP",
    }
    close_event = {"pnl_r": 1.5, "hold_minutes": 42.0, "outcome": "WIN"}
    eng._journal_trade("EURUSD", trade, close_event, datetime(2024, 1, 1, tzinfo=timezone.utc))
    # _journal_trade fires the async write; drain it if a task was scheduled.
    assert eng.journal.captured is not None
    assert eng.journal.captured.source == "zone"
