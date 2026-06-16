"""Phase 5 performance fixes — tests.

Covers:
  * CandleCache: TTL expiry, count-aware misses, empty-not-cached, stats,
    thread safety.
  * Batch MT5 symbol_info: fallback to {} when MT5 unavailable.
  * _mt5_market_open: served from the per-cycle info cache.
  * LiquidityMapper thread safety: concurrent map() over different pip sizes
    matches sequential results (no shared-state corruption).
  * Per-cycle account/margin cache: cached within a cycle, invalidated when the
    cycle id advances, bypassed when disabled.
"""

import threading
import time

import numpy as np
import pandas as pd
import pytest


# ───────────────────────────── CandleCache ──────────────────────────────
from platforms.candle_cache import CandleCache


def _df(n=5):
    return pd.DataFrame({"close": np.arange(n, dtype=float)})


def test_cache_hit_after_put():
    c = CandleCache(default_ttl=10.0)
    assert c.get("EURUSD", "M5", 200) is None  # cold miss
    c.put("EURUSD", "M5", 200, _df())
    got = c.get("EURUSD", "M5", 200)
    assert got is not None and len(got) == 5
    s = c.cache_stats()
    assert s["hits"] == 1 and s["misses"] == 1 and s["stores"] == 1


def test_cache_ttl_expiry():
    c = CandleCache(ttl_by_tf={"M5": 0.2}, default_ttl=0.2)
    c.put("EURUSD", "M5", 200, _df())
    assert c.get("EURUSD", "M5", 200) is not None
    time.sleep(0.25)
    assert c.get("EURUSD", "M5", 200) is None  # expired → miss
    assert c.cache_stats()["expired"] == 1


def test_cache_count_aware_miss():
    """A request for more bars than cached must be a miss, never a short frame."""
    c = CandleCache(default_ttl=10.0)
    c.put("EURUSD", "M5", 100, _df(100))
    assert c.get("EURUSD", "M5", 100) is not None
    assert c.get("EURUSD", "M5", 200) is None  # different count = miss


def test_cache_never_stores_empty():
    c = CandleCache(default_ttl=10.0)
    c.put("X", "M5", 200, pd.DataFrame())  # empty
    c.put("Y", "M5", 200, None)            # None
    assert c.get("X", "M5", 200) is None
    assert c.get("Y", "M5", 200) is None
    assert c.cache_stats()["stores"] == 0


def test_cache_disabled_is_passthrough():
    c = CandleCache(enabled=False)
    c.put("EURUSD", "M5", 200, _df())
    assert c.get("EURUSD", "M5", 200) is None  # always miss when disabled


def test_cache_invalidate():
    c = CandleCache(default_ttl=10.0)
    c.put("EURUSD", "M5", 200, _df())
    c.put("EURUSD", "H1", 200, _df())
    c.invalidate(symbol="EURUSD", timeframe="M5")
    assert c.get("EURUSD", "M5", 200) is None
    assert c.get("EURUSD", "H1", 200) is not None
    c.invalidate()  # clear all
    assert c.get("EURUSD", "H1", 200) is None


def test_cache_thread_safety():
    """Concurrent put/get from many threads must not corrupt the cache."""
    c = CandleCache(default_ttl=30.0)
    errors = []

    def worker(i):
        try:
            for _ in range(50):
                sym = f"SYM{i % 8}"
                c.put(sym, "M5", 200, _df())
                c.get(sym, "M5", 200)
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    # Stats counters stay internally consistent.
    s = c.cache_stats()
    assert s["total"] == s["hits"] + s["misses"]


# ─────────────────────── Batch symbol_info / market_open ────────────────────
import scanner.pair_scanner as ps


def test_batch_symbol_info_fallback_when_no_mt5(monkeypatch):
    monkeypatch.setattr(ps, "_MT5_AVAILABLE", False)
    monkeypatch.setattr(ps, "mt5", None)
    assert ps._batch_mt5_symbol_info() == {}


def test_batch_symbol_info_handles_get_failure(monkeypatch):
    class _MT5:
        @staticmethod
        def symbols_get():
            raise RuntimeError("ipc down")

    monkeypatch.setattr(ps, "_MT5_AVAILABLE", True)
    monkeypatch.setattr(ps, "mt5", _MT5)
    # Any failure → {} so callers fall back to per-symbol lookups.
    assert ps._batch_mt5_symbol_info() == {}


