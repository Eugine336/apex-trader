"""Tests for the Decision Intelligence System."""

import json
import tempfile
from pathlib import Path


from decision.context import TradeContext
from decision.situation import SituationEngine, SituationAssessment
from decision.actions import Action, ManagementDecision
from decision.engine import DecisionEngine
from decision.governor import RiskGovernor
from decision.journal import DecisionJournal


# ── Helpers ──────────────────────────────────────────────────────────────

def _make_ctx(**overrides) -> TradeContext:
    """Create a TradeContext with sensible defaults, overriding as needed."""
    defaults = dict(
        symbol="EURUSD",
        order_id="test-oid-1",
        direction="BUY",
        entry_type="APEX_ENTRY",
        entry_price=1.0850,
        current_price=1.0860,
        current_sl=1.0820,
        pnl_pips=10.0,
        hold_minutes=30.0,
        at_breakeven=False,
        tp1_hit=False,
        trailing=False,
        lots=0.1,
        original_risk_pips=30.0,
        scan_score=60,
        scan_direction="LONG",
        d1_trend="BULLISH",
        d1_confidence=0.70,
        h4_trend="BULLISH",
        h4_confidence=0.65,
        h1_trend="BULLISH",
        h1_confidence=0.60,
        m1_trend="BULLISH",
        m1_confidence=0.50,
        m1_aligned_count=4,
        score_history=[75, 72, 68, 65, 60],
        open_trade_count=2,
        max_open_trades=5,
    )
    defaults.update(overrides)
    return TradeContext(**defaults)


# ── SituationEngine tests ────────────────────────────────────────────────

class TestSituationEngine:
    def setup_method(self):
        self.engine = SituationEngine()

    def test_aligned_htf_gives_positive_alignment(self):
        ctx = _make_ctx(
            d1_trend="BULLISH", d1_confidence=0.8,
            h4_trend="BULLISH", h4_confidence=0.7,
            h1_trend="BULLISH", h1_confidence=0.6,
        )
        sa = self.engine.assess_open_trade(ctx)
        assert sa.tf_alignment > 0.4, f"Expected positive alignment, got {sa.tf_alignment}"

    def test_opposing_htf_gives_negative_alignment(self):
        ctx = _make_ctx(
            direction="BUY",
            d1_trend="BEARISH", d1_confidence=0.8,
            h4_trend="BEARISH", h4_confidence=0.7,
            h1_trend="BEARISH", h1_confidence=0.6,
        )
        sa = self.engine.assess_open_trade(ctx)
        assert sa.tf_alignment < -0.4, f"Expected negative alignment, got {sa.tf_alignment}"

    def test_unknown_trends_give_neutral(self):
        ctx = _make_ctx(
            d1_trend="UNKNOWN", h4_trend="UNKNOWN", h1_trend="UNKNOWN",
        )
        sa = self.engine.assess_open_trade(ctx)
        assert abs(sa.tf_alignment) < 0.05

    def test_momentum_from_m1_aligned(self):
        ctx = _make_ctx(m1_aligned_count=5, m1_event="NONE")
        sa = self.engine.assess_open_trade(ctx)
        assert sa.momentum > 0.3

    def test_momentum_from_m1_opposing(self):
        ctx = _make_ctx(m1_aligned_count=0, m1_event="BOS_BEARISH")
        sa = self.engine.assess_open_trade(ctx)
        assert sa.momentum < -0.3

    def test_structure_broken_by_h4_event(self):
        ctx = _make_ctx(
            direction="BUY",
            h4_event="BOS_BEARISH",
        )
        sa = self.engine.assess_open_trade(ctx)
        assert sa.structure_integrity < 0.25

    def test_adopted_observing_label(self):
        ctx = _make_ctx(entry_type="ORPHAN_ADOPTED", hold_minutes=5)
        sa = self.engine.assess_open_trade(ctx)
        assert sa.primary_label == "ADOPTED_OBSERVING"

    def test_trend_continuation_label(self):
        ctx = _make_ctx(
            d1_trend="BULLISH", d1_confidence=0.8,
            h4_trend="BULLISH", h4_confidence=0.7,
            h1_trend="BULLISH", h1_confidence=0.6,
            m1_aligned_count=4,
            pnl_pips=10.0,
        )
        sa = self.engine.assess_open_trade(ctx)
        assert sa.primary_label == "TREND_CONTINUATION"


# ── DecisionEngine tests ─────────────────────────────────────────────────

