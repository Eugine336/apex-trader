"""Tests for Decision Engine entry decisions."""

import pytest

from decision.context import EntryContext
from decision.situation import SituationEngine, SituationAssessment
from decision.engine import DecisionEngine
from decision.governor import RiskGovernor
from decision.actions import EntryAction


@pytest.fixture
def engine():
    return DecisionEngine()


@pytest.fixture
def situation():
    return SituationEngine()


@pytest.fixture
def governor():
    return RiskGovernor()


def _make_ctx(**overrides) -> EntryContext:
    defaults = dict(
        symbol="EURUSD",
        direction="LONG",
        scan_score=75,
        scan_direction="LONG",
        entry_type="FVG_MIDPOINT",
        entry_price=1.0850,
        stop_loss=1.0820,
        tp1=1.0895,
        tp2=1.0940,
        risk_reward_1=1.5,
        risk_reward_2=3.0,
        risk_pips=30.0,
        entry_mode="PENDING",
        micro_confirmation="momentum_only",
        base_lots=0.10,
        account_balance=1000.0,
        risk_pct=0.01,
        d1_trend="BULLISH",
        d1_confidence=0.75,
        h4_trend="BULLISH",
        h4_confidence=0.80,
        h1_trend="BULLISH",
        h1_confidence=0.70,
        m1_trend="BULLISH",
        m1_aligned_count=4,
        session_name="LONDON",
        session_tradeable=True,
        open_trade_count=1,
        max_open_trades=5,
        current_spread=1.2,
        typical_spread=1.0,
        regime="TRENDING",
    )
    defaults.update(overrides)
    return EntryContext(**defaults)


class TestSituationAssessEntry:
    def test_strong_alignment_produces_high_tf_alignment(self, situation):
        ctx = _make_ctx(d1_trend="BULLISH", d1_confidence=0.8,
                        h4_trend="BULLISH", h4_confidence=0.8,
                        h1_trend="BULLISH", h1_confidence=0.7)
        sa = situation.assess_entry(ctx)
        assert sa.tf_alignment > 0.5

    def test_opposing_htf_produces_negative_alignment(self, situation):
        ctx = _make_ctx(d1_trend="BEARISH", d1_confidence=0.8,
                        h4_trend="BEARISH", h4_confidence=0.7,
                        h1_trend="BEARISH", h1_confidence=0.6)
        sa = situation.assess_entry(ctx)
        assert sa.tf_alignment < -0.3

    def test_fvg_ob_overlap_boosts_structure(self, situation):
        ctx = _make_ctx(entry_type="FVG_OB_OVERLAP")
        sa = situation.assess_entry(ctx)
        assert sa.structure_integrity > 0.7

    def test_no_entry_type_gives_baseline_structure(self, situation):
        ctx = _make_ctx(entry_type="")
        sa = situation.assess_entry(ctx)
        assert sa.structure_integrity == pytest.approx(0.5, abs=0.01)

    def test_news_proximity_raises_urgency(self, situation):
        ctx = _make_ctx(minutes_to_high_impact_news=5.0, news_impact="HIGH")
        sa = situation.assess_entry(ctx)
        assert sa.urgency > 0.5

    def test_momentum_from_m1_aligned(self, situation):
        ctx = _make_ctx(m1_aligned_count=5)
        sa = situation.assess_entry(ctx)
        assert sa.momentum > 0.3

    def test_labels_derived_not_hardcoded(self, situation):
        ctx = _make_ctx()
        sa = situation.assess_entry(ctx)
        assert sa.primary_label in (
            "TREND_CONTINUATION", "COUNTER_MOMENTUM", "RANGE_ENTRY",
            "COUNTER_TREND", "URGENT_RISK", "WEAK_STRUCTURE", "MIXED",
        )


class TestDecideEntry:
    def test_high_alignment_good_structure_enters(self, engine, situation):
        ctx = _make_ctx(scan_score=60)
        sa = situation.assess_entry(ctx)
        decision = engine.decide_entry(ctx, sa)
        assert decision.should_enter

    def test_conflicted_alignment_weak_structure_skips(self, engine, situation):
        ctx = _make_ctx(
            d1_trend="BEARISH", d1_confidence=0.7,
            h4_trend="BEARISH", h4_confidence=0.6,
            h1_trend="BEARISH", h1_confidence=0.5,
            entry_type="",
            risk_reward_2=1.2,
            m1_aligned_count=1,
        )
        sa = situation.assess_entry(ctx)
        decision = engine.decide_entry(ctx, sa)
        assert not decision.should_enter

    def test_conviction_sizing_high(self, engine):
        sa = SituationAssessment(
            tf_alignment=0.8, momentum=0.6,
            structure_integrity=0.9, read_confidence=0.9,
        )
        conviction = engine.compute_conviction(sa)
        mult = engine._conviction_to_size_multiplier(conviction)
        assert mult >= 1.2

    def test_conviction_sizing_low(self, engine):
        sa = SituationAssessment(
            tf_alignment=-0.3, momentum=-0.2,
            structure_integrity=0.2, read_confidence=0.4,
        )
        conviction = engine.compute_conviction(sa)
        mult = engine._conviction_to_size_multiplier(conviction)
        assert mult <= 0.9

    def test_market_mode_when_strong_momentum(self, engine, situation):
        ctx = _make_ctx(
            entry_mode="PENDING",
            micro_confirmation="choch_bos",
            scan_score=85,
            m1_aligned_count=5,
        )
        sa = situation.assess_entry(ctx)
        decision = engine.decide_entry(ctx, sa)
        if decision.should_enter:
            assert decision.action == EntryAction.ENTER_MARKET


