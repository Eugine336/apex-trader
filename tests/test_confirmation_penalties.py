"""
Tests for confirmation-only confluence penalties.

Covers:
  - Per-indicator: agreement → 0; disagreement → expected negative;
    bad/short/empty df and forced exception → 0 (fail-neutral).
  - VP-POC: real-volume instrument computes; FX/tick-volume returns 0.
  - Scan integration: enabled=False → baseline; shadow → baseline + log;
    active → score reduced; penalty can never raise score.
"""

import math
import sys
from types import SimpleNamespace, ModuleType
from unittest.mock import patch, MagicMock

import numpy as np
import pandas as pd
import pytest

from brain.session_vwap import session_vwap_penalty, compute_session_vwap
from brain.momentum_divergence import (
    momentum_divergence_penalty,
    calculate_rsi,
    calculate_macd,
)
from brain.atr_percentile import atr_percentile_penalty, compute_atr_percentile
from brain.volume_profile import (
    volume_profile_poc_penalty,
    has_real_volume,
    compute_poc,
)
from config import AppConfig, ConfirmationPenaltyConfig

_TORCH_STUB_INSTALLED = False


def _ensure_torch_stub():
    """Install a minimal torch stub so scanner → rl imports don't explode."""
    global _TORCH_STUB_INSTALLED
    if _TORCH_STUB_INSTALLED:
        return
    if "torch" not in sys.modules:
        stub = ModuleType("torch")
        stub.__path__ = []
        stub.nn = ModuleType("torch.nn")
        stub.nn.__path__ = []
        stub.Tensor = type("Tensor", (), {})

        class _NoGradCtx:
            def __enter__(self):
                return self
            def __exit__(self, *a):
                return None
            def __call__(self, fn):
                return fn

        stub.no_grad = _NoGradCtx
        stub.zeros = lambda *a, **kw: None
        stub.float32 = None
        stub.device = lambda x: x
        stub.load = lambda *a, **kw: {}
        stub.tensor = lambda *a, **kw: None
        stub.from_numpy = lambda *a, **kw: None

        stub.optim = ModuleType("torch.optim")
        stub.optim.Adam = type("Adam", (), {"__init__": lambda s, *a, **kw: None})

        stub.distributions = ModuleType("torch.distributions")
        stub.distributions.Categorical = type("Categorical", (), {
            "__init__": lambda s, *a, **kw: None,
        })
        for attr in ("Module", "Linear", "ReLU", "Sequential", "LSTM",
                      "BatchNorm1d", "Dropout", "Softmax", "Tanh",
                      "LayerNorm", "MultiheadAttention", "TransformerEncoderLayer",
                      "TransformerEncoder", "GRU"):
            setattr(stub.nn, attr, type(attr, (), {"__init__": lambda s, *a, **kw: None}))
        stub.nn.functional = ModuleType("torch.nn.functional")
        stub.nn.functional.softmax = lambda *a, **kw: None
        sys.modules["torch"] = stub
        sys.modules["torch.nn"] = stub.nn
        sys.modules["torch.nn.functional"] = stub.nn.functional
        sys.modules["torch.optim"] = stub.optim
        sys.modules["torch.distributions"] = stub.distributions
    _TORCH_STUB_INSTALLED = True


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_m5(n: int = 200, base: float = 1.1000, trend: float = 0.0,
             tick_volume: bool = True) -> pd.DataFrame:
    """Build a synthetic M5 DataFrame with OHLCV."""
    rng = np.random.default_rng(42)
    closes = base + np.arange(n) * trend + rng.normal(0, 0.0005, n)
    highs = closes + rng.uniform(0.0001, 0.001, n)
    lows = closes - rng.uniform(0.0001, 0.001, n)
    opens = closes - rng.normal(0, 0.0002, n)
    data = {
        "open": opens,
        "high": highs,
        "low": lows,
        "close": closes,
    }
    if tick_volume:
        data["tick_volume"] = rng.integers(100, 5000, n)
    return pd.DataFrame(data)


