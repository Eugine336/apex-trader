"""Tests for dynamic consensus voting weights (brain/dynamic_weights.py).

Covers the DynamicWeightProvider math (regime / volatility / recency scaling,
composition, clamps, master switch, fail-safe), the build_consensus
integration (provider applied at vote-construction time, behaviour-neutral when
absent or disabled), the decision_core context helpers, and the
DynamicWeightConfig validation.
"""

from __future__ import annotations

import math

import pytest

from brain.dynamic_weights import DynamicWeightProvider, _normalize_regime
from brain.decision_core import (
    build_consensus,
    _pick_consensus_regime,
    _volatility_ratio_from_df,
)
from config import DynamicWeightConfig, _DEFAULT_CONSENSUS_WEIGHTS


# Base weights: structure (HTF) + momentum (LTF) + an unclassified module.
_BASE = {"structure": 1.0, "momentum": 1.0, "mystery": 1.0}


def _cfg(**kw) -> DynamicWeightConfig:
    return DynamicWeightConfig(
        htf_modules=["structure"],
        ltf_modules=["momentum"],
        **kw,
    )


# ---------------------------------------------------------------------------
# Regime label normalisation
# ---------------------------------------------------------------------------


class TestRegimeNormalization:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("TREND", "trending"),
            ("TRENDING", "trending"),
            ("TRENDING_UP", "trending"),
            ("TRENDING_DOWN", "trending"),
            ("RANGE", "ranging"),
            ("RANGING", "ranging"),
            ("VOLATILE", "volatile"),
            ("UNKNOWN", "neutral"),
            ("QUIET", "neutral"),
            ("", "neutral"),
            (None, "neutral"),
            ("garbage", "neutral"),
        ],
    )
    def test_normalize(self, raw, expected):
        assert _normalize_regime(raw) == expected


# ---------------------------------------------------------------------------
# Regime scaling
# ---------------------------------------------------------------------------


class TestRegimeScaling:
    def test_trending_favours_htf(self):
        p = DynamicWeightProvider(_BASE, _cfg())
        w = p.compute_weights("X", "TREND", 1.0, True)
        assert w["structure"] == pytest.approx(1.3)   # HTF boosted
        assert w["momentum"] == pytest.approx(0.8)     # LTF dampened
        assert w["mystery"] == pytest.approx(1.0)      # unclassified untouched

    def test_ranging_favours_ltf(self):
        p = DynamicWeightProvider(_BASE, _cfg())
        w = p.compute_weights("X", "RANGE", 1.0, True)
        assert w["structure"] == pytest.approx(0.8)
        assert w["momentum"] == pytest.approx(1.3)

    def test_volatile_leans_ltf(self):
        p = DynamicWeightProvider(_BASE, _cfg())
        # vol_ratio kept normal (1.0) to isolate the regime factor.
        w = p.compute_weights("X", "VOLATILE", 1.0, True)
        assert w["structure"] == pytest.approx(0.9)
        assert w["momentum"] == pytest.approx(1.2)

    def test_neutral_regime_no_bias(self):
        p = DynamicWeightProvider(_BASE, _cfg())
        w = p.compute_weights("X", "UNKNOWN", 1.0, True)
        assert w["structure"] == pytest.approx(1.0)
        assert w["momentum"] == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# Volatility scaling
# ---------------------------------------------------------------------------


