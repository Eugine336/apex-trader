"""Tests for HTF authority weighting across ranker horizons.

Opportunistic-trading rewire: the system no longer demotes higher-timeframe
(H4/D1) authority based on a SCALP/SWING label. The MARKET decides the
opportunity, and HTF context is weighed UNIFORMLY for every trade by default
(scale 1.0 for every horizon). The horizon-scaling MECHANISM is retained — and
still works when an operator explicitly configures per-horizon scales — but it
is inert out of the box.

These tests cover the uniform defaults, the (opt-in) EntryEngine and
DecisionEngine scaling mechanisms, and the default decide_entry behaviour
(horizon does not change the verdict).
"""

import pytest

from config import OpportunityRankerConfig
from decision.actions import EntryAction
from decision.context import EntryContext
from decision.engine import DecisionEngine, DecisionWeights
from decision.situation import SituationAssessment
from trigger.entry_engine import EntryEngine


# ── Config ──────────────────────────────────────────────────────────────────

class TestOpportunityRankerConfig:
    def test_execute_is_live_by_default(self):
        cfg = OpportunityRankerConfig()
        assert cfg.execute is True

    def test_default_horizon_scales_are_uniform(self):
        # HTF authority is uniform by default — no per-horizon demotion.
        cfg = OpportunityRankerConfig()
        assert cfg.scalp_htf_penalty_scale == 1.0
        assert cfg.swing_htf_penalty_scale == 1.0
        assert cfg.mixed_htf_penalty_scale == 1.0

    @pytest.mark.parametrize("field", [
        "scalp_htf_penalty_scale",
        "swing_htf_penalty_scale",
        "mixed_htf_penalty_scale",
    ])
    def test_scale_must_be_within_unit_interval(self, field):
        with pytest.raises(ValueError):
            OpportunityRankerConfig(**{field: 1.5})
        with pytest.raises(ValueError):
            OpportunityRankerConfig(**{field: -0.1})


# ── EntryEngine H4 penalty scaling ────────────────────────────────────────────

class TestEntryEnginePenaltyScale:
    def _engine(self, **cfg_overrides) -> EntryEngine:
        # Pure-method test: bypass the heavy __init__ and inject only the config
        # the helper reads.
        eng = EntryEngine.__new__(EntryEngine)

        class _Cfg:
            pass

        eng.config = _Cfg()
        eng.config.opportunity_ranker = OpportunityRankerConfig(**cfg_overrides)
        return eng

    def test_uniform_full_authority_by_default(self):
        eng = self._engine()
        assert eng._htf_penalty_scale("SCALP") == 1.0
        assert eng._htf_penalty_scale("SWING") == 1.0
        assert eng._htf_penalty_scale("MIXED") == 1.0

    def test_no_horizon_is_full_authority(self):
        eng = self._engine()
        assert eng._htf_penalty_scale("") == 1.0
        assert eng._htf_penalty_scale(None) == 1.0
        assert eng._htf_penalty_scale("UNKNOWN") == 1.0

    def test_custom_scales_still_respected(self):
        # The mechanism remains tunable for operators who opt in.
        eng = self._engine(scalp_htf_penalty_scale=0.25)
        assert eng._htf_penalty_scale("SCALP") == 0.25


# ── DecisionEngine HTF weight scaling ─────────────────────────────────────────