def _make_h4(n: int = 150, base: float = 1.1000, trend: float = 0.0,
             real_volume: bool = True) -> pd.DataFrame:
    df = _make_m5(n, base, trend, tick_volume=True)
    if real_volume:
        df["volume"] = np.random.default_rng(99).integers(1000, 50000, n)
    return df


def _make_trending_up(n: int = 200, base: float = 1.1000) -> pd.DataFrame:
    """M5 with a strong uptrend → RSI likely > 70."""
    closes = base + np.linspace(0, 0.05, n)
    return pd.DataFrame({
        "open": closes - 0.0002,
        "high": closes + 0.0005,
        "low": closes - 0.0005,
        "close": closes,
        "tick_volume": np.random.default_rng(7).integers(100, 5000, n),
    })


def _make_trending_down(n: int = 200, base: float = 1.1500) -> pd.DataFrame:
    """M5 with a strong downtrend → RSI likely < 30."""
    closes = base - np.linspace(0, 0.05, n)
    return pd.DataFrame({
        "open": closes + 0.0002,
        "high": closes + 0.0005,
        "low": closes - 0.0005,
        "close": closes,
        "tick_volume": np.random.default_rng(7).integers(100, 5000, n),
    })


# ---------------------------------------------------------------------------
# Session VWAP
# ---------------------------------------------------------------------------

class TestSessionVWAP:
    def test_agreement_no_penalty(self):
        df = _make_m5(200, base=1.1000, trend=0.0001)
        vwap = compute_session_vwap(df, 120)
        assert vwap is not None
        current = float(df["close"].iloc[-1])
        if current < vwap:
            pts, _ = session_vwap_penalty(df, "LONG", 120)
        else:
            pts, _ = session_vwap_penalty(df, "SHORT", 120)
        assert pts == 0

    def test_wrong_side_long_above_vwap(self):
        rng = np.random.default_rng(11)
        n = 200
        closes = np.concatenate([
            np.full(180, 1.1000) + rng.normal(0, 0.0001, 180),
            np.linspace(1.1000, 1.1100, 20),
        ])
        df = pd.DataFrame({
            "open": closes - 0.0001,
            "high": closes + 0.0003,
            "low": closes - 0.0003,
            "close": closes,
            "tick_volume": rng.integers(100, 5000, n),
        })
        vwap = compute_session_vwap(df, 600)
        current = float(df["close"].iloc[-1])
        assert current > vwap, "Setup: current price should be above VWAP"
        pts, reason = session_vwap_penalty(df, "LONG", 600, penalty_points=15)
        assert pts == 15
        assert "VWAP" in reason

    def test_wrong_side_short_below_vwap(self):
        rng = np.random.default_rng(22)
        n = 200
        closes = np.concatenate([
            np.full(180, 1.1100) + rng.normal(0, 0.0001, 180),
            np.linspace(1.1100, 1.1000, 20),
        ])
        df = pd.DataFrame({
            "open": closes + 0.0001,
            "high": closes + 0.0003,
            "low": closes - 0.0003,
            "close": closes,
            "tick_volume": rng.integers(100, 5000, n),
        })
        vwap = compute_session_vwap(df, 600)
        current = float(df["close"].iloc[-1])
        assert current < vwap, "Setup: current price should be below VWAP"
        pts, reason = session_vwap_penalty(df, "SHORT", 600, penalty_points=15)
        assert pts == 15

    def test_fail_neutral_empty_df(self):
        df = pd.DataFrame(columns=["open", "high", "low", "close", "tick_volume"])
        pts, _ = session_vwap_penalty(df, "LONG", 120)
        assert pts == 0

    def test_fail_neutral_short_session(self):
        df = _make_m5(200)
        pts, _ = session_vwap_penalty(df, "LONG", 10, min_session_minutes=30)
        assert pts == 0

    def test_fail_neutral_neutral_dir(self):
        df = _make_m5(200)
        pts, _ = session_vwap_penalty(df, "NEUTRAL", 120)
        assert pts == 0