class TestVolatilityScaling:
    def test_high_vol_favours_ltf(self):
        p = DynamicWeightProvider(_BASE, _cfg())
        # Neutral regime so only volatility moves the weights.
        w = p.compute_weights("X", "UNKNOWN", 1.6, True)
        assert w["structure"] == pytest.approx(0.9)   # HTF dampened
        assert w["momentum"] == pytest.approx(1.2)     # LTF boosted

    def test_low_vol_favours_htf(self):
        p = DynamicWeightProvider(_BASE, _cfg())
        w = p.compute_weights("X", "UNKNOWN", 0.5, True)
        assert w["structure"] == pytest.approx(1.2)
        assert w["momentum"] == pytest.approx(0.9)

    def test_normal_vol_no_bias(self):
        p = DynamicWeightProvider(_BASE, _cfg())
        w = p.compute_weights("X", "UNKNOWN", 1.0, True)
        assert w["structure"] == pytest.approx(1.0)
        assert w["momentum"] == pytest.approx(1.0)

    @pytest.mark.parametrize("bad", [0.0, -1.0, float("nan"), float("inf"), "x", None])
    def test_bad_vol_ratio_is_neutral(self, bad):
        p = DynamicWeightProvider(_BASE, _cfg())
        w = p.compute_weights("X", "UNKNOWN", bad, True)
        # Bad ratio → treated as 1.0 (normal) → no volatility bias.
        assert w["structure"] == pytest.approx(1.0)
        assert w["momentum"] == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# Recency scaling
# ---------------------------------------------------------------------------


class TestRecencyScaling:
    def test_confirmed_full_weight(self):
        p = DynamicWeightProvider(_BASE, _cfg())
        w = p.compute_weights("X", "UNKNOWN", 1.0, True)
        assert w["structure"] == pytest.approx(1.0)
        assert w["momentum"] == pytest.approx(1.0)
        assert w["mystery"] == pytest.approx(1.0)

    def test_developing_discounted_uniformly(self):
        p = DynamicWeightProvider(_BASE, _cfg(developing_weight_mult=0.85))
        w = p.compute_weights("X", "UNKNOWN", 1.0, False)
        # Recency applies to EVERY module, including the unclassified one.
        assert w["structure"] == pytest.approx(0.85)
        assert w["momentum"] == pytest.approx(0.85)
        assert w["mystery"] == pytest.approx(0.85)


# ---------------------------------------------------------------------------
# Composition (all three factors together)
# ---------------------------------------------------------------------------


class TestComposition:
    def test_full_composition(self):
        p = DynamicWeightProvider(_BASE, _cfg())
        # Ranging + high vol + developing:
        #   HTF: 1.0 × 0.8 (ranging) × 0.9 (high-vol) × 0.85 (developing) = 0.612
        #   LTF: 1.0 × 1.3 (ranging) × 1.2 (high-vol) × 0.85 (developing) = 1.326
        w = p.compute_weights("X", "RANGE", 1.6, False)
        assert w["structure"] == pytest.approx(0.612, abs=1e-6)
        assert w["momentum"] == pytest.approx(1.326, abs=1e-6)
        # Unclassified: regime/vol = 1.0, recency only.
        assert w["mystery"] == pytest.approx(0.85, abs=1e-6)

    def test_base_weight_scales_through(self):
        base = {"structure": 2.0, "momentum": 0.5, "mystery": 3.0}
        p = DynamicWeightProvider(base, _cfg())
        w = p.compute_weights("X", "TREND", 1.0, True)
        assert w["structure"] == pytest.approx(2.0 * 1.3)
        assert w["momentum"] == pytest.approx(0.5 * 0.8)
        assert w["mystery"] == pytest.approx(3.0 * 1.0)


# ---------------------------------------------------------------------------
# Clamps
# ---------------------------------------------------------------------------


class TestClamps:
    def test_upper_clamp(self):
        base = {"structure": 10.0}
        p = DynamicWeightProvider(base, _cfg(max_weight=5.0))
        # 10.0 × 1.3 (trending) = 13.0 → clamped to 5.0
        w = p.compute_weights("X", "TREND", 1.0, True)
        assert w["structure"] == pytest.approx(5.0)

    def test_lower_clamp(self):
        base = {"momentum": 0.5}
        p = DynamicWeightProvider(
            base, _cfg(min_weight=0.3, ranging_ltf_mult=0.8, developing_weight_mult=0.5),
        )
        # 0.5 × 0.8 (ranging LTF) × 0.5 (developing) = 0.2 → clamped to 0.3
        w = p.compute_weights("X", "RANGE", 1.0, False)
        assert w["momentum"] == pytest.approx(0.3)


# ---------------------------------------------------------------------------
# Master switch + fail-safe
# ---------------------------------------------------------------------------


