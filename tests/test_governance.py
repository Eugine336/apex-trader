"""Tests for the Governance Division (Department 8 — authorise + contain).

Covers:
* The GovernanceVerdict enum (only AUTHORIZED is an approval).
* The PromotionStage lifecycle (SHADOW → VALIDATION → LIMITED → FULL).
* Recommendation authorisation: behaviour-neutral bounded approve, pathological
  reject, and fail-closed on unknown types.
* The module-promotion lifecycle gate (DEFERRED / AUTHORIZED / REJECTED).
* Toxic-pair tracking + (opt-in) enforcement.
* The enforcement arm (contain a module, freeze/release a tunable).
* End-to-end wiring through the RecommendationGateway.
"""

from __future__ import annotations

from types import SimpleNamespace

from governance import (
    GovernanceDivision,
    GovernanceVerdict,
    PromotionStage,
)


# ── Test doubles ──────────────────────────────────────────────────────────


class _FakeModuleGovernor:
    """Minimal ModuleGovernor double: records force_mode calls + cf signals."""

    def __init__(self, cf=None):
        self.calls = []
        self._cf = cf or {}

    def force_mode(self, module, mode, reason=""):
        self.calls.append((module, str(getattr(mode, "value", mode)), reason))
        return True

    def _cf_signal_map(self):
        return dict(self._cf)


class _FakeTuner:
    def __init__(self):
        self.frozen = []
        self.reset = []

    def freeze_tunable(self, name, reason=""):
        self.frozen.append((name, reason))
        return True

    def reset_failure_count(self, name):
        self.reset.append(name)
        return True


def _rec(rec_type, source="test", confidence=0.5, **payload):
    return SimpleNamespace(
        recommendation_type=rec_type, source=source,
        payload=payload, confidence=confidence,
    )


# ── Verdict + stage models ───────────────────────────────────────────────


class TestVerdictAndStages:
    def test_only_authorized_is_approval(self):
        assert GovernanceVerdict.AUTHORIZED.approved is True
        assert GovernanceVerdict.REJECTED.approved is False
        assert GovernanceVerdict.DEFERRED.approved is False

    def test_stage_ladder(self):
        assert PromotionStage.SHADOW.next_stage() is PromotionStage.VALIDATION
        assert PromotionStage.VALIDATION.next_stage() is PromotionStage.LIMITED
        assert PromotionStage.LIMITED.next_stage() is PromotionStage.FULL
        assert PromotionStage.FULL.next_stage() is None

    def test_stage_weight_and_influence(self):
        assert PromotionStage.SHADOW.weight_factor == 0.0
        assert PromotionStage.VALIDATION.weight_factor == 0.0
        assert PromotionStage.LIMITED.influences_live is True
        assert PromotionStage.FULL.weight_factor == 1.0
        assert PromotionStage.SHADOW.influences_live is False

    def test_stage_coerce(self):
        assert PromotionStage.coerce("full") is PromotionStage.FULL
        assert PromotionStage.coerce("nonsense") is PromotionStage.SHADOW


# ── Recommendation authorisation ─────────────────────────────────────────


class TestAuthorize:
    def test_size_adjust_within_bounds_approved(self):
        g = GovernanceDivision()
        ok, _ = g.authorize(_rec("SIZE_ADJUST", multiplier=1.5))
        assert ok is True

    def test_size_adjust_out_of_bounds_rejected(self):
        g = GovernanceDivision()
        assert g.authorize(_rec("SIZE_ADJUST", multiplier=99.0))[0] is False
        assert g.authorize(_rec("SIZE_ADJUST", multiplier=-1.0))[0] is False

    def test_size_adjust_non_finite_rejected(self):
        g = GovernanceDivision()
        assert g.authorize(_rec("SIZE_ADJUST", multiplier=float("inf")))[0] is False

    def test_weight_update_within_bounds_approved(self):
        g = GovernanceDivision()
        ok, _ = g.authorize(_rec("WEIGHT_UPDATE", multipliers={"a": 1.2, "b": 0.7}))
        assert ok is True

    def test_weight_update_out_of_bounds_rejected(self):
        g = GovernanceDivision()
        assert g.authorize(_rec("WEIGHT_UPDATE", multipliers={"a": 999.0}))[0] is False

    def test_protective_types_approved(self):
        g = GovernanceDivision()
        assert g.authorize(_rec("AVOID_PATTERN", pair="EURUSD"))[0] is True
        assert g.authorize(_rec("MODULE_SUPPRESS", module="vwap"))[0] is True

    def test_profile_change_requires_profile(self):
        g = GovernanceDivision()
        assert g.authorize(_rec("PROFILE_CHANGE", profile="scalp"))[0] is True
        assert g.authorize(_rec("PROFILE_CHANGE"))[0] is False

    def test_unknown_type_fail_closed(self):
        g = GovernanceDivision()
        ok, reason = g.authorize(_rec("MADE_UP_TYPE", x=1))
        assert ok is False
        assert "unknown recommendation type" in reason

    def test_decision_counts_tracked(self):
        g = GovernanceDivision()
        g.authorize(_rec("SIZE_ADJUST", multiplier=1.0))
        g.authorize(_rec("MADE_UP", x=1))
        counts = g.get_status()["decision_counts"]
        assert counts["AUTHORIZED"] == 1
        assert counts["REJECTED"] == 1
        assert len(g.recent_authorizations()) == 2


# ── Promotion lifecycle ──────────────────────────────────────────────────