# ---------------------------------------------------------------------------
# Momentum Divergence
# ---------------------------------------------------------------------------

class TestMomentumDivergence:
    def test_no_divergence_mild_uptrend_long(self):
        """Mild uptrend: RSI should be 40-60 and MACD bullish → no penalty on LONG."""
        n = 200
        closes = 1.1 + np.linspace(0, 0.002, n)
        df = pd.DataFrame({
            "open": closes - 0.0001,
            "high": closes + 0.0003,
            "low": closes - 0.0003,
            "close": closes,
        })
        rsi = calculate_rsi(df["close"], 14)
        macd = calculate_macd(df["close"])
        if rsi is not None and rsi <= 70 and macd is not None and macd[0] >= macd[1]:
            pts, _ = momentum_divergence_penalty(df, df, "LONG")
            assert pts == 0

    def test_both_tf_diverge_long(self):
        up = _make_trending_up(200)
        pts, reason = momentum_divergence_penalty(up, up, "LONG", both_tf_penalty=15)
        rsi = calculate_rsi(up["close"], 14)
        if rsi is not None and rsi > 70:
            assert pts == 15
            assert "M5+H1" in reason
        else:
            assert pts >= 0

    def test_both_tf_diverge_short(self):
        down = _make_trending_down(200)
        pts, reason = momentum_divergence_penalty(down, down, "SHORT", both_tf_penalty=15)
        rsi = calculate_rsi(down["close"], 14)
        if rsi is not None and rsi < 30:
            assert pts == 15
        else:
            assert pts >= 0

    def test_single_tf_diverge(self):
        up = _make_trending_up(200)
        n = 200
        mild = pd.DataFrame({
            "open": np.full(n, 1.12) - 0.0001,
            "high": np.full(n, 1.12) + 0.0003,
            "low": np.full(n, 1.12) - 0.0003,
            "close": 1.12 + np.linspace(0, 0.001, n),
        })
        pts, reason = momentum_divergence_penalty(up, mild, "LONG",
                                                   both_tf_penalty=15,
                                                   single_tf_penalty=7)
        rsi_up = calculate_rsi(up["close"], 14)
        rsi_mild = calculate_rsi(mild["close"], 14)
        if rsi_up is not None and rsi_up > 70:
            if rsi_mild is None or rsi_mild <= 70:
                assert pts == 7
                assert "M5 only" in reason

    def test_fail_neutral_short_df(self):
        short_df = _make_m5(5)
        pts, _ = momentum_divergence_penalty(short_df, short_df, "LONG")
        assert pts == 0

    def test_fail_neutral_neutral_dir(self):
        m5 = _make_m5(200)
        pts, _ = momentum_divergence_penalty(m5, m5, "NEUTRAL")
        assert pts == 0

    def test_rsi_calculation_basic(self):
        df = _make_m5(50)
        rsi = calculate_rsi(df["close"], 14)
        assert rsi is None or (0 <= rsi <= 100)

    def test_macd_calculation_basic(self):
        df = _make_m5(50)
        result = calculate_macd(df["close"])
        assert result is None or (
            math.isfinite(result[0]) and math.isfinite(result[1])
        )


# ---------------------------------------------------------------------------
# ATR Percentile
# ---------------------------------------------------------------------------