class TestMasterSwitch:
    def test_disabled_returns_base_unchanged(self):
        p = DynamicWeightProvider(_BASE, _cfg(enabled=False))
        w = p.compute_weights("X", "TREND", 2.0, False)
        assert w == _BASE

    def test_empty_base_returns_empty(self):
        p = DynamicWeightProvider({}, _cfg())
        assert p.compute_weights("X", "TREND", 1.0, True) == {}


class TestFailSafe:
    def test_provider_never_raises_on_bad_config(self):
        # A config missing the multiplier attributes must not propagate an
        # AttributeError into the consensus path — it falls back to base.
        class _BadCfg:
            enabled = True
            htf_modules = ["structure"]
            ltf_modules = ["momentum"]
            # deliberately missing every *_mult / threshold / clamp attribute

        p = DynamicWeightProvider(_BASE, _BadCfg())
        w = p.compute_weights("X", "TREND", 1.0, True)
        assert w == _BASE

    def test_non_finite_base_weights_dropped(self):
        base = {"structure": float("nan"), "momentum": float("inf"), "ok": 1.0, "neg": -2.0}
        p = DynamicWeightProvider(base, _cfg())
        # Only the finite, non-negative weight survives construction.
        assert set(p.base_weights) == {"ok"}


# ---------------------------------------------------------------------------
# decision_core context helpers
# ---------------------------------------------------------------------------


class TestContextHelpers:
    def test_pick_regime_prefers_h1(self):
        class _WM:
            def regime_by_tf(self):
                return {"M5": "RANGE", "H1": "TREND"}

        assert _pick_consensus_regime(_WM()) == "TREND"

    def test_pick_regime_falls_back_to_m5(self):
        class _WM:
            def regime_by_tf(self):
                return {"M5": "VOLATILE"}

        assert _pick_consensus_regime(_WM()) == "VOLATILE"

    def test_pick_regime_empty_when_none(self):
        class _WM:
            def regime_by_tf(self):
                return {}

        assert _pick_consensus_regime(_WM()) == ""

    def test_pick_regime_guarded_against_exception(self):
        class _WM:
            def regime_by_tf(self):
                raise RuntimeError("boom")

        assert _pick_consensus_regime(_WM()) == ""

    def test_volatility_ratio_none_df(self):
        assert _volatility_ratio_from_df(None) == 1.0

    def test_volatility_ratio_short_df(self):
        import pandas as pd

        df = pd.DataFrame({"high": [1, 2], "low": [0, 1]})
        assert _volatility_ratio_from_df(df) == 1.0

    def test_volatility_ratio_expansion(self):
        import pandas as pd

        # 60 flat-range bars then a burst of wide-range bars → recent ATR > base.
        highs = [1.0] * 55 + [5.0] * 10
        lows = [0.9] * 55 + [0.0] * 10
        df = pd.DataFrame({"high": highs, "low": lows})
        ratio = _volatility_ratio_from_df(df)
        assert ratio > 1.0


# ---------------------------------------------------------------------------
# build_consensus integration
# ---------------------------------------------------------------------------


class _FakeWM:
    """Minimal duck-typed WorldModel for build_consensus — bullish structure."""

    def __init__(self, regime: str = "TREND") -> None:
        self._regime = regime

    def bias_dict(self) -> dict:
        return {"direction": "LONG", "confidence": 0.8}

    def volume_by_tf(self) -> dict:
        class _Vol:
            confirmation_bias = "BULLISH"
            volume_ratio = 3.0
            has_spike = True

        return {"M5": _Vol()}

    def wyckoff_by_tf(self) -> dict:
        return {}

    def inducement_by_tf(self) -> dict:
        return {}

    def all_order_blocks(self):
        return []

    def all_fvgs(self):
        return []

    def regime_by_tf(self) -> dict:
        return {"H1": self._regime}


def _vote_map(votes) -> dict:
    return {v.module: v for v in votes}


