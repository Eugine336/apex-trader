"""Tests for the fully-resolved partial collapses #4, #5, #8.

#4 — the executor no longer blindly dispatches ``candidates[0]`` (the ranker's
     raw EV sort); a scorer (the orchestrator's per-candidate grader) can choose
     among ALL candidates.
#5 — the decision engine reads the per-timeframe alignment VECTOR, so a split
     HTF stack erodes the ENTER bonus / adds a SKIP penalty instead of being
     hidden inside the averaged scalar.
#8 — the planner conviction gate is dispersion-aware: a split advisor panel
     reads below a united-but-mediocre one.
"""

import types

from brain.opportunity_ranker import Opportunity
from brain.orchestrator import Orchestrator
from decision.engine import DecisionEngine
from decision.situation import SituationAssessment
from decision.context import EntryContext
from management.opportunity_executor import OpportunityExecutor
from planning import PlannerConfig, TradePlanContext, TradePlanner


# ── helpers ────────────────────────────────────────────────────────────────

def _opp(direction, tf, ev, conf=0.7, coherence=0.7) -> Opportunity:
    return Opportunity(
        direction=direction,
        timeframe_class=tf,
        expected_value=ev,
        confidence=conf,
        coherence=coherence,
        net_score=1.0 if direction == "LONG" else -1.0,
        reward_risk=2.0,
        win_prob=0.55,
    )


def _entry_ctx(**overrides) -> EntryContext:
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


def _sa(tf_alignment, tf_components) -> SituationAssessment:
    return SituationAssessment(
        tf_alignment=tf_alignment,
        momentum=0.3,
        structure_integrity=0.7,
        read_confidence=0.7,
        primary_label="TREND_CONTINUATION",
        tf_components=dict(tf_components),
    )


# ── #4: executor selects among ALL candidates via a scorer ───────────────────

class TestExecutorCandidateSelection:
    def _exec(self):
        return OpportunityExecutor(types.SimpleNamespace(execute=True, max_concurrent=1))

    def test_no_scorer_keeps_ranker_top1(self):
        cands = [_opp("LONG", "SWING", 2.0), _opp("SHORT", "SCALP", 1.5)]
        chosen = self._exec().select(cands)
        assert chosen is cands[0]

    def test_scorer_can_override_ranker_order(self):
        cands = [_opp("LONG", "SWING", 2.0), _opp("SHORT", "SCALP", 1.9)]
        # Scorer prefers the SHORT scalp (second candidate).
        chosen = self._exec().select(
            cands, scorer=lambda c: 1.0 if c.direction == "SHORT" else 0.1
        )
        assert chosen is cands[1]
        assert chosen.direction == "SHORT"

    def test_scorer_failure_falls_back_to_top1(self):
        cands = [_opp("LONG", "SWING", 2.0), _opp("SHORT", "SCALP", 1.5)]

        def _boom(_c):
            raise RuntimeError("scorer broke")

        chosen = self._exec().select(cands, scorer=_boom)
        assert chosen is cands[0]

    def test_empty_returns_none(self):
        assert self._exec().select([]) is None
        assert self._exec().select(None, scorer=lambda c: 1.0) is None


class TestOrchestratorGradeCandidate:
    def test_strong_candidate_grades_higher_than_weak(self):
        orch = Orchestrator(None)
        strong = orch.grade_candidate(
            ranker_ev=2.0, ranker_coherence=0.95, ranker_confidence=0.9,
        )
        weak = orch.grade_candidate(
            ranker_ev=0.05, ranker_coherence=0.2, ranker_confidence=0.2,
        )
        assert strong > weak
        assert 0.0 <= weak <= 1.0 and 0.0 <= strong <= 1.0

    def test_orchestrator_pick_is_used_by_executor(self):
        orch = Orchestrator(None)
        # candidate[0] has marginally higher EV but poor coherence/confidence;
        # candidate[1] is slightly lower EV but far more coherent + confident.
        cands = [
            _opp("LONG", "SWING", 1.6, conf=0.2, coherence=0.2),
            _opp("SHORT", "SCALP", 1.5, conf=0.95, coherence=0.95),
        ]
        ex = OpportunityExecutor(types.SimpleNamespace(execute=True, max_concurrent=1))
        chosen = ex.select(
            cands,
            scorer=lambda c: orch.grade_candidate(
                direction=c.direction, horizon=c.timeframe_class,
                ranker_ev=c.expected_value, ranker_coherence=c.coherence,
                ranker_confidence=c.confidence,
            ),
        )
        assert chosen is cands[1]