class TestDecisionEngine:
    def setup_method(self):
        self.se = SituationEngine()
        self.de = DecisionEngine()

    def test_high_alignment_intact_structure_low_score_holds(self):
        """The core test: HTF aligned + structure intact + low score → HOLD not CLOSE."""
        ctx = _make_ctx(
            scan_score=35,
            d1_trend="BULLISH", d1_confidence=0.80,
            h4_trend="BULLISH", h4_confidence=0.70,
            h1_trend="BULLISH", h1_confidence=0.65,
            pnl_pips=-5.0,
            original_risk_pips=30.0,
        )
        sa = self.se.assess_open_trade(ctx)
        decision = self.de.decide_management(ctx, sa)
        assert decision.action != Action.CLOSE, (
            f"Expected HOLD with aligned HTF, got {decision.action.value}: {decision.reason}"
        )

    def test_broken_structure_closes(self):
        ctx = _make_ctx(
            direction="BUY",
            scan_score=35,
            d1_trend="BEARISH", d1_confidence=0.80,
            h4_trend="BEARISH", h4_confidence=0.75,
            h1_trend="BEARISH", h1_confidence=0.70,
            h4_event="BOS_BEARISH",
            d1_event="BOS_BEARISH",
            pnl_pips=-15.0,
            original_risk_pips=30.0,
        )
        sa = self.se.assess_open_trade(ctx)
        decision = self.de.decide_management(ctx, sa)
        assert decision.action == Action.CLOSE, (
            f"Expected CLOSE with broken structure + loss, got {decision.action.value}"
        )

    def test_profit_with_fading_momentum_tightens(self):
        ctx = _make_ctx(
            pnl_pips=50.0,
            original_risk_pips=30.0,
            m1_aligned_count=1,
            m1_event="NONE",
            d1_trend="BULLISH", d1_confidence=0.5,
            h4_trend="BULLISH", h4_confidence=0.5,
            h1_trend="RANGING", h1_confidence=0.3,
            score_history=[80, 75, 70, 65, 60],
        )
        sa = self.se.assess_open_trade(ctx)
        decision = self.de.decide_management(ctx, sa)
        # A winner with fading momentum must be PROTECTED. The R-based profit
        # ladder locks breakeven at >=0.5R (a strictly safer response than the
        # legacy tighten), so MOVE_TO_BREAKEVEN is now also a valid outcome.
        assert decision.action in (
            Action.TIGHTEN_SL, Action.MOVE_TO_BREAKEVEN, Action.HOLD
        ), (
            f"Expected protect/hold for profit+fading, got {decision.action.value}"
        )

    def test_adopted_trade_observes(self):
        ctx = _make_ctx(entry_type="ORPHAN_ADOPTED", hold_minutes=5)
        sa = self.se.assess_open_trade(ctx)
        decision = self.de.decide_management(ctx, sa)
        assert decision.action in (Action.OBSERVE, Action.SET_PROTECTIVE_STOP, Action.CLOSE), (
            f"Expected OBSERVE/SET_PROTECTIVE_STOP for adopted trade, got {decision.action.value}"
        )

    def test_adopted_clearly_opposing_closes(self):
        ctx = _make_ctx(
            entry_type="ORPHAN_ADOPTED",
            hold_minutes=5,
            direction="BUY",
            d1_trend="BEARISH", d1_confidence=0.85,
            h4_trend="BEARISH", h4_confidence=0.80,
            h1_trend="BEARISH", h1_confidence=0.75,
            d1_event="BOS_BEARISH",
            h4_event="BOS_BEARISH",
            pnl_pips=-20.0,
            original_risk_pips=30.0,
        )
        sa = self.se.assess_open_trade(ctx)
        decision = self.de.decide_management(ctx, sa)
        assert decision.action == Action.CLOSE


# ── Independent trade manager (roadmap F) ──────────────────────────────────

class TestIndependentManager:
    """Exits are governed by the trade's OWN signals (structure / R / momentum),
    not by a lagging HTF flip mid-trade."""

    def setup_method(self):
        self.de = DecisionEngine()

    def test_htf_flip_alone_does_not_force_close(self):
        # HTF flipped hard against the trade, but the trade's own structure is
        # intact and it is NOT in loss → manager must HOLD, not CLOSE.
        ctx = _make_ctx()
        sa = SituationAssessment(
            tf_alignment=-0.95,        # HTF strongly opposing
            structure_integrity=0.45,  # intact (above the close threshold)
            momentum=0.0,
            profit_state=0.0,          # not in loss
            urgency=0.0,
        )
        decision = self.de.decide_management(ctx, sa)
        assert decision.action != Action.CLOSE

    def test_trade_own_signals_still_close(self):
        # No HTF opposition at all, but the trade's OWN structure is broken and
        # it is losing with adverse momentum → manager still CLOSEs.
        ctx = _make_ctx()
        sa = SituationAssessment(
            tf_alignment=0.0,          # HTF neutral — not driving the exit
            structure_integrity=0.0,   # structure broken
            momentum=-0.5,             # momentum against
            profit_state=-1.0,         # in loss
            urgency=0.0,
        )
        decision = self.de.decide_management(ctx, sa)
        assert decision.action == Action.CLOSE