class TestGovernorEntry:
    def test_veto_max_trades(self, governor, situation):
        ctx = _make_ctx(open_trade_count=5, max_open_trades=5)
        sa = situation.assess_entry(ctx)
        decision = DecisionEngine().decide_entry(ctx, sa)
        reviewed = governor.review_entry(decision, ctx, sa)
        assert not reviewed.should_enter
        assert reviewed.governor_vetoed

    def test_veto_portfolio_heat(self, governor, situation):
        ctx = _make_ctx(portfolio_heat_pct=2.0)
        sa = situation.assess_entry(ctx)
        decision = DecisionEngine().decide_entry(ctx, sa)
        reviewed = governor.review_entry(decision, ctx, sa)
        assert not reviewed.should_enter

    def test_veto_wide_spread(self, governor, situation):
        ctx = _make_ctx(current_spread=10.0, typical_spread=2.0)
        sa = situation.assess_entry(ctx)
        decision = DecisionEngine().decide_entry(ctx, sa)
        reviewed = governor.review_entry(decision, ctx, sa)
        assert not reviewed.should_enter

    def test_no_veto_when_safe(self, governor, situation):
        ctx = _make_ctx()
        sa = situation.assess_entry(ctx)
        decision = DecisionEngine().decide_entry(ctx, sa)
        reviewed = governor.review_entry(decision, ctx, sa)
        if decision.should_enter:
            assert reviewed.should_enter
            assert not reviewed.governor_vetoed

    def test_skip_passes_through(self, governor, situation):
        ctx = _make_ctx(
            d1_trend="BEARISH", d1_confidence=0.9,
            h4_trend="BEARISH", h4_confidence=0.8,
            h1_trend="BEARISH", h1_confidence=0.7,
            entry_type="",
            risk_reward_2=1.0,
            m1_aligned_count=0,
        )
        sa = situation.assess_entry(ctx)
        decision = DecisionEngine().decide_entry(ctx, sa)
        reviewed = governor.review_entry(decision, ctx, sa)
        assert not reviewed.should_enter


class TestEntryDecisionProperties:
    def test_enter_market_is_market(self):
        from decision.actions import EntryDecision
        d = EntryDecision(action=EntryAction.ENTER_MARKET, reason="test")
        assert d.should_enter
        assert d.is_market

    def test_enter_pending_is_not_market(self):
        from decision.actions import EntryDecision
        d = EntryDecision(action=EntryAction.ENTER_PENDING, reason="test")
        assert d.should_enter
        assert not d.is_market

    def test_skip_is_not_enter(self):
        from decision.actions import EntryDecision
        d = EntryDecision(action=EntryAction.SKIP, reason="test")
        assert not d.should_enter
        assert not d.is_market


# ── Roadmap C: M5 as the primary decision engine ───────────────────────────

class TestM5PrimaryWeighting:
    def test_conviction_favors_m5_over_htf(self, engine):
        """Under the M5-primary defaults, a strong-M5 / weak-HTF setup must out-
        convict a strong-HTF / weak-M5 one. (Under the old HTF-40% weights the
        HTF setup won — this is the behavioural flip C delivers.)"""
        great_m5 = SituationAssessment(
            tf_alignment=-0.5, structure_integrity=1.0,
            momentum=0.5, read_confidence=0.6,
        )
        great_htf = SituationAssessment(
            tf_alignment=1.0, structure_integrity=0.2,
            momentum=0.0, read_confidence=0.6,
        )
        assert engine.compute_conviction(great_m5) > engine.compute_conviction(great_htf)

    def test_strong_ltf_overcomes_opposing_htf(self, engine):
        """HTF opposing, but strong M5 structure + M1 momentum → ENTER."""
        sa = SituationAssessment(
            tf_alignment=-0.4, structure_integrity=0.9,
            momentum=0.8, read_confidence=0.8, urgency=0.0,
        )
        ctx = _make_ctx(risk_reward_2=3.0)
        decision = engine.decide_entry(ctx, sa)
        assert decision.should_enter

    def test_custom_weights_are_respected(self):
        """An HTF-heavy weight set flips the conviction ranking back — proving
        the weights are config-driven, not hardcoded."""
        from decision.engine import DecisionWeights

        htf_heavy = DecisionEngine(DecisionWeights(
            conviction_htf=0.70, conviction_structure=0.10,
            conviction_momentum=0.10, conviction_confidence=0.10,
        ))
        great_m5 = SituationAssessment(
            tf_alignment=-0.5, structure_integrity=1.0,
            momentum=0.5, read_confidence=0.6,
        )
        great_htf = SituationAssessment(
            tf_alignment=1.0, structure_integrity=0.2,
            momentum=0.0, read_confidence=0.6,
        )
        assert htf_heavy.compute_conviction(great_htf) > htf_heavy.compute_conviction(great_m5)

    def test_config_defaults_are_m5_primary(self):
        from config import AppConfig
        d = AppConfig().decision
        assert d.conviction_structure_weight > d.conviction_htf_weight
        assert d.conviction_momentum_weight > d.conviction_htf_weight
        assert d.enter_structure_coeff > d.enter_htf_coeff
