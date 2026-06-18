"""Tests for scanner.candle_close_handler (Phase 3).

Verifies CandleClose events trigger the correct brain modules per TF,
skip-if-unchanged works, WorldModel merging preserves existing TF data,
and the handler is thread-safe under concurrent events.
"""

import sys
import threading
import time
import types
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd
import pytest

# Ensure torch stub exists before any rl imports
if "torch" not in sys.modules:
    _torch = types.ModuleType("torch")
    _torch.nn = types.ModuleType("torch.nn")
    _torch.optim = types.ModuleType("torch.optim")

    class _NoGrad:
        def __enter__(self): return None
        def __exit__(self, *a): return None
        def __call__(self, fn): return fn

    _torch.no_grad = _NoGrad
    _torch.tensor = lambda *a, **kw: None
    _torch.float32 = "float32"
    _torch.device = lambda x: x
    _torch.load = lambda *a, **kw: {}
    _torch.save = lambda *a, **kw: None
    _torch.zeros = lambda *a, **kw: None
    _torch.cat = lambda *a, **kw: None
    _torch.from_numpy = lambda *a, **kw: None
    class _Module:
        def __init__(self, *a, **kw): pass
        def forward(self, *a, **kw): return None
        def parameters(self, *a, **kw): return []
        def eval(self): return self
        def train(self, *a): return self
        def state_dict(self, *a): return {}
        def load_state_dict(self, *a): pass
        def __call__(self, *a, **kw): return None
    _torch.nn.Module = _Module
    _torch.nn.Linear = _Module
    _torch.nn.ReLU = _Module
    _torch.nn.Tanh = _Module
    _torch.nn.Sequential = _Module
    _torch.nn.functional = types.ModuleType("torch.nn.functional")
    _torch.distributions = types.ModuleType("torch.distributions")
    _torch.distributions.Categorical = type("Categorical", (), {"__init__": lambda *a, **kw: None})
    class _Adam:
        def __init__(self, *a, **kw): pass
        def zero_grad(self): pass
        def step(self): pass
    _torch.optim.Adam = _Adam
    sys.modules["torch"] = _torch
    sys.modules["torch.nn"] = _torch.nn
    sys.modules["torch.optim"] = _torch.optim
    sys.modules["torch.nn.functional"] = _torch.nn.functional
    sys.modules["torch.distributions"] = _torch.distributions

from brain.world_model import WorldModelStore, build_world_model
from scanner.candle_close_handler import (
    CandleCloseHandler,
    TF_MODULE_MAP,
    _bar_hash,
)
from tick.event_bus import EventBus
from tick.models import CandleClose, Tick


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_candles(n: int = 50, base: float = 1.1000) -> pd.DataFrame:
    """Synthetic OHLCV DataFrame with ``n`` bars."""
    np.random.seed(42)
    closes = base + np.cumsum(np.random.randn(n) * 0.0005)
    return pd.DataFrame({
        "open": closes - 0.0002,
        "high": closes + 0.0005,
        "low": closes - 0.0005,
        "close": closes,
        "volume": np.random.randint(100, 5000, n),
    })


def _make_tick(symbol: str = "EURUSD", ts: datetime = None) -> Tick:
    ts = ts or datetime(2025, 6, 18, 12, 5, 0, tzinfo=timezone.utc)
    return Tick(symbol=symbol, bid=1.1000, ask=1.1002, timestamp=ts, source="mt5")


def _make_candle_close(
    symbol: str = "EURUSD",
    tf: str = "M5",
    ts: datetime = None,
) -> CandleClose:
    ts = ts or datetime(2025, 6, 18, 12, 5, 0, tzinfo=timezone.utc)
    return CandleClose(
        symbol=symbol,
        timeframe=tf,
        close_time=ts,
        last_tick=_make_tick(symbol, ts),
    )


def _fetcher_factory(candles: pd.DataFrame = None):
    """Return a candle fetcher callable that returns the given DataFrame."""
    df = candles if candles is not None else _make_candles()

    def fetch(symbol: str, tf: str, count: int):
        return df.copy()

    return fetch


