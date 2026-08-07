"""Tests for tick.live_candle_aggregator.LiveCandleAggregator (Phase 1)."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import pandas as pd
import pytest

from tick.live_candle_aggregator import LiveCandleAggregator
from tick.models import Tick


def _make_tick(symbol: str, mid: float, epoch: float) -> Tick:
    return Tick(
        symbol=symbol,
        bid=mid - 0.0001,
        ask=mid + 0.0001,
        timestamp=datetime.fromtimestamp(epoch, tz=timezone.utc),
        source="test",
    )


def test_on_tick_creates_forming_candle():
    agg = LiveCandleAggregator()
    # 12:00:00 UTC
    agg.on_tick(_make_tick("EURUSD", 1.1000, 1_700_000_000))
    df = agg.get_dataframe("EURUSD", "M5")
    assert df is not None
    last = df.iloc[-1]
    assert last["open"] == pytest.approx(1.1000)
    assert last["high"] == pytest.approx(1.1000)
    assert last["low"] == pytest.approx(1.1000)
    assert last["close"] == pytest.approx(1.1000)


def test_on_tick_updates_forming_candle():
    agg = LiveCandleAggregator()
    base = 1_700_000_000
    agg.on_tick(_make_tick("EURUSD", 1.1000, base))       # open
    agg.on_tick(_make_tick("EURUSD", 1.1020, base + 10))  # higher
    agg.on_tick(_make_tick("EURUSD", 1.0980, base + 20))  # lower
    agg.on_tick(_make_tick("EURUSD", 1.1005, base + 30))  # close
    df = agg.get_dataframe("EURUSD", "M5")
    last = df.iloc[-1]
    assert last["open"] == pytest.approx(1.1000)   # unchanged
    assert last["high"] == pytest.approx(1.1020)
    assert last["low"] == pytest.approx(1.0980)
    assert last["close"] == pytest.approx(1.1005)


def test_candle_boundary_crossing():
    agg = LiveCandleAggregator()
    # base is aligned to a 300s boundary; +299 and +301 straddle the next close.
    base = 1_699_920_000  # divisible by 86400 → aligned for every timeframe
    agg.on_tick(_make_tick("EURUSD", 1.1000, base + 299))
    agg.on_tick(_make_tick("EURUSD", 1.1050, base + 301))
    confirmed = agg.get_confirmed_dataframe("EURUSD", "M5")
    assert confirmed is not None
    assert len(confirmed) == 1  # one candle closed
    assert confirmed.iloc[-1]["close"] == pytest.approx(1.1000)
    # New forming candle has the post-boundary price.
    df = agg.get_dataframe("EURUSD", "M5")
    assert len(df) == 2
    assert df.iloc[-1]["open"] == pytest.approx(1.1050)


def test_ring_buffer_eviction():
    agg = LiveCandleAggregator(max_candles=3)
    tf_secs = 300
    base = 1_700_000_000
    # Fire one tick per consecutive M5 period — each new period closes the prior.
    for i in range(6):
        agg.on_tick(_make_tick("EURUSD", 1.1000 + i * 0.001, base + i * tf_secs))
    confirmed = agg.get_confirmed_dataframe("EURUSD", "M5")
    # 5 closed (the 6th is still forming), capped at max_candles=3.
    assert len(confirmed) == 3
    # Oldest retained should be candle index 2 (open 1.1020).
    assert confirmed.iloc[0]["open"] == pytest.approx(1.1020)


def test_get_dataframe_includes_forming():
    agg = LiveCandleAggregator()
    tf_secs = 300
    base = 1_700_000_000
    agg.on_tick(_make_tick("EURUSD", 1.1000, base))
    agg.on_tick(_make_tick("EURUSD", 1.1010, base + tf_secs))  # closes #1, forms #2
    df = agg.get_dataframe("EURUSD", "M5")
    assert len(df) == 2
    assert df.iloc[-1]["open"] == pytest.approx(1.1010)  # forming bar last


def test_get_confirmed_excludes_forming():
    agg = LiveCandleAggregator()
    tf_secs = 300
    base = 1_700_000_000
    agg.on_tick(_make_tick("EURUSD", 1.1000, base))
    agg.on_tick(_make_tick("EURUSD", 1.1010, base + tf_secs))
    df = agg.get_dataframe("EURUSD", "M5")
    confirmed = agg.get_confirmed_dataframe("EURUSD", "M5")
    assert len(df) == 2
    assert len(confirmed) == 1  # forming bar excluded


def test_get_dataframe_columns():
    agg = LiveCandleAggregator()
    agg.on_tick(_make_tick("EURUSD", 1.1000, 1_700_000_000))
    df = agg.get_dataframe("EURUSD", "M5")
    assert list(df.columns) == ["time", "open", "high", "low", "close", "volume"]
    assert isinstance(df.iloc[-1]["time"], (pd.Timestamp, datetime))


def _seed_df(start_epoch: float, tf_secs: int, n: int) -> pd.DataFrame:
    rows = []
    for i in range(n):
        rows.append(
            {
                "time": datetime.fromtimestamp(
                    start_epoch + i * tf_secs, tz=timezone.utc
                ),
                "open": 1.1000 + i * 0.001,
                "high": 1.1005 + i * 0.001,
                "low": 1.0995 + i * 0.001,
                "close": 1.1002 + i * 0.001,
                "volume": 100 + i,
            }
        )
    return pd.DataFrame(rows, columns=["time", "open", "high", "low", "close", "volume"])


def test_warmup_seeding():
    agg = LiveCandleAggregator()
    base = 1_700_000_000

    def fetcher(symbol, tf, count):
        return _seed_df(base, 300, 5)

    seeded = agg.warmup(["EURUSD"], ["M5"], fetcher)
    assert seeded == 1
    confirmed = agg.get_confirmed_dataframe("EURUSD", "M5")
    assert len(confirmed) == 5
    assert confirmed.iloc[0]["open"] == pytest.approx(1.1000)
    assert confirmed.iloc[-1]["open"] == pytest.approx(1.1040)
    assert confirmed.iloc[0]["volume"] == pytest.approx(100)


def test_warmup_then_live_ticks():
    agg = LiveCandleAggregator()
    tf_secs = 300
    base = 1_700_000_000

    def fetcher(symbol, tf, count):
        return _seed_df(base, tf_secs, 5)

    agg.warmup(["EURUSD"], ["M5"], fetcher)
    # First live tick in a NEW period (after the 5 seeded bars) starts bar #6.
    agg.on_tick(_make_tick("EURUSD", 1.2000, base + 5 * tf_secs))
    df = agg.get_dataframe("EURUSD", "M5")
    assert len(df) == 6
    assert df.iloc[-1]["open"] == pytest.approx(1.2000)  # forming bar #6


def test_gap_handling():
    agg = LiveCandleAggregator()
    tf_secs = 300
    base = 1_700_000_000
    # Tick at boundary 0, then a tick 3 periods later (gap).
    agg.on_tick(_make_tick("EURUSD", 1.1000, base))
    agg.on_tick(_make_tick("EURUSD", 1.1500, base + 3 * tf_secs))
    confirmed = agg.get_confirmed_dataframe("EURUSD", "M5")
    # Only ONE candle closed — no empty intermediates fabricated.
    assert len(confirmed) == 1
    assert confirmed.iloc[-1]["close"] == pytest.approx(1.1000)


def test_multiple_symbols_independent():
    agg = LiveCandleAggregator()
    base = 1_700_000_000
    agg.on_tick(_make_tick("EURUSD", 1.1000, base))
    agg.on_tick(_make_tick("GBPUSD", 1.3000, base))
    eur = agg.get_dataframe("EURUSD", "M5")
    gbp = agg.get_dataframe("GBPUSD", "M5")
    assert eur.iloc[-1]["close"] == pytest.approx(1.1000)
    assert gbp.iloc[-1]["close"] == pytest.approx(1.3000)


def test_all_timeframes_updated():
    agg = LiveCandleAggregator()
    agg.on_tick(_make_tick("EURUSD", 1.1000, 1_700_000_000))
    for tf in ("M1", "M5", "M15", "H1", "H4", "D1"):
        df = agg.get_dataframe("EURUSD", tf)
        assert df is not None, f"timeframe {tf} not tracked"
        assert df.iloc[-1]["close"] == pytest.approx(1.1000)


def test_stats():
    agg = LiveCandleAggregator()
    tf_secs = 300
    base = 1_700_000_000
    agg.on_tick(_make_tick("EURUSD", 1.1000, base))
    agg.on_tick(_make_tick("EURUSD", 1.1010, base + tf_secs))  # closes M5 #1
    st = agg.stats()
    assert set(st.keys()) == {
        "candles_closed",
        "forming_candles",
        "symbols_tracked",
        "warmup_pairs_seeded",
    }
    assert st["symbols_tracked"] == 1
    assert st["candles_closed"] >= 1  # at least the M5 close


def test_none_when_no_data():
    agg = LiveCandleAggregator()
    assert agg.get_dataframe("NOPE", "M5") is None
    assert agg.get_confirmed_dataframe("NOPE", "M5") is None


def test_thread_safety():
    agg = LiveCandleAggregator()
    base = 1_700_000_000

    def fire(thread_id):
        for i in range(100):
            # All within the same M5 period so the forming candle keeps updating.
            agg.on_tick(
                _make_tick("EURUSD", 1.1000 + (i % 10) * 0.0001, base + (i % 250))
            )

    with ThreadPoolExecutor(max_workers=10) as pool:
        futures = [pool.submit(fire, t) for t in range(10)]
        for f in futures:
            f.result()

    df = agg.get_dataframe("EURUSD", "M5")
    assert df is not None
    # State is consistent — high >= low on every row, no corruption / crash.
    assert (df["high"] >= df["low"]).all()
    forming = df.iloc[-1]
    assert forming["high"] >= forming["low"]
