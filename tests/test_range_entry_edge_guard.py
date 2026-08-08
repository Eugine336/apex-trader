"""
APEX TRADER — No-directional-edge range guard.

A flat "range" setup (|tf_alignment| below the HTF-lean threshold) with no
strong module-panel consensus has no directional edge. The decision engine
must SKIP it rather than enter on zone shape + R:R alone (the dominant
slow-bleed loss pattern in the audit), while leaving genuinely directional
setups — real HTF alignment or a strong consensus panel — untouched.
"""

from decision.context import EntryContext
from decision.situation import SituationAssessment
from decision.engine import DecisionEngine
from decision.actions import EntryAction


def _ctx(**overrides) -> EntryContext:
    defaults = dict(
        symbol="AUDUSD",
        direction="LONG",
        scan_score=75,
        scan_direction="LONG",
        entry_type="FVG_OB_OVERLAP",
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
        # Flat HTF stack → a true range (not counter-HTF, so not a reversal).
        d1_trend="RANGING",
        d1_confidence=0.30,
        h4_trend="RANGING",
        h4_confidence=0.30,
        h1_trend="RANGING",
        h1_confidence=0.30,
        m1_trend="BULLISH",
        m1_aligned_count=2,
        session_name="LONDON",
        session_tradeable=True,
        open_trade_count=1,
        max_open_trades=5,
        current_spread=1.2,
        typical_spread=1.0,
        regime="RANGING",
    )
    defaults.update(overrides)
    return EntryContext(**defaults)


def _no_edge_sa(**overrides) -> SituationAssessment:
    # Mirrors the AUDUSD pattern in the audit log: flat alignment, good-looking
    # zone, favourable R:R, no real panel conviction.
    defaults = dict(
        tf_alignment=0.15,
        momentum=0.0,
        structure_integrity=0.80,
        read_confidence=0.80,
        urgency=0.0,
        consensus_alignment=0.0,
    )
    defaults.update(overrides)
    return SituationAssessment(**defaults)


class TestNoEdgeRangeGuard:
    def test_skips_when_guard_on(self):
        eng = DecisionEngine(range_edge_required=True)
        d = eng.decide_entry(_ctx(), _no_edge_sa())
        assert not d.should_enter
        assert d.action == EntryAction.SKIP
        assert "no directional edge" in d.reason

    def test_enters_when_guard_off_legacy(self):
        # Same setup is taken by the legacy engine — proving the guard, not the
        # scenario, is what changes the outcome.
        eng = DecisionEngine(range_edge_required=False)
        d = eng.decide_entry(_ctx(), _no_edge_sa())
        assert d.should_enter

    def test_strong_consensus_supplies_edge(self):
        eng = DecisionEngine(range_edge_required=True)
        d = eng.decide_entry(_ctx(), _no_edge_sa(consensus_alignment=0.6))
        assert d.should_enter

    def test_weak_consensus_still_skips(self):
        eng = DecisionEngine(range_edge_required=True, range_edge_consensus_min=0.40)
        d = eng.decide_entry(_ctx(), _no_edge_sa(consensus_alignment=0.25))
        assert not d.should_enter

    def test_htf_aligned_unaffected(self):
        eng = DecisionEngine(range_edge_required=True)
        d = eng.decide_entry(_ctx(), _no_edge_sa(tf_alignment=0.6))
        assert d.should_enter

    def test_guard_is_not_softened(self):
        # Even with the gate dimmer on, a no-edge range is a genuine no-trade,
        # not a mildly-negative margin for the orchestrator to size.
        eng = DecisionEngine(
            range_edge_required=True, soften_gate=True, gate_safety_margin=-1.0,
        )
        d = eng.decide_entry(_ctx(), _no_edge_sa())
        assert not d.should_enter
        assert not d.gate_softened
        assert "DE-SOFTENED" not in d.reason