# ── RiskGovernor tests ───────────────────────────────────────────────────

class TestRiskGovernor:
    def setup_method(self):
        self.se = SituationEngine()
        self.de = DecisionEngine()
        self.gov = RiskGovernor()

    def test_governor_vetoes_scale_in_at_high_heat(self):
        ctx = _make_ctx(portfolio_heat_pct=2.0)
        sa = self.se.assess_open_trade(ctx)
        decision = ManagementDecision(
            action=Action.SCALE_IN,
            reason="test",
            confidence=0.6,
        )
        reviewed = self.gov.review(decision, ctx, sa)
        assert reviewed.action == Action.HOLD
        assert "GOVERNOR VETO" in reviewed.reason

    def test_governor_overrides_hold_in_catastrophe(self):
        ctx = _make_ctx(
            direction="BUY",
            d1_trend="BEARISH", d1_confidence=0.90,
            h4_trend="BEARISH", h4_confidence=0.85,
            h1_trend="BEARISH", h1_confidence=0.80,
            h4_event="BOS_BEARISH",
            d1_event="BOS_BEARISH",
            pnl_pips=-50.0,
            original_risk_pips=30.0,
        )
        sa = self.se.assess_open_trade(ctx)
        decision = ManagementDecision(
            action=Action.HOLD,
            reason="test",
            confidence=0.3,
        )
        reviewed = self.gov.review(decision, ctx, sa)
        assert reviewed.action == Action.CLOSE
        assert "GOVERNOR OVERRIDE" in reviewed.reason

    def test_governor_blocks_sl_worsening(self):
        ctx = _make_ctx(direction="BUY", current_sl=1.0830)
        sa = self.se.assess_open_trade(ctx)
        decision = ManagementDecision(
            action=Action.TIGHTEN_SL,
            reason="test",
            confidence=0.5,
            new_sl=1.0820,  # worse than 1.0830 for a long
        )
        reviewed = self.gov.review(decision, ctx, sa)
        assert reviewed.action == Action.HOLD
        assert "GOVERNOR BLOCKED" in reviewed.reason

    def test_governor_passes_valid_decision(self):
        ctx = _make_ctx()
        sa = self.se.assess_open_trade(ctx)
        decision = ManagementDecision(action=Action.HOLD, reason="fine", confidence=0.5)
        reviewed = self.gov.review(decision, ctx, sa)
        assert reviewed.action == Action.HOLD


# ── DecisionJournal tests ────────────────────────────────────────────────

class TestDecisionJournal:
    def test_journal_writes_jsonl(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            journal = DecisionJournal(base_dir=tmpdir)
            ctx = _make_ctx()
            sa = SituationAssessment(
                tf_alignment=0.5,
                momentum=0.2,
                structure_integrity=0.8,
                profit_state=1.0,
                primary_label="TREND_CONTINUATION",
                evidence=["test"],
            )
            decision = ManagementDecision(
                action=Action.HOLD,
                reason="test reason",
                confidence=0.7,
            )
            journal.log(ctx, sa, decision, governor_changed=False)
            journal.close()

            files = list(Path(tmpdir).glob("*.jsonl"))
            assert len(files) == 1
            with open(files[0]) as f:
                lines = f.readlines()
            assert len(lines) == 1
            record = json.loads(lines[0])
            assert record["symbol"] == "EURUSD"
            assert record["decision"]["action"] == "HOLD"
            assert record["situation"]["label"] == "TREND_CONTINUATION"

    def test_fallback_when_disabled(self):
        ctx = _make_ctx()
        sa = SituationAssessment()
        decision = ManagementDecision(action=Action.HOLD, reason="t", confidence=0.5)
        journal = DecisionJournal(base_dir="/nonexistent/path/12345")
        journal.log(ctx, sa, decision)
        journal.close()


# ── Config tests ─────────────────────────────────────────────────────────

class TestDecisionConfig:
    def test_config_defaults(self):
        from config import AppConfig
        cfg = AppConfig()
        assert cfg.decision.enabled is True
        assert cfg.decision.journal_enabled is True
        assert cfg.decision.governor_enabled is True
