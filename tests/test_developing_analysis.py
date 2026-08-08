"""APEX TRADER — Phase 2 developing analysis tests.

Covers:
  * the ``include_forming`` flag on ``run_tf_modules`` (forming bar kept),
  * the ``DevelopingAnalysisLoop`` (analysis, rate limiting, merge, isolation,
    lifecycle, stats).
"""

from types import SimpleNamespace

import pandas as pd

import brain.decision_core as dc
from brain.decision_core import run_tf_modules
from brain.developing_analysis import DevelopingAnalysisLoop
from brain.structure_engine import StructureAnalysis, StructureEvent, Trend
from brain.world_model import WorldModelStore
from config import DevelopingAnalysisConfig


# ── Helpers ───────────────────────────────────────────────────────────────


def _sa(trend: str, confidence: float) -> StructureAnalysis:
    """Minimal StructureAnalysis for developing-structure tests."""
    return StructureAnalysis(
        trend=Trend(trend),
        last_event=StructureEvent.NONE,
        swing_high=None,
        swing_low=None,
        last_bos_level=None,
        last_choch_level=None,
        structure_broken=False,
        bullish_swing_points=[],
        bearish_swing_points=[],
        confidence=confidence,
    )


def _ohlcv_df(n: int = 60) -> pd.DataFrame:
    closes = [1.1000 + i * 0.0010 for i in range(n)]
    opens = [closes[0]] + closes[:-1]
    return pd.DataFrame(
        {
            "time": pd.date_range("2026-01-01", periods=n, freq="1h", tz="UTC"),
            "open": opens,
            "high": [c + 0.0008 for c in closes],
            "low": [c - 0.0008 for c in closes],
            "close": closes,
            "volume": [100.0 + i for i in range(n)],
        }
    )


def _profile() -> SimpleNamespace:
    return SimpleNamespace(
        fvg_proximity_pips=10.0,
        fvg_min_size_pips=1.0,
        ob_min_impulse_pips=5.0,
        ob_buffer_pips=2.0,
        wyckoff_enabled=True,
        swing_lookback=2,
    )


class _FakeAggregator:
    """Stand-in LiveCandleAggregator returning a fixed DataFrame."""

    def __init__(self, df=None):
        self._df = df

    def get_dataframe(self, symbol, tf):
        return self._df


class _FakePool:
    """Records submissions instead of running them."""

    def __init__(self):
        self.calls = []

    def submit(self, fn, *args):
        self.calls.append((fn, args))

    def shutdown(self, wait=False):
        pass


def _loop(symbols=None, df=None, config=None) -> DevelopingAnalysisLoop:
    return DevelopingAnalysisLoop(
        candle_aggregator=_FakeAggregator(df),
        developing_store=WorldModelStore(),
        symbols=symbols if symbols is not None else ["EURUSD"],
        config=config or DevelopingAnalysisConfig(),
    )


# ── Config ─────────────────────────────────────────────────────────────────


def test_config_defaults():
    cfg = DevelopingAnalysisConfig()
    assert cfg.enabled is True
    assert cfg.max_workers == 4
    assert cfg.confidence_discount == 0.7
    assert cfg.refresh_m5 == 10.0
    assert cfg.refresh_h1 == 30.0
    assert cfg.refresh_d1 == 300.0


# ── include_forming ──────────────────────────────────────────────────────────


def test_include_forming_passes_through(monkeypatch):
    """include_forming=True keeps the forming bar; default drops it."""
    n = 60
    df = _ohlcv_df(n)
    monkeypatch.setattr(dc, "get_pip_size", lambda symbol: 0.0001)
    monkeypatch.setattr(dc, "get_profile", lambda symbol: _profile())

    seen = {}

    def _fvg_detect(self, d, timeframe=None):
        seen["fvg"] = len(d)
        return []

    monkeypatch.setattr(dc.FVGDetector, "detect", _fvg_detect)

    structure = SimpleNamespace(analyze=lambda d, pip_size=None: None)
    liquidity = SimpleNamespace(map=lambda d, pip_size: {})
    volume = SimpleNamespace(analyze=lambda d, closed_df=None: {})

    # Confirmed path drops the forming bar.
    run_tf_modules(
        "EURUSD", "H1", df,
        structure=structure, liquidity=liquidity, volume=volume,
    )
    assert seen["fvg"] == n - 1

    # Developing path keeps it.
    run_tf_modules(
        "EURUSD", "H1", df,
        structure=structure, liquidity=liquidity, volume=volume,
        include_forming=True,
    )
    assert seen["fvg"] == n


# ── DevelopingAnalysisLoop ──────────────────────────────────────────────────


def test_analyze_one_produces_developing_worldmodel(monkeypatch):
    loop = _loop(df=_ohlcv_df(60))
    monkeypatch.setattr(
        "brain.developing_analysis.run_tf_modules",
        lambda *a, **k: {"structure": _sa("BULLISH", 0.7)},
    )
    loop._analyze_one("EURUSD", "H1")
    wm = loop._developing_store.get("EURUSD")
    assert wm is not None
    assert "H1" in wm.structure_by_tf()
    assert loop._analyses_run == 1


def test_rate_limiting():
    """Within the refresh window, a (symbol, tf) is not re-submitted."""
    loop = _loop()
    fake = _FakePool()
    loop._pool = fake
    loop._cycle()
    first = len(fake.calls)
    # 5 TFs in TF_MODULE_MAP for a single symbol.
    assert first == len(dc.TF_MODULE_MAP)
    loop._cycle()  # immediately again — all still within refresh window
    assert len(fake.calls) == first  # no new submissions


def test_merge_preserves_existing_tfs():
    loop = _loop()
    loop._merge_and_publish("EURUSD", "H1", {"structure": _sa("BULLISH", 0.7)})
    loop._merge_and_publish("EURUSD", "M5", {"structure": _sa("BEARISH", 0.6)})
    wm = loop._developing_store.get("EURUSD")
    struct = wm.structure_by_tf()
    assert "H1" in struct and "M5" in struct
    assert struct["H1"].trend == Trend.BULLISH
    assert struct["M5"].trend == Trend.BEARISH


def test_failure_isolation(monkeypatch):
    """A failing analysis increments the counter but never raises."""
    loop = _loop(df=_ohlcv_df(60))

    def _boom(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr("brain.developing_analysis.run_tf_modules", _boom)
    loop._analyze_one("EURUSD", "H1")  # must not raise
    assert loop._analyses_failed == 1
    assert loop._developing_store.get("EURUSD") is None


def test_analyze_one_empty_df_noop():
    loop = _loop(df=None)
    loop._analyze_one("EURUSD", "H1")
    assert loop._developing_store.get("EURUSD") is None
    assert loop._analyses_run == 0


def test_loop_start_stop():
    loop = _loop()
    loop.start()
    assert loop._running is True
    assert loop._thread is not None and loop._thread.is_alive()
    loop.stop()
    assert loop._running is False


def test_stats():
    loop = _loop()
    s = loop.stats()
    for key in (
        "running", "cycles", "analyses_run", "analyses_failed",
        "symbols_tracked", "developing_models",
    ):
        assert key in s
    assert s["symbols_tracked"] == 1

