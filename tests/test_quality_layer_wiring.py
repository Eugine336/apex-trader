"""Tests for the wired-in setup-quality layer and SystemVolatilityMonitor.

Covers the orphaned brain modules that are now wired into BOTH the live and
backtest planes via the shared ``brain.quality_layer``:

* Real Opportunity/Entry Quality scoring (``brain.setup_quality``)
* ATR-percentile volatility scoring (``brain.atr_percentile``)
* RSI/MACD momentum-divergence penalty (``brain.momentum_divergence``)
* Cross-instrument ``SystemVolatilityMonitor`` sizing gate
"""

from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from brain.quality_layer import (
    compute_quality_layer,
    compute_regime_analysis,
    entry_quality_for,
)
from brain.regime_detector import RegimeAnalysis, MarketRegime, SystemVolatilityMonitor
from brain.world_model import WorldModel, build_world_model


def _frame(n: int, base: float = 1.10, vol: float = 0.0008, seed: int = 0) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    closes = base + np.cumsum(rng.normal(0, vol, n))
    highs = closes + abs(rng.normal(0, vol, n))
    lows = closes - abs(rng.normal(0, vol, n))
    opens = closes - rng.normal(0, vol, n)
    return pd.DataFrame({
        "time": pd.date_range("2024-01-01", periods=n, freq="h", tz="UTC"),
        "open": opens, "high": highs, "low": lows, "close": closes,
        "volume": rng.integers(80, 200, n).astype(float),
    })


def _empty_wm() -> WorldModel:
    import datetime as _dt
    return build_world_model(symbol="EURUSD", version=1, timestamp=_dt.datetime.now(_dt.timezone.utc))


# ── WorldModel carries the new quality fields ────────────────────────────


def test_worldmodel_quality_fields_default_none():
    wm = _empty_wm()
    assert wm.opportunity_quality is None
    assert wm.entry_quality_long is None
    assert wm.entry_quality_short is None
    assert wm.regime_analysis is None


# ── compute_quality_layer ────────────────────────────────────────────────


def test_compute_quality_layer_returns_scores():
    wm = _empty_wm()
    m5 = _frame(120, seed=1)
    h1 = _frame(120, seed=2)
    out = compute_quality_layer("EURUSD", wm, m5_df=m5, h1_df=h1, current_price=1.10)
    assert set(out) == {
        "opportunity_quality", "entry_quality_long",
        "entry_quality_short", "regime_analysis",
    }
    assert 0.0 <= out["opportunity_quality"] <= 10.0
    assert 0.0 <= out["entry_quality_long"] <= 10.0
    assert 0.0 <= out["entry_quality_short"] <= 10.0


def test_compute_quality_layer_is_defensive_on_empty_inputs():
    wm = _empty_wm()
    out = compute_quality_layer("EURUSD", wm, m5_df=None, h1_df=None, current_price=0.0)
    # No candles → regime cannot be computed, but OQ still scores via defaults.
    assert out["regime_analysis"] is None
    assert out["opportunity_quality"] is None or 0.0 <= out["opportunity_quality"] <= 10.0


def test_regime_analysis_computed_with_enough_bars():
    ra = compute_regime_analysis(_frame(120, seed=3))
    assert ra is not None
    assert ra.volatility_ratio >= 0.0


# ── entry_quality_for direction routing ──────────────────────────────────


def test_entry_quality_for_routes_by_direction():
    wm = SimpleNamespace(entry_quality_long=6.5, entry_quality_short=3.5)
    assert entry_quality_for(wm, "LONG") == pytest.approx(6.5)
    assert entry_quality_for(wm, "BUY") == pytest.approx(6.5)
    assert entry_quality_for(wm, "SHORT") == pytest.approx(3.5)
    assert entry_quality_for(SimpleNamespace(), "LONG") is None


# ── SystemVolatilityMonitor applied as a sizing gate ──────────────────────


def _spike_analysis() -> RegimeAnalysis:
    return RegimeAnalysis(
        regime=MarketRegime.VOLATILE, atr_current=0.002, atr_average=0.001,
        volatility_ratio=2.5, directional_strength=0.1, confidence=0.9,
        tradeable=False,
    )


def test_monitor_spike_cuts_size_multiplier():
    mon = SystemVolatilityMonitor()
    mon.update([_spike_analysis()])
    assert mon.get_size_multiplier() == SystemVolatilityMonitor.SPIKE_MULTIPLIER