# ── #5: decision engine is TF-vector (conflict) aware ────────────────────────

class TestTfConflictAwareness:
    def test_opposition_detected_in_split_stack(self):
        sa = _sa(0.285, {"D1": 0.8, "H4": -0.6, "H1": 0.7})
        assert DecisionEngine._tf_conflict_opposition(sa) > 0.0

    def test_no_opposition_when_stack_agrees(self):
        sa = _sa(0.6, {"D1": 0.6, "H4": 0.6, "H1": 0.6})
        assert DecisionEngine._tf_conflict_opposition(sa) == 0.0

    def test_no_vector_is_neutral(self):
        sa = _sa(0.4, {})
        assert DecisionEngine._tf_conflict_opposition(sa) == 0.0

    def test_split_stack_lowers_enter_margin(self):
        ctx = _entry_ctx()
        # Same supportive scalar, but one hides a strongly-opposing H4.
        united = _sa(0.40, {"D1": 0.40, "H4": 0.40, "H1": 0.40})
        split = _sa(0.40, {"D1": 0.95, "H4": -0.55, "H1": 0.75})

        aware = DecisionEngine(tf_conflict_aware=True)
        m_united = aware.decide_entry(ctx, united).entry_margin
        m_split = aware.decide_entry(ctx, split).entry_margin
        assert m_split < m_united

    def test_legacy_ignores_the_vector(self):
        ctx = _entry_ctx()
        united = _sa(0.40, {"D1": 0.40, "H4": 0.40, "H1": 0.40})
        split = _sa(0.40, {"D1": 0.95, "H4": -0.55, "H1": 0.75})
        legacy = DecisionEngine(tf_conflict_aware=False)
        assert legacy.decide_entry(ctx, united).entry_margin == \
            legacy.decide_entry(ctx, split).entry_margin


# ── #8: planner conviction gate is dispersion-aware ──────────────────────────

def _plan_ctx(**overrides) -> TradePlanContext:
    base = dict(
        symbol="EURUSD",
        pip_size=0.0001,
        atr_pips=20.0,
        current_price=1.1000,
        direction="LONG",
        scanner_score=60.0,
        zone_type="ORDER_BLOCK",
        zone_quality=0.5,
        zone_entry_price=1.0998,
        de_confidence=0.5,
        de_tf_alignment=0.5,
        rl_action=1,
        rl_confidence=0.5,
        rl_expected_r=2.0,
        pair_win_rate=0.5,
        session_win_rate=0.6,
        proposed_sl_price=1.0980,
        proposed_sl_pips=20.0,
        structure_sl_available=True,
        risk_reward_2=3.0,
        base_risk_pct=0.5,
        session="LONDON",
        situation_label="TREND_CONTINUATION",
    )
    base.update(overrides)
    return TradePlanContext(**base)


class TestPlannerDispersionAwareAgreement:
    def test_split_panel_reads_below_mean(self):
        cfg = PlannerConfig(dispersion_aware_agreement=True, advisor_dispersion_penalty=0.5)
        planner = TradePlanner(cfg)
        # Scanner strongly supports, RL strongly opposes — a split panel.
        ctx = _plan_ctx(scanner_score=95.0, de_tf_alignment=0.6,
                        rl_action=2, rl_confidence=0.9, pair_win_rate=0.5)
        mean = planner._advisor_agreement(ctx)
        gate, dispersion = planner._gate_agreement(ctx, mean)
        assert dispersion > 0.0
        assert gate < mean

    def test_united_panel_unchanged(self):
        cfg = PlannerConfig(dispersion_aware_agreement=True)
        planner = TradePlanner(cfg)
        ctx = _plan_ctx(scanner_score=60.0, de_tf_alignment=0.2,
                        rl_action=1, rl_confidence=0.2, pair_win_rate=0.6)
        mean = planner._advisor_agreement(ctx)
        gate, dispersion = planner._gate_agreement(ctx, mean)
        assert dispersion < 1e-6
        assert abs(gate - mean) < 1e-9

    def test_legacy_returns_mean(self):
        planner = TradePlanner(PlannerConfig(dispersion_aware_agreement=False))
        ctx = _plan_ctx(scanner_score=95.0, rl_action=2, rl_confidence=0.9)
        mean = planner._advisor_agreement(ctx)
        gate, dispersion = planner._gate_agreement(ctx, mean)
        assert gate == mean and dispersion == 0.0
