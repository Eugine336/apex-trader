"""Tests for the Decision Engine enter/skip gate softening (#6).

The DE's enter/skip binary (``margin <= 0 → SKIP``) was the last CRITICAL
collapse: a marginally-negative margin died exactly like a hopeless one and the
setup never reached the orchestrator round table. When ``soften_gate`` is on
(the caller sets it only when the orchestrator is the final sizer) a *mildly*
negative margin instead flows through as ENTER carrying a bounded quality
multiplier — the round table decides *how big*, not *whether*. A margin at/below
the hard safety floor is still genuinely hopeless and hard-SKIPs.
"""

import pytest

from config import OrchestratorConfig
from decision.actions import EntryAction
from decision.engine import DecisionEngine
from decision.context import EntryContext
from decision.situation import SituationAssessment
from brain.orchestrator import Orchestrator, TradeProposal


def _make_ctx(**overrides) -> EntryContext:
    defaults = dict(
        symbol="EURUSD",
        direction="LONG",
        scan_score=70,
        scan_direction="LONG",
        entry_type="FVG_MIDPOINT",
        entry_price=1.0850,
        stop_loss=1.0820,
        tp1=1.0895,
        tp2=1.0940,
        risk_reward_1=1.5,
        risk_reward_2=2.0,
        risk_pips=30.0,
        entry_mode="PENDING",
        micro_confirmation="momentum_only",
        base_lots=0.10,
        account_balance=1000.0,
        risk_pct=0.01,
        # HTF trends kept BULLISH for a LONG so the counter-HTF / reversal path
        # never fires — we want to isolate the enter/skip margin behaviour.
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


def _mildly_negative_sa() -> SituationAssessment:
    """A situation whose enter/skip margin is mildly negative (~-0.07).

    enter = 0.20 baseline (no bonus clears its sub-threshold).
    skip  = HTF opposing (0.06) + weak structure (0.06) + opposing momentum (0.15)
          = 0.27  →  margin = 0.20 - 0.27 = -0.07.
    """
    return SituationAssessment(
        tf_alignment=-0.3,
        momentum=-0.5,
        structure_integrity=0.2,
        read_confidence=0.5,
        urgency=0.0,
        primary_label="MIXED",
    )


class TestMargin:
    def test_baseline_margin_is_mildly_negative(self):
        # Sanity: the default engine (softening off) SKIPs this with margin < 0.
        eng = DecisionEngine()
        ctx, sa = _make_ctx(), _mildly_negative_sa()
        d = eng.decide_entry(ctx, sa)
        assert not d.should_enter
        assert d.action == EntryAction.SKIP
        assert -0.2 < d.entry_margin < 0.0


class TestSoftenedFlowsThrough:
    def test_negative_margin_above_safety_flows_with_multiplier(self):
        eng = DecisionEngine(soften_gate=True, gate_safety_margin=-1.0, gate_quality_floor=0.15)
        ctx, sa = _make_ctx(), _mildly_negative_sa()
        d = eng.decide_entry(ctx, sa)
        # No longer killed — flows through for the orchestrator to size.
        assert d.should_enter
        assert d.action in (EntryAction.ENTER_MARKET, EntryAction.ENTER_PENDING)
        assert d.gate_softened is True
        assert d.entry_margin < 0.0
        # multiplier == max(1 + margin, floor), and strictly between floor and 1.
        assert d.de_quality_multiplier == pytest.approx(
            max(1.0 + d.entry_margin, 0.15), abs=1e-4
        )
        assert 0.15 <= d.de_quality_multiplier < 1.0
        assert "DE-SOFTENED" in d.reason

    def test_more_negative_margin_yields_smaller_multiplier(self):
        eng = DecisionEngine(soften_gate=True, gate_safety_margin=-2.0, gate_quality_floor=0.05)
        ctx = _make_ctx()
        mild = eng.decide_entry(ctx, _mildly_negative_sa())
        worse = eng.decide_entry(
            ctx,
            SituationAssessment(
                tf_alignment=-0.8, momentum=-0.9, structure_integrity=0.0,
                read_confidence=0.2, urgency=0.6, primary_label="MIXED",
            ),
        )
        assert worse.entry_margin < mild.entry_margin < 0.0
        # A deeper-negative margin must size strictly smaller (gradient preserved).
        assert worse.de_quality_multiplier < mild.de_quality_multiplier


class TestSafetyFloorStillKills:
    def test_margin_below_safety_floor_hard_skips(self):
        # Same mildly-negative margin (~-0.07) but a tighter safety floor → SKIP.
        eng = DecisionEngine(soften_gate=True, gate_safety_margin=-0.05)
        ctx, sa = _make_ctx(), _mildly_negative_sa()
        d = eng.decide_entry(ctx, sa)
        assert not d.should_enter
        assert d.action == EntryAction.SKIP
        assert d.gate_softened is False
        assert d.size_multiplier == 0.0
        assert d.conviction == 0.0


class TestSofteningDisabled:
    def test_disabled_keeps_legacy_skip(self):
        eng = DecisionEngine(soften_gate=False)
        ctx, sa = _make_ctx(), _mildly_negative_sa()
        d = eng.decide_entry(ctx, sa)
        assert not d.should_enter
        assert d.action == EntryAction.SKIP
        assert d.gate_softened is False
        assert d.de_quality_multiplier == 1.0  # default, untouched

    def test_positive_margin_unaffected_by_softening(self):
        # A clearly-positive margin enters normally and is NOT marked softened
        # regardless of the flag.
        eng = DecisionEngine(soften_gate=True)
        ctx = _make_ctx(risk_reward_2=4.0)
        sa = SituationAssessment(
            tf_alignment=0.8, momentum=0.6, structure_integrity=0.9,
            read_confidence=0.9, urgency=0.0, primary_label="TREND_CONTINUATION",
        )
        d = eng.decide_entry(ctx, sa)
        assert d.should_enter
        assert d.gate_softened is False
        assert d.de_quality_multiplier == 1.0
        assert d.entry_margin > 0.0


class TestExceptionFallsBackToHardSkip:
    def test_exception_in_softening_check_hard_skips(self):
        eng = DecisionEngine(soften_gate=True, gate_safety_margin=-1.0)
        # Corrupt the safety margin so the `margin > self.gate_safety_margin`
        # comparison raises — the guard must fall back to the legacy hard SKIP
        # rather than letting a broken setup through.
        eng.gate_safety_margin = object()  # type: ignore[assignment]
        ctx, sa = _make_ctx(), _mildly_negative_sa()
        d = eng.decide_entry(ctx, sa)
        assert not d.should_enter
        assert d.action == EntryAction.SKIP
        assert d.gate_softened is False


class TestOrchestratorFoldsQuality:
    def test_de_quality_dim_present_and_lowers_size(self):
        orch = Orchestrator(OrchestratorConfig())
        # Strong inputs so the base product sits at/near 1.0 (above size_floor),
        # making the softened-quality reduction visible rather than floored out.
        strong = dict(
            ranker_ev=1.5, ranker_coherence=1.0, tf_alignment=1.0, de_margin=0.5,
            de_conviction=1.0, advisor_agreement=1.0, scan_score=100,
        )
        base = TradeProposal(pair="EURUSD", direction="LONG", **strong)
        softened = TradeProposal(
            pair="EURUSD", direction="LONG", de_quality_multiplier=0.7, **strong,
        )
        vb = orch.evaluate(base)
        vs = orch.evaluate(softened)
        # The dimension is always present; neutral (1.0) when not softened.
        bq = next(d for d in vb.dimensions if d.name == "de_quality")
        sq = next(d for d in vs.dimensions if d.name == "de_quality")
        assert bq.multiplier == 1.0
        assert sq.multiplier == pytest.approx(0.7, abs=1e-6)
        # Folding the softened quality in must size the trade strictly smaller.
        assert vs.size_multiplier < vb.size_multiplier

    def test_de_quality_none_is_neutral(self):
        orch = Orchestrator(OrchestratorConfig())
        v = orch.evaluate(TradeProposal(
            pair="X", direction="LONG", ranker_ev=1.5, de_quality_multiplier=None,
        ))
        q = next(d for d in v.dimensions if d.name == "de_quality")
        assert q.multiplier == 1.0
        assert v.to_dict()  # serialisable


class TestConfigValidation:
    def test_defaults(self):
        c = OrchestratorConfig()
        assert c.soften_de_gate is True
        assert c.de_safety_margin == -0.3
        assert c.de_gate_quality_floor == 0.15

    def test_quality_floor_out_of_range_rejected(self):
        with pytest.raises(ValueError):
            OrchestratorConfig(de_gate_quality_floor=1.5)
        with pytest.raises(ValueError):
            OrchestratorConfig(de_gate_quality_floor=-0.1)

    def test_positive_safety_margin_rejected(self):
        with pytest.raises(ValueError):
            OrchestratorConfig(de_safety_margin=0.5)

    def test_zero_safety_margin_allowed(self):
        c = OrchestratorConfig(de_safety_margin=0.0)
        assert c.de_safety_margin == 0.0
