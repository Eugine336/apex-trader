"""
Tests for the Learning → Governance recommendation pipeline
(adaptive/recommendations.py) and its integration with the VoteCalibrator and
InteractionAnalyzer.

Covers:
* The gateway's auto-approve default (behaviour-neutral until Phase 7).
* Authoriser delegation: approve, reject, tuple-form, and fault fallback.
* Audit surface (recent / stats).
* VoteCalibrator publication routed through the gateway — identical behaviour
  when auto-approved, and old map retained when a real authoriser rejects.
"""

import pytest

from config import VoteCalibratorConfig
from adaptive.recommendations import (
    LearningRecommendation,
    RecommendationGateway,
    RecommendationStatus,
    RecommendationType,
)
from adaptive.vote_calibrator import VoteCalibrator


# ── Test doubles ──────────────────────────────────────────────────────────


class _FakeResp:
    def __init__(self, accuracy_all, total_signals):
        self.accuracy_all = accuracy_all
        self.total_signals = total_signals


class _FakeFeedback:
    def __init__(self, data):
        self._data = data

    def get_all_emitter_summaries(self, lookback=100):
        return {m: _FakeResp(a, n) for m, (a, n) in self._data.items()}


def _rec(rec_type=RecommendationType.SIZE_ADJUST, **payload):
    return LearningRecommendation(
        source="test", recommendation_type=rec_type, payload=payload,
    )


def _cfg(**kw):
    base = dict(vote_calibration_enabled=True)
    base.update(kw)
    return VoteCalibratorConfig(**base)


# ── Contract ───────────────────────────────────────────────────────────────


class TestRecommendationContract:
    def test_to_dict_roundtrips_fields(self):
        rec = LearningRecommendation(
            source="optimizer", recommendation_type=RecommendationType.AVOID_PATTERN,
            payload={"pair": "EURUSD"}, confidence=0.7, evidence={"n": 30},
        )
        d = rec.to_dict()
        assert d["source"] == "optimizer"
        assert d["recommendation_type"] == RecommendationType.AVOID_PATTERN
        assert d["payload"] == {"pair": "EURUSD"}
        assert d["confidence"] == 0.7
        assert d["evidence"] == {"n": 30}
        assert "timestamp" in d


# ── Gateway: auto-approve default ────────────────────────────────────────────


class TestGatewayAutoApprove:
    def test_default_auto_approves(self):
        gw = RecommendationGateway()
        decision = gw.submit(_rec())
        assert decision.approved
        assert decision.status == RecommendationStatus.APPROVED

    def test_is_approved_convenience(self):
        gw = RecommendationGateway()
        assert gw.is_approved(_rec()) is True

    def test_auto_approves_even_with_authorizer_when_not_required(self):
        # Authoriser present but governance_required False → still auto-approve.
        gw = RecommendationGateway(governance_required=False, authorizer=lambda r: False)
        assert gw.is_approved(_rec()) is True


# ── Gateway: authorisation when governance required ──────────────────────────


class TestGatewayGovernance:
    def test_authorizer_rejects(self):
        gw = RecommendationGateway(governance_required=True, authorizer=lambda r: False)
        decision = gw.submit(_rec())
        assert not decision.approved
        assert decision.status == RecommendationStatus.REJECTED

    def test_authorizer_approves(self):
        gw = RecommendationGateway(governance_required=True, authorizer=lambda r: True)
        assert gw.is_approved(_rec()) is True

    def test_authorizer_tuple_form_with_reason(self):
        gw = RecommendationGateway(
            governance_required=True, authorizer=lambda r: (False, "too risky"),
        )
        decision = gw.submit(_rec())
        assert not decision.approved
        assert decision.reason == "too risky"

    def test_authorizer_fault_falls_back_to_reject(self):
        def boom(_rec):
            raise RuntimeError("authoriser down")

        gw = RecommendationGateway(governance_required=True, authorizer=boom)
        decision = gw.submit(_rec())
        assert not decision.approved
        assert "authoriser error" in decision.reason

    def test_set_governance_required_toggles_behaviour(self):
        gw = RecommendationGateway(authorizer=lambda r: False)
        assert gw.is_approved(_rec()) is True       # not required yet
        gw.set_governance_required(True)
        assert gw.is_approved(_rec()) is False      # now enforced


# ── Gateway: audit surface ──────────────────────────────────────────────────


class TestGatewayAudit:
    def test_recent_and_stats(self):
        gw = RecommendationGateway()
        gw.submit(_rec(RecommendationType.SIZE_ADJUST))
        gw.submit(_rec(RecommendationType.AVOID_PATTERN))
        recent = gw.recent()
        assert len(recent) == 2
        stats = gw.stats()
        assert stats["approved"] == 2
        assert stats["rejected"] == 0
        assert RecommendationType.SIZE_ADJUST in stats["by_type"]

    def test_history_is_bounded(self):
        gw = RecommendationGateway(history_limit=3)
        for _ in range(10):
            gw.submit(_rec())
        assert len(gw.recent()) == 3


# ── VoteCalibrator integration ───────────────────────────────────────────────


class TestVoteCalibratorGateway:
    def _feedback(self):
        return _FakeFeedback({
            "momentum": (0.85, 100), "structure": (0.35, 100),
            "vwap": (0.6, 100), "liquidity": (0.5, 100),
        })

    def test_publication_identical_when_auto_approved(self):
        """With an auto-approving gateway, the published map equals the no-gateway
        map — the recommendation pipeline is behaviour-neutral by default."""
        baseline = VoteCalibrator(_cfg(), self._feedback())
        baseline_cal = baseline.recalibrate()

        gw = RecommendationGateway()  # auto-approve
        gated = VoteCalibrator(_cfg(), self._feedback())
        gated.set_recommendation_gateway(gw)
        gated_cal = gated.recalibrate()

        assert not gated_cal.skipped
        assert gated.get_weight_multipliers() == baseline.get_weight_multipliers()
        assert gated_cal.multipliers == baseline_cal.multipliers
        # A WEIGHT_UPDATE recommendation was recorded.
        stats = gw.stats()
        assert stats["by_type"].get(RecommendationType.WEIGHT_UPDATE, {}).get(
            RecommendationStatus.APPROVED, 0
        ) == 1

    def test_rejection_retains_previous_map(self):
        """When Governance rejects, the previously-published map is retained and
        the calibration is marked skipped."""
        gw = RecommendationGateway(governance_required=True, authorizer=lambda r: False)
        vc = VoteCalibrator(_cfg(), self._feedback())
        vc.set_recommendation_gateway(gw)
        cal = vc.recalibrate()
        assert cal.skipped
        assert "rejected by governance" in cal.reason
        # Nothing published — map stays empty (neutral).
        assert vc.get_weight_multipliers() == {}
