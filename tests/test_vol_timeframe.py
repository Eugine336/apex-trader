"""
Tests for the intraday-responsive system volatility timeframe selection
and the responsiveness premise (a recent spike is detectable on M15-scale data).
"""

import sys
import types
from unittest.mock import MagicMock

import numpy as np
import pandas as pd
import pytest

# Stub torch before any transitive import can pull it in (rl → torch).
# Use a single MagicMock with __path__ so Python treats it as a real package
# and resolves all sub-module imports (torch.nn, torch.distributions, etc.).
if "torch" not in sys.modules:
    _torch = MagicMock()
    _torch.__path__ = []
    _torch.__file__ = "mock"
    for _sub in (
        "torch", "torch.nn", "torch.nn.functional", "torch.optim",
        "torch.distributions", "torch.cuda", "torch.utils",
        "torch.utils.data",
    ):
        sys.modules[_sub] = _torch

from brain.regime_detector import RegimeDetector
from platforms.main_loop import TradingLoop


# ─── Helpers ────────────────────────────────────────────────────────────────

def _make_ohlc(n: int, base_close: float = 1.0, atr_frac: float = 0.005) -> pd.DataFrame:
    """Build a calm synthetic OHLC DataFrame with *n* rows."""
    closes = np.full(n, base_close)
    highs = closes + atr_frac * base_close
    lows = closes - atr_frac * base_close
    return pd.DataFrame({"open": closes, "high": highs, "low": lows, "close": closes})


def _call_select(frames: dict):
    """Call TradingLoop._select_vol_timeframe as an unbound method."""
    stub = types.SimpleNamespace()
    return TradingLoop._select_vol_timeframe(stub, frames)


# ─── _select_vol_timeframe tests ────────────────────────────────────────────

class TestSelectVolTimeframe:

    def test_prefers_m15_when_valid(self):
        m15 = _make_ohlc(60)
        h1 = _make_ohlc(60)
        h4 = _make_ohlc(60)
        assert _call_select({"M15": m15, "H1": h1, "H4": h4}) is m15

    def test_falls_back_to_h1_when_m15_missing(self):
        h1 = _make_ohlc(60)
        h4 = _make_ohlc(60)
        assert _call_select({"H1": h1, "H4": h4}) is h1

    def test_falls_back_to_h1_when_m15_too_short(self):
        m15_short = _make_ohlc(30)
        h1 = _make_ohlc(60)
        assert _call_select({"M15": m15_short, "H1": h1}) is h1

    def test_falls_back_to_h4_when_m15_and_h1_missing(self):
        h4 = _make_ohlc(60)
        assert _call_select({"H4": h4}) is h4

    def test_returns_none_when_all_missing(self):
        assert _call_select({}) is None

    def test_returns_none_when_all_too_short(self):
        result = _call_select({
            "M15": _make_ohlc(10),
            "H1": _make_ohlc(20),
            "H4": _make_ohlc(40),
        })
        assert result is None


# ─── Responsiveness regression guard ────────────────────────────────────────

class TestSpikeResponsiveness:

    def test_recent_spike_detected_on_short_window(self):
        """A spike in the final bars of a short (M15-scale) window crosses
        the SPIKE threshold (volatility_ratio > 1.8)."""
        n = 70
        base = 1.0
        calm_atr_frac = 0.003
        spike_atr_frac = 0.025

        closes = np.full(n, base)
        highs = closes + calm_atr_frac * base
        lows = closes - calm_atr_frac * base

        spike_start = n - 5
        highs[spike_start:] = base + spike_atr_frac * base
        lows[spike_start:] = base - spike_atr_frac * base

        df = pd.DataFrame({"open": closes, "high": highs, "low": lows, "close": closes})
        analysis = RegimeDetector().analyze(df)
        assert analysis.volatility_ratio > 1.8, (
            f"Expected volatility_ratio > 1.8 on short-window spike, got {analysis.volatility_ratio}"
        )

    def test_calm_market_not_flagged_as_spike(self):
        """A calm frame with no spike stays well below the SPIKE threshold,
        confirming detection is real and not a false positive."""
        n = 70
        base = 1.0
        calm_atr_frac = 0.003

        closes = np.full(n, base)
        highs = closes + calm_atr_frac * base
        lows = closes - calm_atr_frac * base

        df = pd.DataFrame({"open": closes, "high": highs, "low": lows, "close": closes})
        analysis = RegimeDetector().analyze(df)
        assert analysis.volatility_ratio < 1.4, (
            f"Expected volatility_ratio < 1.4 on calm market, got {analysis.volatility_ratio}"
        )