class TestDecisionEngineHorizonScaling:
    def test_uniform_full_authority_by_default(self):
        de = DecisionEngine()
        assert de._horizon_htf_scale("SCALP") == 1.0
        assert de._horizon_htf_scale("SWING") == 1.0
        assert de._horizon_htf_scale("MIXED") == 1.0
        assert de._horizon_htf_scale("") == 1.0

    def test_scale_lookup_with_explicit_args(self):
        # The mechanism still honours explicit per-horizon scales when set.
        de = DecisionEngine(scalp_htf_scale=0.0, swing_htf_scale=1.0, mixed_htf_scale=0.5)
        assert de._horizon_htf_scale("SCALP") == 0.0
        assert de._horizon_htf_scale("SWING") == 1.0
        assert de._horizon_htf_scale("MIXED") == 0.5
        assert de._horizon_htf_scale("") == 1.0
        assert de._horizon_htf_scale("anything") == 1.0

    def test_default_engine_leaves_weights_unchanged(self):
        de = DecisionEngine()
        w = DecisionWeights()
        # Uniform default (scale 1.0) → weights returned unchanged for any horizon.
        assert de._apply_horizon_scaling(w, "SCALP") is w
        assert de._apply_horizon_scaling(w, "MIXED") is w

    def test_explicit_scalp_zeroes_htf_weights(self):
        de = DecisionEngine(scalp_htf_scale=0.0)
        w = DecisionWeights()
        scaled = de._apply_horizon_scaling(w, "SCALP")
        assert scaled.enter_htf == 0.0
        assert scaled.skip_htf == 0.0
        assert scaled.conviction_htf == 0.0

    def test_conviction_sum_is_preserved_when_scaled(self):
        de = DecisionEngine(scalp_htf_scale=0.0)
        w = DecisionWeights()
        scaled = de._apply_horizon_scaling(w, "SCALP")
        base_sum = (w.conviction_htf + w.conviction_structure
                    + w.conviction_momentum + w.conviction_confidence)
        scaled_sum = (scaled.conviction_htf + scaled.conviction_structure
                      + scaled.conviction_momentum + scaled.conviction_confidence)
        assert scaled_sum == pytest.approx(base_sum)
        # Freed HTF conviction is shifted onto momentum.
        assert scaled.conviction_momentum > w.conviction_momentum

    def test_empty_horizon_returns_weights_unchanged(self):
        de = DecisionEngine(scalp_htf_scale=0.0)
        w = DecisionWeights()
        assert de._apply_horizon_scaling(w, "") is w

    def test_explicit_mixed_halves_htf_weights(self):
        de = DecisionEngine(mixed_htf_scale=0.5)
        w = DecisionWeights()
        scaled = de._apply_horizon_scaling(w, "MIXED")
        assert scaled.enter_htf == pytest.approx(w.enter_htf * 0.5)
        assert scaled.skip_htf == pytest.approx(w.skip_htf * 0.5)


# ── DecisionEngine.decide_entry behaviour ─────────────────────────────────────

def _counter_htf_ctx(horizon: str) -> EntryContext:
    """A LONG entry against a BEARISH H4 — counter-HTF by construction."""
    return EntryContext(
        symbol="EURUSD",
        direction="LONG",
        scan_score=70,
        scan_direction="LONG",
        entry_type="OB_MIDPOINT",
        entry_price=1.0850,
        stop_loss=1.0820,
        tp1=1.0880,
        tp2=1.0910,
        risk_reward_1=1.0,
        risk_reward_2=2.0,
        risk_pips=30.0,
        d1_trend="BEARISH",
        h4_trend="BEARISH",
        h1_trend="BEARISH",
        regime="TRENDING",
        horizon=horizon,
    )


def _neutral_sa() -> SituationAssessment:
    # HTF strongly opposes; everything else is exactly balanced so the ONLY
    # mover between horizons is the HTF skip weight.
    return SituationAssessment(
        tf_alignment=-1.0,
        momentum=0.0,
        structure_integrity=0.5,
        read_confidence=0.5,
        urgency=0.0,
        primary_label="TEST",
    )


class TestDecideEntryUniformHtf:
    def test_horizon_does_not_change_default_verdict(self):
        # Opportunistic-trading rewire: by default HTF is uniform, so a SCALP
        # label no longer flips a counter-HTF skip into an entry.
        de = DecisionEngine(reversal_enabled=False)
        sa = _neutral_sa()
        no_horizon = de.decide_entry(_counter_htf_ctx(""), sa)
        scalp = de.decide_entry(_counter_htf_ctx("SCALP"), sa)
        swing = de.decide_entry(_counter_htf_ctx("SWING"), sa)
        assert no_horizon.action == scalp.action == swing.action

    def test_explicit_scalp_demotion_flips_skip_to_enter(self):
        # The mechanism still works when an operator explicitly opts in.
        de = DecisionEngine(reversal_enabled=False, scalp_htf_scale=0.0)
        sa = _neutral_sa()
        no_horizon = de.decide_entry(_counter_htf_ctx(""), sa)
        scalp = de.decide_entry(_counter_htf_ctx("SCALP"), sa)
        assert no_horizon.action == EntryAction.SKIP
        assert scalp.action != EntryAction.SKIP