def _wait_processed(handler: CandleCloseHandler, target: int, timeout: float = 5.0):
    """Block until handler.events_processed >= target or timeout."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        total = handler.events_processed + handler.events_skipped
        if total >= target:
            return True
        time.sleep(0.02)
    return False


# ---------------------------------------------------------------------------
# _bar_hash
# ---------------------------------------------------------------------------


class TestBarHash:
    def test_same_data_same_hash(self):
        df = _make_candles(10)
        assert _bar_hash(df) == _bar_hash(df.copy())

    def test_different_close_different_hash(self):
        df1 = _make_candles(10)
        df2 = df1.copy()
        df2.iloc[-1, df2.columns.get_loc("close")] += 0.001
        assert _bar_hash(df1) != _bar_hash(df2)

    def test_empty_returns_empty(self):
        assert _bar_hash(pd.DataFrame()) == ""
        assert _bar_hash(None) == ""


# ---------------------------------------------------------------------------
# CandleCloseHandler — basic
# ---------------------------------------------------------------------------


class TestCandleCloseHandlerBasic:

    def test_subscribes_on_init(self):
        bus = EventBus()
        store = WorldModelStore()
        handler = CandleCloseHandler(bus, store, _fetcher_factory())
        assert bus.subscriber_count("candle_close") == 1
        handler.shutdown()

    def test_unsubscribes_on_shutdown(self):
        bus = EventBus()
        store = WorldModelStore()
        handler = CandleCloseHandler(bus, store, _fetcher_factory())
        handler.shutdown()
        assert bus.subscriber_count("candle_close") == 0

    def test_ignores_unsupported_timeframe(self):
        bus = EventBus()
        store = WorldModelStore()
        handler = CandleCloseHandler(bus, store, _fetcher_factory())
        event = _make_candle_close(tf="W1")
        bus.publish("candle_close", event)
        time.sleep(0.1)
        assert handler.events_processed == 0
        handler.shutdown()

    def test_ignores_events_after_shutdown(self):
        bus = EventBus()
        store = WorldModelStore()
        handler = CandleCloseHandler(bus, store, _fetcher_factory())
        handler.shutdown()
        event = _make_candle_close()
        bus.publish("candle_close", event)
        time.sleep(0.1)
        assert handler.events_processed == 0


# ---------------------------------------------------------------------------
# CandleCloseHandler — module execution per TF
# ---------------------------------------------------------------------------


class TestModuleExecution:

    @pytest.fixture(autouse=True)
    def _setup(self):
        self.bus = EventBus()
        self.store = WorldModelStore()
        self.candles = _make_candles(60)
        self.handler = CandleCloseHandler(
            self.bus, self.store, _fetcher_factory(self.candles), max_workers=1,
        )
        yield
        self.handler.shutdown()

    def test_m5_close_populates_worldmodel(self):
        event = _make_candle_close(tf="M5")
        self.bus.publish("candle_close", event)
        assert _wait_processed(self.handler, 1)
        wm = self.store.get("EURUSD")
        assert wm is not None
        fvgs = wm.fvgs_by_tf()
        assert "M5" in fvgs

    def test_h1_close_populates_worldmodel(self):
        event = _make_candle_close(tf="H1")
        self.bus.publish("candle_close", event)
        assert _wait_processed(self.handler, 1)
        wm = self.store.get("EURUSD")
        assert wm is not None
        assert "H1" in wm.structure_by_tf()
        assert "H1" in wm.liquidity_by_tf()

    def test_h4_close_populates_worldmodel(self):
        event = _make_candle_close(tf="H4")
        self.bus.publish("candle_close", event)
        assert _wait_processed(self.handler, 1)
        wm = self.store.get("EURUSD")
        assert wm is not None
        assert "H4" in wm.structure_by_tf()

    def test_d1_close_populates_structure_only(self):
        event = _make_candle_close(tf="D1")
        self.bus.publish("candle_close", event)
        assert _wait_processed(self.handler, 1)
        wm = self.store.get("EURUSD")
        assert wm is not None
        assert "D1" in wm.structure_by_tf()
        assert wm.fvgs_by_tf().get("D1") is None

    def test_m15_close_populates_fvg_only(self):
        event = _make_candle_close(tf="M15")
        self.bus.publish("candle_close", event)
        assert _wait_processed(self.handler, 1)
        wm = self.store.get("EURUSD")
        assert wm is not None
        assert "M15" in wm.fvgs_by_tf()
        assert wm.structure_by_tf().get("M15") is None


# ---------------------------------------------------------------------------
# CandleCloseHandler — skip-if-unchanged
# ---------------------------------------------------------------------------


class TestSkipIfUnchanged:

    def test_same_candles_skipped(self):
        bus = EventBus()
        store = WorldModelStore()
        candles = _make_candles(60)
        handler = CandleCloseHandler(
            bus, store, _fetcher_factory(candles), max_workers=1,
        )
        event = _make_candle_close(tf="M5")
        bus.publish("candle_close", event)
        assert _wait_processed(handler, 1)
        assert handler.events_processed == 1
        assert handler.events_skipped == 0

        bus.publish("candle_close", event)
        assert _wait_processed(handler, 2)
        assert handler.events_processed == 1
        assert handler.events_skipped == 1
        handler.shutdown()

    def test_different_candles_not_skipped(self):
        bus = EventBus()
        store = WorldModelStore()
        call_count = [0]
        candles1 = _make_candles(60, base=1.1000)
        candles2 = _make_candles(60, base=1.2000)

        def fetcher(symbol, tf, count):
            call_count[0] += 1
            return candles1 if call_count[0] == 1 else candles2

        handler = CandleCloseHandler(bus, store, fetcher, max_workers=1)
        event = _make_candle_close(tf="M5")
        bus.publish("candle_close", event)
        assert _wait_processed(handler, 1)
        bus.publish("candle_close", event)
        assert _wait_processed(handler, 2)
        assert handler.events_processed == 2
        assert handler.events_skipped == 0
        handler.shutdown()


# ---------------------------------------------------------------------------
# CandleCloseHandler — WorldModel merging
# ---------------------------------------------------------------------------


class TestWorldModelMerging:

    def test_preserves_existing_tf_data(self):
        bus = EventBus()
        store = WorldModelStore()
        candles = _make_candles(60)
        handler = CandleCloseHandler(
            bus, store, _fetcher_factory(candles), max_workers=1,
        )

        m5_event = _make_candle_close(tf="M5")
        bus.publish("candle_close", m5_event)
        assert _wait_processed(handler, 1)

        wm1 = store.get("EURUSD")
        assert wm1 is not None
        m5_fvg_count = len(wm1.fvgs_by_tf().get("M5", ()))

        h1_event = _make_candle_close(tf="H1")
        bus.publish("candle_close", h1_event)
        assert _wait_processed(handler, 2)

        wm2 = store.get("EURUSD")
        assert wm2 is not None
        assert wm2.version > wm1.version
        assert len(wm2.fvgs_by_tf().get("M5", ())) == m5_fvg_count
        assert "H1" in wm2.structure_by_tf()
        handler.shutdown()

    def test_multiple_symbols_independent(self):
        bus = EventBus()
        store = WorldModelStore()
        candles = _make_candles(60)
        handler = CandleCloseHandler(
            bus, store, _fetcher_factory(candles), max_workers=2,
        )

        bus.publish("candle_close", _make_candle_close(symbol="EURUSD", tf="M5"))
        bus.publish("candle_close", _make_candle_close(symbol="GBPUSD", tf="M5"))
        assert _wait_processed(handler, 2)

        assert store.get("EURUSD") is not None
        assert store.get("GBPUSD") is not None
        assert store.get("EURUSD").symbol == "EURUSD"
        assert store.get("GBPUSD").symbol == "GBPUSD"
        handler.shutdown()


# ---------------------------------------------------------------------------
# CandleCloseHandler — fetcher failure
# ---------------------------------------------------------------------------


class TestFetcherFailure:

    def test_none_fetcher_skips_gracefully(self):
        bus = EventBus()
        store = WorldModelStore()
        handler = CandleCloseHandler(
            bus, store, lambda s, t, c: None, max_workers=1,
        )
        bus.publish("candle_close", _make_candle_close(tf="M5"))
        time.sleep(0.3)
        assert handler.events_processed == 0
        assert store.get("EURUSD") is None
        handler.shutdown()

    def test_empty_fetcher_skips_gracefully(self):
        bus = EventBus()
        store = WorldModelStore()
        handler = CandleCloseHandler(
            bus, store, lambda s, t, c: pd.DataFrame(), max_workers=1,
        )
        bus.publish("candle_close", _make_candle_close(tf="M5"))
        time.sleep(0.3)
        assert handler.events_processed == 0
        assert store.get("EURUSD") is None
        handler.shutdown()

    def test_fetcher_exception_does_not_crash(self):
        bus = EventBus()
        store = WorldModelStore()

        def bad_fetcher(s, t, c):
            raise RuntimeError("broker down")

        handler = CandleCloseHandler(bus, store, bad_fetcher, max_workers=1)
        bus.publish("candle_close", _make_candle_close(tf="M5"))
        time.sleep(0.3)
        assert handler.events_processed == 0
        assert store.get("EURUSD") is None
        handler.shutdown()


# ---------------------------------------------------------------------------
# CandleCloseHandler — thread safety
# ---------------------------------------------------------------------------


class TestThreadSafety:

    def test_concurrent_events_different_symbols(self):
        bus = EventBus()
        store = WorldModelStore()
        candles = _make_candles(60)
        handler = CandleCloseHandler(
            bus, store, _fetcher_factory(candles), max_workers=4,
        )

        symbols = [f"SYM{i}" for i in range(8)]
        for sym in symbols:
            bus.publish("candle_close", _make_candle_close(symbol=sym, tf="M5"))

        assert _wait_processed(handler, 8, timeout=10.0)
        for sym in symbols:
            assert store.get(sym) is not None
        handler.shutdown()

    def test_concurrent_events_same_symbol_different_tf(self):
        bus = EventBus()
        store = WorldModelStore()
        call_count = [0]
        base_candles = _make_candles(60)

        def varying_fetcher(symbol, tf, count):
            call_count[0] += 1
            df = base_candles.copy()
            df.iloc[-1, df.columns.get_loc("close")] += call_count[0] * 0.001
            return df

        handler = CandleCloseHandler(
            bus, store, varying_fetcher, max_workers=4,
        )

        for tf in ["M5", "M15", "H1", "H4", "D1"]:
            bus.publish("candle_close", _make_candle_close(tf=tf))

        assert _wait_processed(handler, 5, timeout=10.0)
        wm = store.get("EURUSD")
        assert wm is not None
        assert "M5" in wm.fvgs_by_tf()
        assert "H1" in wm.structure_by_tf()
        assert "D1" in wm.structure_by_tf()
        handler.shutdown()


# ---------------------------------------------------------------------------
# CandleCloseHandler — stats / observability
# ---------------------------------------------------------------------------


class TestStats:

    def test_stats_dict(self):
        bus = EventBus()
        store = WorldModelStore()
        handler = CandleCloseHandler(
            bus, store, _fetcher_factory(), max_workers=1,
        )
        bus.publish("candle_close", _make_candle_close(tf="M5"))
        assert _wait_processed(handler, 1)
        s = handler.stats()
        assert s["received"] >= 1
        assert s["processed"] == 1
        assert s["skipped"] == 0
        handler.shutdown()


# ---------------------------------------------------------------------------
# TF_MODULE_MAP coverage
# ---------------------------------------------------------------------------


class TestTFModuleMap:

    def test_all_expected_tfs_present(self):
        assert set(TF_MODULE_MAP.keys()) == {"M5", "M15", "H1", "H4", "D1"}

    def test_m5_modules(self):
        assert set(TF_MODULE_MAP["M5"]) == {"fvg", "order_block", "volume", "inducement"}

    def test_m15_modules(self):
        assert TF_MODULE_MAP["M15"] == ["fvg"]

    def test_h1_modules(self):
        expected = {"fvg", "order_block", "liquidity", "volume", "wyckoff", "structure"}
        assert set(TF_MODULE_MAP["H1"]) == expected

    def test_h4_modules(self):
        assert set(TF_MODULE_MAP["H4"]) == {"order_block", "liquidity", "structure"}

    def test_d1_modules(self):
        assert TF_MODULE_MAP["D1"] == ["structure"]
