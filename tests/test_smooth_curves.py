"""
Session 1: Smooth Curves — verify the step functions are now continuous.

Each test asserts: (a) values at/around the old tier boundaries transition
smoothly (no cliff), (b) neighbouring inputs map to *distinct* outputs, and
(c) default behaviour is preserved at the old tier centres.
"""

import pytest

from decision.engine import DecisionEngine, DecisionWeights
from decision.situation import SituationAssessment
from planning.trade_planner import (
    PlannerConfig,
    TradePlanner,
    _smooth_derisk,
    _smoothstep,
)
from risk.risk_engine import RiskEngine
from risk.position_sizer import PositionSizer


# ── #18 conviction → size multiplier ───────────────────────────────────────

class TestConvictionSizeMultiplier:
    def test_default_range_preserves_legacy(self):
        eng = DecisionEngine()
        # Legacy mapping was round(0.5 + conviction, 2).
        for c in (0.0, 0.25, 0.5, 0.75, 1.0):
            assert eng._conviction_to_size_multiplier(c) == pytest.approx(round(0.5 + c, 2))

    def test_range_is_configurable_and_wider(self):
        eng = DecisionEngine(conviction_size_min=0.25, conviction_size_max=2.0)
        assert eng._conviction_to_size_multiplier(0.0) == pytest.approx(0.25)
        assert eng._conviction_to_size_multiplier(1.0) == pytest.approx(2.0)
        assert eng._conviction_to_size_multiplier(0.5) == pytest.approx(1.125)

    def test_neighbours_distinct(self):
        eng = DecisionEngine(conviction_size_min=0.25, conviction_size_max=2.0)
        assert eng._conviction_to_size_multiplier(0.851) != eng._conviction_to_size_multiplier(0.879)

    def test_reversed_bounds_are_normalised(self):
        eng = DecisionEngine(conviction_size_min=2.0, conviction_size_max=0.25)
        assert eng._conviction_to_size_multiplier(0.0) == pytest.approx(0.25)
        assert eng._conviction_to_size_multiplier(1.0) == pytest.approx(2.0)

    def test_dominant_dimension_surfaced(self):
        w = DecisionWeights()
        sa = SituationAssessment(
            tf_alignment=0.2, momentum=0.1,
            structure_integrity=0.95, read_confidence=0.5,
        )
        label, contrib = DecisionEngine._dominant_conviction_dimension(sa, w)
        assert label == "structure"
        assert contrib > 0.0


# ── #22 planner smooth de-risk / boost ──────────────────────────────────────

class TestPlannerSmoothSizing:
    def test_derisk_smooth_through_threshold(self):
        # Just below vs just above the threshold should be close (no cliff),
        # and both strictly between full (1.0) and the reduction floor.
        below = _smooth_derisk(0.39, 0.4, 0.5, 0.15)
        above = _smooth_derisk(0.41, 0.4, 0.5, 0.15)
        assert abs(below - above) < 0.1
        assert 0.5 < below < 1.0
        assert 0.5 < above < 1.0

    def test_derisk_monotonic_decreasing(self):
        prev = 1.1
        v = 0.0
        while v <= 1.0:
            f = _smooth_derisk(v, 0.4, 0.5, 0.15)
            assert f <= prev + 1e-9
            assert 0.5 - 1e-9 <= f <= 1.0 + 1e-9
            prev = f
            v += 0.02

    def test_far_below_threshold_is_full(self):
        assert _smooth_derisk(0.0, 0.4, 0.5, 0.15) == pytest.approx(1.0)

    def test_far_above_threshold_is_reduction(self):
        assert _smooth_derisk(1.0, 0.4, 0.5, 0.15) == pytest.approx(0.5)

    def test_zero_band_recovers_step_at_threshold(self):
        assert _smooth_derisk(0.41, 0.4, 0.5, 0.0) == pytest.approx(0.5)
        assert _smooth_derisk(0.39, 0.4, 0.5, 0.0) == pytest.approx(1.0)


# ── #30 risk-engine smooth scalers ──────────────────────────────────────────

class TestRiskEngineSmoothScalers:
    def test_conviction_neighbours_distinct(self):
        a = RiskEngine._scale_by_conviction(0.851)
        b = RiskEngine._scale_by_conviction(0.879)
        assert a != b
        assert a < b

    def test_conviction_monotonic_and_bounded(self):
        prev = -1.0
        c = 0.0
        while c <= 1.0:
            f = RiskEngine._scale_by_conviction(c)
            assert f >= prev - 1e-9
            assert 0.0 <= f <= 1.0
            prev = f
            c += 0.01

    def test_heat_smooth_through_boundary(self):
        # 1.49% and 1.51% no longer jump from 0.85 to 0.7.
        assert abs(RiskEngine._scale_by_portfolio_heat(1.49)
                   - RiskEngine._scale_by_portfolio_heat(1.51)) < 0.05

    def test_heat_monotonic_decreasing_derisk_only(self):
        prev = 1.1
        h = 0.0
        while h <= 3.0:
            f = RiskEngine._scale_by_portfolio_heat(h)
            assert f <= prev + 1e-9
            assert f <= 1.0 + 1e-9
            prev = f
            h += 0.05

    def test_drawdown_smooth_through_boundary(self):
        # 0.049 and 0.051 no longer jump from 1.0 to 0.85.
        assert abs(RiskEngine._scale_by_drawdown(0.049)
                   - RiskEngine._scale_by_drawdown(0.051)) < 0.05

    def test_drawdown_monotonic_decreasing(self):
        prev = 1.1
        d = 0.0
        while d <= 0.3:
            f = RiskEngine._scale_by_drawdown(d)
            assert f <= prev + 1e-9
            assert f <= 1.0 + 1e-9
            prev = f
            d += 0.005


# ── #30 position-sizer volatility scaler is continuous at 0.5 ───────────────

class TestVolatilityScalerContinuity:
    def test_no_jump_at_half_ratio(self):
        sizer = PositionSizer()
        # ATR ratios straddling the old 0.5 discontinuity.
        just_above = sizer.adjust_for_volatility(1.0, 0.51, 1.0)
        at_half = sizer.adjust_for_volatility(1.0, 0.50, 1.0)
        just_below = sizer.adjust_for_volatility(1.0, 0.49, 1.0)
        assert abs(just_above - at_half) < 0.05
        assert abs(at_half - just_below) < 0.05

    def test_high_vol_reduces_low_vol_increases(self):
        sizer = PositionSizer()
        assert sizer.adjust_for_volatility(1.0, 0.003, 0.0015) < 1.0
        assert sizer.adjust_for_volatility(1.0, 0.0005, 0.0015) > 1.0
