"""
Tests for the Trade Orchestrator (the round table / graded sizer).

Dependency-light — pure multiplier math, no torch/pandas/numpy required.
Verifies that every evidence dimension *dims* size rather than *killing* a
trade, that the only hard veto is physics, and that the overall multiplier is
bounded so the orchestrator can never oversize or zero out an approved trade.
"""

import pytest

from brain.orchestrator import (
    Orchestrator,
    TradeProposal,
    OrchestratorVerdict,
    DimensionContribution,
    VETO_NEGATIVE_MARGIN,
    VETO_MARKET_CLOSED,
)


class _Cfg:
    """Minimal duck-typed OrchestratorConfig."""
    size_floor = 0.5
    dimension_floor = 0.6
    ranker_ev_full = 1.5
    de_margin_full = 0.5
    scan_score_full = 100.0
    scalp_htf_opposition_scale = 0.3


@pytest.fixture
def orch():
    return Orchestrator(_Cfg())


class TestPhysicsVeto:
    def test_physics_veto_zeroes_size(self, orch):
        v = orch.evaluate(TradeProposal(
            pair="EURUSD", direction="LONG",
            physics_vetoes=[VETO_NEGATIVE_MARGIN],
        ))
        assert v.vetoed is True
        assert v.size_multiplier == 0.0
        assert v.tradeable is False
        assert VETO_NEGATIVE_MARGIN in v.veto_reason

    def test_multiple_physics_vetoes_listed(self, orch):
        v = orch.evaluate(TradeProposal(
            pair="X", direction="SHORT",
            physics_vetoes=[VETO_NEGATIVE_MARGIN, VETO_MARKET_CLOSED],
        ))
        assert v.vetoed
        assert VETO_NEGATIVE_MARGIN in v.veto_reason
        assert VETO_MARKET_CLOSED in v.veto_reason

    def test_empty_veto_strings_ignored(self, orch):
        v = orch.evaluate(TradeProposal(pair="X", direction="LONG", physics_vetoes=["", None]))
        assert v.vetoed is False


class TestGradedSizing:
    def test_strong_setup_near_full_size(self, orch):
        v = orch.evaluate(TradeProposal(
            pair="EURUSD", direction="LONG", horizon="SWING",
            ranker_ev=1.5, ranker_coherence=1.0, tf_alignment=0.8,
            de_margin=0.5, de_conviction=1.0, advisor_agreement=1.0, scan_score=100,
        ))
        assert v.tradeable
        # Every dimension at/above full → multiplier at the ceiling.
        assert v.size_multiplier == pytest.approx(1.0, abs=1e-6)

    def test_weak_setup_floored_not_killed(self, orch):
        v = orch.evaluate(TradeProposal(
            pair="X", direction="SHORT", horizon="SWING",
            ranker_ev=0.0, ranker_coherence=0.0, tf_alignment=-1.0,
            de_margin=0.0, de_conviction=0.0, advisor_agreement=0.0, scan_score=0,
        ))
        # A graded "no" is still a small trade, never zero.
        assert v.size_multiplier == pytest.approx(0.5, abs=1e-6)
        assert v.tradeable

    def test_missing_dimensions_are_neutral(self, orch):
        v = orch.evaluate(TradeProposal(pair="X", direction="LONG"))
        assert v.size_multiplier == pytest.approx(1.0, abs=1e-6)
        assert v.tradeable

    def test_size_never_exceeds_one(self, orch):
        v = orch.evaluate(TradeProposal(
            pair="X", direction="LONG", ranker_ev=10.0, de_margin=5.0,
            de_conviction=1.0, scan_score=500, ranker_coherence=1.0,
            tf_alignment=1.0, advisor_agreement=1.0,
        ))
        assert v.size_multiplier <= 1.0


class TestHTFAsContext:
    def test_opposing_htf_dims_scalp_less_than_swing(self, orch):
        # Keep every other dimension at full credit so the size stays above the
        # floor and the HTF dimension alone differentiates the two horizons.
        common = dict(ranker_ev=1.5, ranker_coherence=1.0, de_margin=0.5,
                      de_conviction=1.0, advisor_agreement=1.0, scan_score=100,
                      tf_alignment=-0.8)
        scalp = orch.evaluate(TradeProposal(pair="X", direction="LONG", horizon="SCALP", **common))
        swing = orch.evaluate(TradeProposal(pair="X", direction="LONG", horizon="SWING", **common))
        # A SCALP idea legitimately trades against a slower TF — it should be
        # dimmed far less than a SWING facing the same opposition.
        assert scalp.size_multiplier > swing.size_multiplier

    def test_supporting_htf_full_credit(self, orch):
        v = orch.evaluate(TradeProposal(pair="X", direction="LONG", tf_alignment=0.9))
        tf = next(d for d in v.dimensions if d.name == "tf_alignment")
        assert tf.multiplier == pytest.approx(1.0, abs=1e-6)

    def test_opposing_htf_never_below_floor(self, orch):
        v = orch.evaluate(TradeProposal(pair="X", direction="LONG", horizon="SWING", tf_alignment=-1.0))
        tf = next(d for d in v.dimensions if d.name == "tf_alignment")
        assert tf.multiplier >= 0.6 - 1e-9


class TestDimensionsRecorded:
    def test_all_dimensions_present_and_serialisable(self, orch):
        v = orch.evaluate(TradeProposal(
            pair="X", direction="LONG", ranker_ev=1.0, ranker_coherence=0.7,
            tf_alignment=0.2, de_margin=0.3, de_conviction=0.6,
            advisor_agreement=0.6, scan_score=70,
        ))
        names = {d.name for d in v.dimensions}
        assert names == {
            "ranker_ev", "coherence", "tf_alignment", "de_margin",
            "de_quality", "conviction", "advisor_agreement", "scan_score",
        }
        d = v.to_dict()
        assert "dimensions" in d and len(d["dimensions"]) == 8
        for dim in v.dimensions:
            assert isinstance(dim, DimensionContribution)
            assert dim.reason  # never silently empty


class TestNoConfigDefaults:
    def test_orchestrator_without_config_uses_internal_defaults(self):
        o = Orchestrator(None)
        v = o.evaluate(TradeProposal(pair="X", direction="LONG", ranker_ev=1.5))
        assert isinstance(v, OrchestratorVerdict)
        assert 0.0 < v.size_multiplier <= 1.0