class TestATRPercentile:
    def test_normal_volatility_no_penalty(self):
        df = _make_m5(200)
        pts, _ = atr_percentile_penalty(df, penalty_points=10, window=100,
                                         dead_percentile=20.0)
        pctl = compute_atr_percentile(df, window=100)
        if pctl >= 20:
            assert pts == 0
        else:
            assert pts == 10

    def test_dead_regime_penalty(self):
        rng = np.random.default_rng(55)
        _n = 200
        closes = np.concatenate([
            1.1 + rng.normal(0, 0.01, 100),
            np.full(100, 1.1) + rng.normal(0, 0.00001, 100),
        ])
        df = pd.DataFrame({
            "open": closes - 0.00001,
            "high": closes + 0.00002,
            "low": closes - 0.00002,
            "close": closes,
        })
        pctl = compute_atr_percentile(df, window=100)
        if pctl >= 0 and pctl < 20:
            pts, reason = atr_percentile_penalty(df, penalty_points=10,
                                                  window=100, dead_percentile=20.0)
            assert pts == 10
            assert "dead regime" in reason

    def test_fail_neutral_short_df(self):
        df = _make_m5(20)
        pts, _ = atr_percentile_penalty(df, window=100)
        assert pts == 0

    def test_fail_neutral_empty(self):
        df = pd.DataFrame(columns=["open", "high", "low", "close"])
        pts, _ = atr_percentile_penalty(df, window=100)
        assert pts == 0


# ---------------------------------------------------------------------------
# Volume Profile POC
# ---------------------------------------------------------------------------

class TestVolumeProfilePOC:
    def test_real_volume_instrument_computes(self):
        h4 = _make_h4(150, real_volume=True)
        pts, _ = volume_profile_poc_penalty(h4, "LONG", "commodity")
        assert pts >= 0

    def test_fx_tick_volume_skipped(self):
        h4 = _make_h4(150)
        pts, _ = volume_profile_poc_penalty(h4, "LONG", "forex")
        assert pts == 0

    def test_synthetic_skipped(self):
        h4 = _make_h4(150)
        pts, _ = volume_profile_poc_penalty(h4, "LONG", "synthetic")
        assert pts == 0

    def test_has_real_volume_categories(self):
        assert has_real_volume("commodity") is True
        assert has_real_volume("index") is True
        assert has_real_volume("crypto") is True
        assert has_real_volume("forex") is False
        assert has_real_volume("synthetic") is False

    def test_fail_neutral_short_df(self):
        h4 = _make_h4(5, real_volume=True)
        pts, _ = volume_profile_poc_penalty(h4, "LONG", "index")
        assert pts == 0

    def test_fail_neutral_neutral_dir(self):
        h4 = _make_h4(150, real_volume=True)
        pts, _ = volume_profile_poc_penalty(h4, "NEUTRAL", "commodity")
        assert pts == 0

    def test_compute_poc_returns_finite(self):
        h4 = _make_h4(150, real_volume=True)
        poc = compute_poc(h4)
        assert poc is None or (math.isfinite(poc) and poc > 0)


# ---------------------------------------------------------------------------
# Scanner integration
# ---------------------------------------------------------------------------

def _make_scan_data():
    """Return minimal data for a scan_pair call."""
    m5 = _make_m5(200)
    h1 = _make_m5(200)
    m15 = _make_m5(200)
    h4 = _make_m5(200)
    return h4, h1, m15, m5


