"""Developing-analysis tests for raw, non-directional WorldModel facts.

Covers forming-bar handling, the DevelopingAnalysisLoop (analysis, rate limiting,
merge, isolation, lifecycle and stats), and publication of confirmed structure.
The legacy ``compute_bias`` helper was intentionally removed because it
synthesized direction outside the CognitiveBrain; these tests must not restore it.
"""

from types import SimpleNamespace

import pandas as pd

import brain.decision_core as dc
from brain.decision_core import run_tf_modules
from brain.developing_analysis import DevelopingAnalysisLoop
from brain.structure_engine import StructureAnalysis, StructureEvent, Trend
from brain.world_model import WorldModelStore, build_world_model
from config import DevelopingAnalysisConfig


# ── Helpers ───────────────────────────────────────────────────────────────


def _sa(trend: str, confidence: float) -> StructureAnalysis:
    """Minimal StructureAnalysis for developing-analysis tests."""
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


# ── Legacy directional helper retirement ─────────────────────────────────


def test_legacy_direction_calculator_remains_retired():
    """No subsystem outside CognitiveBrain should synthesize market direction."""
    assert not hasattr(dc, "compute_bias")


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
    # The refresh windows may make only a subset of TFs due on the first
    # cycle; the invariant is that at least one is scheduled, then rate-limited.
    assert 0 < first <= len(dc.TF_MODULE_MAP)
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


# ── CandleCloseHandler publishes raw structural facts ───────────────────────


def test_candle_handler_publishes_structure_without_directional_bias():
    """The handler publishes source facts; CognitiveBrain alone forms direction."""
    from datetime import datetime, timezone

    from scanner.candle_close_handler import CandleCloseHandler
    from tick.event_bus import EventBus

    confirmed_store = WorldModelStore()
    handler = CandleCloseHandler(
        EventBus(),
        confirmed_store,
        lambda symbol, timeframe, count: pd.DataFrame(),
        max_workers=1,
    )
    handler._merge_and_publish_locked(
        "EURUSD", "H1",
        {"structure": _sa("BULLISH", 0.8)},
        datetime.now(timezone.utc),
    )
    handler._merge_and_publish_locked(
        "EURUSD", "H4",
        {"structure": _sa("BULLISH", 0.8)},
        datetime.now(timezone.utc),
    )

    model = confirmed_store.get("EURUSD")
    assert model is not None
    structure = model.structure_by_tf()
    assert {"H1", "H4"} <= set(structure)
    assert structure["H1"].trend == Trend.BULLISH
    assert structure["H4"].trend == Trend.BULLISH

    alignment = model.multi_tf_alignment_dict()
    forbidden = {
        "direction", "score", "signed_score", "long_probability",
        "short_probability", "dominant", "bias", "lean", "signal", "vote",
    }
    assert not forbidden.intersection(alignment)