def test_market_open_uses_info_cache(monkeypatch):
    class _Info:
        def __init__(self, mode):
            self.trade_mode = mode

    class _MT5:
        select_calls = 0
        info_calls = 0

        @staticmethod
        def symbol_select(name, on):
            _MT5.select_calls += 1
            return True

        @staticmethod
        def symbol_info(name):
            _MT5.info_calls += 1
            return _Info(4)

    monkeypatch.setattr(ps, "_MT5_AVAILABLE", True)
    monkeypatch.setattr(ps, "mt5", _MT5)

    cache = {"EURUSD": _Info(4), "USDJPY": _Info(0)}
    # Cache hit → tradeable, no IPC.
    assert ps._mt5_market_open("EURUSD", connector=None, info_cache=cache) is True
    # Cache hit → trade_mode 0 = not tradeable.
    assert ps._mt5_market_open("USDJPY", connector=None, info_cache=cache) is False
    assert _MT5.info_calls == 0 and _MT5.select_calls == 0
    # Cache miss → falls back to per-symbol IPC.
    assert ps._mt5_market_open("GBPUSD", connector=None, info_cache=cache) is True
    assert _MT5.info_calls == 1


# ─────────────────────── LiquidityMapper thread safety ──────────────────────
from brain.liquidity_mapper import LiquidityMapper


def _ohlc(seed, n=120):
    rng = np.random.default_rng(seed)
    base = 100 + np.cumsum(rng.normal(0, 0.5, n))
    high = base + np.abs(rng.normal(0, 0.3, n))
    low = base - np.abs(rng.normal(0, 0.3, n))
    close = base + rng.normal(0, 0.1, n)
    return pd.DataFrame({
        "time": pd.date_range("2024-01-01", periods=n, freq="5min"),
        "open": base, "high": high, "low": low, "close": close,
    })


def test_liquidity_mapper_concurrent_matches_sequential():
    """map() must be thread-safe: concurrent calls over different pip sizes
    must produce identical results to running them sequentially (proves the
    per-call threshold fix removed the shared-state race)."""
    mapper = LiquidityMapper()
    cases = [(_ohlc(s), 0.0001 if s % 2 else 0.01) for s in range(12)]

    sequential = [mapper.map(df, pip_size=ps_).liquidity_bias for df, ps_ in cases]

    results = [None] * len(cases)

    def worker(idx):
        df, ps_ = cases[idx]
        for _ in range(10):
            results[idx] = mapper.map(df, pip_size=ps_).liquidity_bias

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(len(cases))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert results == sequential


# ─────────────────────── Per-cycle margin/account cache ─────────────────────
class _PerfCfg:
    account_info_cache_enabled = True


class _Cfg:
    performance = _PerfCfg()


class _MixinHarness:
    """Minimal stand-in exercising the cached margin helper from the mixin."""

    def __init__(self, cycle="c1"):
        from platforms.trading_loop.risk_heat_mixin import RiskHeatMarginMixin
        self._get_margin_level_cached = RiskHeatMarginMixin._get_margin_level_cached.__get__(self)
        self.config = _Cfg()
        self._current_cycle_id = cycle
        self.calls = 0

    def _account_key(self, symbol):
        return "acct-A"  # all symbols share one account silo

    def _get_margin_level(self, symbol):
        self.calls += 1
        return 250.0


def test_margin_cache_within_cycle():
    h = _MixinHarness()
    assert h._get_margin_level_cached("EURUSD") == 250.0
    assert h._get_margin_level_cached("GBPUSD") == 250.0  # same account silo
    assert h._get_margin_level_cached("EURUSD") == 250.0
    assert h.calls == 1  # only one real fetch for the whole cycle


def test_margin_cache_invalidates_on_new_cycle():
    h = _MixinHarness(cycle="c1")
    h._get_margin_level_cached("EURUSD")
    assert h.calls == 1
    h._current_cycle_id = "c2"  # cycle advances
    h._get_margin_level_cached("EURUSD")
    assert h.calls == 2  # cache invalidated, refetched


def test_margin_cache_bypassed_when_disabled():
    h = _MixinHarness()
    h.config.performance.account_info_cache_enabled = False
    h._get_margin_level_cached("EURUSD")
    h._get_margin_level_cached("EURUSD")
    assert h.calls == 2  # no caching


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