class TestPromotionLifecycle:
    def test_deferred_when_insufficient_data(self):
        g = GovernanceDivision()
        v = g.authorize_promotion("synth__x", PromotionStage.SHADOW,
                                  {"accuracy": 0.9, "sample_size": 1})
        assert v is GovernanceVerdict.DEFERRED

    def test_authorized_when_bar_met(self):
        g = GovernanceDivision()
        v = g.authorize_promotion("synth__x", PromotionStage.SHADOW,
                                  {"accuracy": 0.60, "sample_size": 25})
        assert v is GovernanceVerdict.AUTHORIZED

    def test_full_authority_requires_strongest_bar(self):
        g = GovernanceDivision()
        # LIMITED→FULL needs >=80 signals, >=0.55 acc, marginal_r>=0.
        weak = g.authorize_promotion("synth__x", PromotionStage.LIMITED,
                                     {"accuracy": 0.60, "sample_size": 50, "marginal_r": 0.1})
        assert weak is GovernanceVerdict.DEFERRED
        strong = g.authorize_promotion("synth__x", PromotionStage.LIMITED,
                                       {"accuracy": 0.60, "sample_size": 100, "marginal_r": 0.1})
        assert strong is GovernanceVerdict.AUTHORIZED

    def test_harmful_attribution_rejected(self):
        g = GovernanceDivision()
        v = g.authorize_promotion("synth__x", PromotionStage.SHADOW,
                                  {"accuracy": 0.9, "sample_size": 100,
                                   "marginal_r": -0.2, "better_off_without": True})
        assert v is GovernanceVerdict.REJECTED

    def test_full_stage_cannot_advance(self):
        g = GovernanceDivision()
        v = g.authorize_promotion("synth__x", PromotionStage.FULL, {})
        assert v is GovernanceVerdict.DEFERRED
        assert len(g.recent_promotions()) == 1


# ── Toxic pairs + enforcement arm ────────────────────────────────────────


class TestToxicAndEnforcement:
    def test_toxic_pair_recorded_not_enforced_by_default(self):
        g = GovernanceDivision()  # enforce_toxic_pairs defaults False
        ok, _ = g.authorize(
            _rec("TOXIC_PAIR_BLOCK", module_a="a", module_b="b", interaction_effect=-0.2)
        )
        assert ok is True
        pairs = g.toxic_pairs()
        assert len(pairs) == 1 and pairs[0]["enforced"] is False

    def test_toxic_pair_missing_module_rejected(self):
        g = GovernanceDivision()
        assert g.authorize(_rec("TOXIC_PAIR_BLOCK", module_a="a"))[0] is False

    def test_toxic_enforcement_contains_weaker_module(self):
        fg = _FakeModuleGovernor(cf={"a": {"mr_per_trade": -0.3},
                                     "b": {"mr_per_trade": 0.1}})
        g = GovernanceDivision(module_governor=fg, enforce_toxic_pairs=True)
        g.authorize(_rec("TOXIC_PAIR_BLOCK", module_a="a", module_b="b",
                         interaction_effect=-0.2))
        # 'a' has the worse marginal R → it is the one shadowed.
        assert fg.calls and fg.calls[0][0] == "a"
        assert fg.calls[0][1] == "SHADOW"
        # Enforced only once per pair.
        g.authorize(_rec("TOXIC_PAIR_BLOCK", module_a="a", module_b="b",
                         interaction_effect=-0.3))
        contained = [c for c in fg.calls if c[0] in ("a", "b")]
        assert len(contained) == 1
        assert g.toxic_pairs()[0]["occurrences"] == 2

    def test_contain_module_and_freeze_release(self):
        fg, ft = _FakeModuleGovernor(), _FakeTuner()
        g = GovernanceDivision(module_governor=fg, tuner_agent=ft)
        assert g.contain_module("vwap", reason="manual") is True
        assert fg.calls[0][0] == "vwap"
        assert g.freeze_tuning("gate_tuner", reason="runaway") is True
        assert ft.frozen == [("gate_tuner", "governance: runaway")]
        assert g.release_tuning("gate_tuner") is True
        assert ft.reset == ["gate_tuner"]

    def test_enforcement_noop_without_arms(self):
        g = GovernanceDivision()
        assert g.contain_module("vwap") is False
        assert g.freeze_tuning("gate_tuner") is False
        assert g.release_tuning("gate_tuner") is False


# ── End-to-end through the RecommendationGateway ─────────────────────────


class TestGatewayIntegration:
    def test_governance_authorizes_through_gateway(self):
        from adaptive.recommendations import (
            LearningRecommendation,
            RecommendationGateway,
            RecommendationType,
        )

        g = GovernanceDivision()
        gw = RecommendationGateway(authorizer=g.authorize)
        # Default is now governance-required (Phase 7).
        assert gw.governance_required is True
        good = gw.submit(LearningRecommendation(
            source="optimizer.size",
            recommendation_type=RecommendationType.SIZE_ADJUST,
            payload={"multiplier": 1.2},
        ))
        bad = gw.submit(LearningRecommendation(
            source="x", recommendation_type="MADE_UP", payload={},
        ))
        assert good.approved is True
        assert bad.approved is False

    def test_no_authorizer_gateway_is_failsafe_open(self):
        # Even with governance required by default, a gateway with no authoriser
        # auto-approves so a governance wiring fault never blocks trading.
        from adaptive.recommendations import (
            LearningRecommendation,
            RecommendationGateway,
            RecommendationType,
        )

        gw = RecommendationGateway()
        assert gw.is_approved(LearningRecommendation(
            source="s", recommendation_type=RecommendationType.SIZE_ADJUST,
            payload={"multiplier": 9.0},
        )) is True