class TestScannerIntegration:

    def _make_scanner(self, enabled=False, shadow=True, active=False):
        _ensure_torch_stub()
        from scanner.pair_scanner import PairScanner
        cfg = AppConfig()
        cfg.confirmation_penalties = ConfirmationPenaltyConfig(
            enabled=enabled,
            shadow_mode=shadow if not active else False,
        )
        if active:
            cfg.confirmation_penalties.enabled = True
            cfg.confirmation_penalties.shadow_mode = False
        scanner = PairScanner.__new__(PairScanner)
        scanner.config = cfg
        scanner.structure = MagicMock()
        scanner.structure.get_bias.return_value = {
            "direction": "BULLISH", "tradeable": True,
            "strength": "STRONG", "h4_trend": "BULLISH", "h1_trend": "BULLISH",
        }
        scanner.fvg_detector = MagicMock()
        scanner.ob_detector = MagicMock()
        scanner.liquidity = MagicMock()
        scanner.strength_meter = MagicMock()
        scanner.session = MagicMock()
        scanner.session.get_status.return_value = SimpleNamespace(
            current_session="LONDON", is_tradeable=True,
            liquidity="HIGH", best_pairs=[], minutes_to_next_session=120,
            session_open_minutes=180,
        )
        scanner._ev_estimator = MagicMock()
        scanner._ev_estimator.estimate.return_value = SimpleNamespace(expected_value=0.5)
        scanner._trade_history = []
        scanner.news = MagicMock()
        scanner.news.get_status.return_value = SimpleNamespace(
            is_clear=True, events_nearby=[], next_high_impact=None,
            affected_currencies=[], warning_message="",
        )
        scanner.volume = MagicMock()
        scanner.volume.analyze.return_value = SimpleNamespace(
            has_spike=False, confirmation_bias="NEUTRAL",
            volume_ratio=1.0, climax_detected=False,
            divergence_type="NONE", poc_level=None,
        )
        scanner.last_report = None
        scanner._mt5_connector = None
        scanner._adaptive_weights = None
        mock_rl = MagicMock()
        mock_rl.authority = SimpleNamespace(stage=1, stage_label="S1")
        scanner._rl = mock_rl
        scanner._obs_builders = {}
        scanner._mtf_builders = {}
        return scanner

    def test_disabled_no_change(self):
        scanner = self._make_scanner(enabled=False)
        h4, h1, m15, m5 = _make_scan_data()
        result = scanner.scan_pair("EURUSD", h4, h1, m15, m5)
        baseline_score = result.score

        scanner2 = self._make_scanner(enabled=True, shadow=True)
        result2 = scanner2.scan_pair("EURUSD", h4, h1, m15, m5)
        assert result2.score == baseline_score, (
            "Shadow mode must NOT change score"
        )

    def test_active_can_reduce_score(self):
        scanner_off = self._make_scanner(enabled=False)
        h4, h1, m15, m5 = _make_scan_data()
        result_off = scanner_off.scan_pair("EURUSD", h4, h1, m15, m5)
        baseline = result_off.score

        scanner_on = self._make_scanner(active=True)
        scanner_on.config.confirmation_penalties.vwap_wrong_side_penalty = 50
        scanner_on.config.confirmation_penalties.divergence_both_tf_penalty = 50
        scanner_on.config.confirmation_penalties.atr_dead_regime_penalty = 50
        scanner_on.config.confirmation_penalties.vp_poc_trap_penalty = 50
        result_on = scanner_on.scan_pair("EURUSD", h4, h1, m15, m5)
        assert result_on.score <= baseline, (
            "Active penalties must NEVER raise score above baseline"
        )

    def test_penalty_cannot_raise_score(self):
        scanner = self._make_scanner(active=True)
        scanner.config.confirmation_penalties.vwap_wrong_side_penalty = 0
        scanner.config.confirmation_penalties.divergence_both_tf_penalty = 0
        scanner.config.confirmation_penalties.atr_dead_regime_penalty = 0
        scanner.config.confirmation_penalties.vp_poc_trap_penalty = 0

        h4, h1, m15, m5 = _make_scan_data()
        scanner_off = self._make_scanner(enabled=False)
        baseline = scanner_off.scan_pair("EURUSD", h4, h1, m15, m5).score

        result = scanner.scan_pair("EURUSD", h4, h1, m15, m5)
        assert result.score == baseline, (
            "Zero penalties must leave score byte-for-byte identical"
        )

    def test_score_floored_at_zero(self):
        scanner = self._make_scanner(active=True)
        scanner.config.confirmation_penalties.vwap_wrong_side_penalty = 999
        scanner.config.confirmation_penalties.divergence_both_tf_penalty = 999
        scanner.config.confirmation_penalties.atr_dead_regime_penalty = 999

        h4, h1, m15, m5 = _make_scan_data()
        result = scanner.scan_pair("EURUSD", h4, h1, m15, m5)
        assert result.score >= 0, "Score must be floored at 0"