class TestBuildConsensusIntegration:
    def test_no_provider_uses_static_weights(self):
        votes, _ = build_consensus("EURUSD", _FakeWM(), 1.10)
        vm = _vote_map(votes)
        assert vm["structure"].weight == pytest.approx(1.0)

    def test_provider_applied_trending(self):
        p = DynamicWeightProvider(
            _DEFAULT_CONSENSUS_WEIGHTS,
            DynamicWeightConfig(htf_modules=["structure"], ltf_modules=["volume"]),
        )
        votes, _ = build_consensus(
            "EURUSD", _FakeWM("TREND"), 1.10,
            dynamic_weight_provider=p, is_confirmed=True,
        )
        vm = _vote_map(votes)
        # structure is HTF → boosted in a trend; volume is LTF → dampened.
        assert vm["structure"].weight == pytest.approx(1.3)
        assert vm["volume"].weight == pytest.approx(0.8)

    def test_provider_disabled_is_neutral(self):
        p = DynamicWeightProvider(
            _DEFAULT_CONSENSUS_WEIGHTS,
            DynamicWeightConfig(enabled=False),
        )
        votes, _ = build_consensus(
            "EURUSD", _FakeWM("TREND"), 1.10,
            dynamic_weight_provider=p, is_confirmed=True,
        )
        vm = _vote_map(votes)
        assert vm["structure"].weight == pytest.approx(1.0)
        assert vm["volume"].weight == pytest.approx(1.0)

    def test_developing_discounts_all(self):
        p = DynamicWeightProvider(
            _DEFAULT_CONSENSUS_WEIGHTS,
            DynamicWeightConfig(
                htf_modules=["structure"], ltf_modules=["volume"],
                developing_weight_mult=0.85,
            ),
        )
        # Neutral regime so only recency moves the weights.
        votes, _ = build_consensus(
            "EURUSD", _FakeWM("UNKNOWN"), 1.10,
            dynamic_weight_provider=p, is_confirmed=False,
        )
        vm = _vote_map(votes)
        assert vm["structure"].weight == pytest.approx(0.85)
        assert vm["volume"].weight == pytest.approx(0.85)

    def test_provider_composes_with_calibrator(self):
        # Dynamic context weight × learned-accuracy multiplier.
        class _Calibrator:
            def calibrated_weight(self, module, base_weight, *a):
                return base_weight * (2.0 if module == "structure" else 1.0)

        p = DynamicWeightProvider(
            _DEFAULT_CONSENSUS_WEIGHTS,
            DynamicWeightConfig(htf_modules=["structure"], ltf_modules=["volume"]),
        )
        votes, _ = build_consensus(
            "EURUSD", _FakeWM("TREND"), 1.10,
            dynamic_weight_provider=p, vote_calibrator=_Calibrator(),
            is_confirmed=True,
        )
        vm = _vote_map(votes)
        # structure: dynamic 1.3 (HTF trend) × calibrator 2.0 = 2.6
        assert vm["structure"].weight == pytest.approx(2.6)

    def test_broken_provider_falls_back_to_static(self):
        class _Boom:
            def compute_weights(self, *a, **k):
                raise RuntimeError("boom")

        votes, _ = build_consensus(
            "EURUSD", _FakeWM("TREND"), 1.10, dynamic_weight_provider=_Boom(),
        )
        vm = _vote_map(votes)
        assert vm["structure"].weight == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# DynamicWeightConfig validation
# ---------------------------------------------------------------------------


class TestConfigValidation:
    def test_defaults_valid(self):
        cfg = DynamicWeightConfig()
        assert cfg.enabled is True
        assert "structure" in cfg.htf_modules
        assert "momentum" in cfg.ltf_modules

    def test_min_must_be_below_max(self):
        with pytest.raises(ValueError):
            DynamicWeightConfig(min_weight=5.0, max_weight=1.0)

    def test_vol_thresholds_ordered(self):
        with pytest.raises(ValueError):
            DynamicWeightConfig(low_vol_threshold=2.0, high_vol_threshold=1.0)

    @pytest.mark.parametrize("bad", [0.0, -1.0, float("nan"), float("inf")])
    def test_non_positive_mult_rejected(self, bad):
        with pytest.raises(ValueError):
            DynamicWeightConfig(trending_htf_mult=bad)
