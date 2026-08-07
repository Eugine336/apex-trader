"""
Tests for brain.smoothing — the continuous-curve primitives that replace
discontinuous step functions (Session 1: Smooth Curves).

Must run WITHOUT torch/pandas/numpy.
"""

import importlib.util
import os

import pytest

# Load brain.smoothing directly from its file so the test does not trigger the
# heavy ``brain`` package __init__ (numpy-dependent) just to reach a pure-math
# leaf module.
_SPEC = importlib.util.spec_from_file_location(
    "apex_smoothing_under_test",
    os.path.join(os.path.dirname(__file__), "..", "brain", "smoothing.py"),
)
smoothing = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(smoothing)

clamp = smoothing.clamp
piecewise_linear = smoothing.piecewise_linear
smoothstep = smoothing.smoothstep
logistic_between = smoothing.logistic_between
soft_ramp = smoothing.soft_ramp


class TestClamp:
    def test_within(self):
        assert clamp(0.5, 0.0, 1.0) == 0.5

    def test_below(self):
        assert clamp(-1.0, 0.0, 1.0) == 0.0

    def test_above(self):
        assert clamp(2.0, 0.0, 1.0) == 1.0

    def test_reversed_bounds(self):
        assert clamp(0.5, 1.0, 0.0) == 0.5


class TestPiecewiseLinear:
    PTS = ((0.0, 0.5), (0.85, 0.7), (0.88, 0.85), (0.92, 1.0), (1.0, 1.0))

    def test_clamps_below_first(self):
        assert piecewise_linear(-5.0, self.PTS) == 0.5

    def test_clamps_above_last(self):
        assert piecewise_linear(5.0, self.PTS) == 1.0

    def test_hits_anchor_exactly(self):
        assert piecewise_linear(0.88, self.PTS) == pytest.approx(0.85)

    def test_interpolates_between(self):
        # Halfway between (0.85,0.7) and (0.88,0.85) → 0.775 at x=0.865.
        assert piecewise_linear(0.865, self.PTS) == pytest.approx(0.775, abs=1e-9)

    def test_distinct_neighbours_no_collapse(self):
        """The whole point: 0.851 and 0.879 must NOT map to the same value."""
        a = piecewise_linear(0.851, self.PTS)
        b = piecewise_linear(0.879, self.PTS)
        assert a != b
        assert a < b

    def test_monotonic_increasing(self):
        prev = -1.0
        x = 0.0
        while x <= 1.0:
            v = piecewise_linear(x, self.PTS)
            assert v >= prev - 1e-9
            prev = v
            x += 0.01

    def test_continuity_no_jumps(self):
        """No step larger than the local slope allows — scan finely and assert
        consecutive samples never jump more than a small epsilon."""
        prev = piecewise_linear(0.0, self.PTS)
        x = 0.001
        while x <= 1.0:
            v = piecewise_linear(x, self.PTS)
            assert abs(v - prev) < 0.05  # max slope * step is tiny
            prev = v
            x += 0.001

    def test_empty_points(self):
        assert piecewise_linear(0.5, ()) == 0.0


class TestSmoothstep:
    def test_below_edge0(self):
        assert smoothstep(-1.0, 0.0, 1.0) == 0.0

    def test_above_edge1(self):
        assert smoothstep(2.0, 0.0, 1.0) == 1.0

    def test_midpoint_is_half(self):
        assert smoothstep(0.5, 0.0, 1.0) == pytest.approx(0.5)

    def test_just_below_edge1_is_near_full(self):
        # The audit requirement: a value of 0.49 against a 0.5 boundary (ramp
        # completing at 0.5 from 0.3) should earn ~95%+ of the weight.
        assert smoothstep(0.49, 0.3, 0.5) > 0.95

    def test_degenerate_edges(self):
        assert smoothstep(0.4, 0.5, 0.5) == 0.0
        assert smoothstep(0.6, 0.5, 0.5) == 1.0

    def test_monotonic_and_continuous(self):
        prev = 0.0
        x = -0.5
        while x <= 1.5:
            v = smoothstep(x, 0.0, 1.0)
            assert v >= prev - 1e-9
            assert 0.0 <= v <= 1.0
            prev = v
            x += 0.01


class TestLogisticBetween:
    def test_midpoint(self):
        assert logistic_between(0.5, 0.0, 1.0, 0.5) == pytest.approx(0.5)

    def test_bounds_respected(self):
        for x in (-100.0, -1.0, 0.0, 0.5, 1.0, 100.0):
            v = logistic_between(x, 0.5, 1.5, 0.5, 10.0)
            assert 0.5 - 1e-6 <= v <= 1.5 + 1e-6

    def test_no_overflow_extreme(self):
        # Must not raise on extreme inputs.
        assert logistic_between(1e9, 0.0, 1.0, 0.0, 50.0) == pytest.approx(1.0, abs=1e-6)
        assert logistic_between(-1e9, 0.0, 1.0, 0.0, 50.0) == pytest.approx(0.0, abs=1e-6)


class TestSoftRamp:
    def test_endpoints(self):
        assert soft_ramp(0.0, 0.0, 1.0, 2.0, 4.0) == 2.0
        assert soft_ramp(1.0, 0.0, 1.0, 2.0, 4.0) == 4.0

    def test_midpoint(self):
        assert soft_ramp(0.5, 0.0, 1.0, 2.0, 4.0) == pytest.approx(3.0)

    def test_clamped_outside(self):
        assert soft_ramp(-1.0, 0.0, 1.0, 2.0, 4.0) == 2.0
        assert soft_ramp(2.0, 0.0, 1.0, 2.0, 4.0) == 4.0
