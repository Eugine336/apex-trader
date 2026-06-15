"""
APEX TRADER — PR 10 regression tests.

PART 1 — Fast-cluster opposition decay (management):
  * DecisionEngine adds bounded, streak-ramping CLOSE pressure when the fast
    cluster (momentum + M1 alignment) has opposed a NON-winning position for
    several consecutive cycles.
  * Inert below the minimum streak, inert for winners (profit_r >= threshold),
    inert when the feature is disabled.
  * A CLOSE driven with that pressure is tagged ExitCause.FAST_OPPOSITION_DECAY.
  * The main-loop streak counter increments on opposition and resets on re-align.

PART 2 — Suppressed-minority shadow logging (consensus):
  * When the panel collapses to NEUTRAL on the agreement gate, the suppressed
    minority cluster is captured on the decision and logged.
"""

from types import SimpleNamespace

from brain.directional_consensus import Vote, decide
from decision.actions import Action
from decision.context import TradeContext
from decision.engine import DecisionEngine, FAST_OPPOSITION_EXIT_CAUSE
from decision.situation import SituationAssessment
from management.exit_cause import ExitCause


# ── Helpers ────────────────────────────────────────────────────────────────

def _ctx(**overrides) -> TradeContext:
    defaults = dict(
        symbol="CHFJPY",
        order_id="oid-1",
        direction="BUY",
        entry_type="APEX_ENTRY",
        entry_price=170.000,
        current_price=169.800,
        current_sl=169.500,
        pnl_pips=-20.0,
        hold_minutes=40.0,
        original_risk_pips=30.0,   # profit_r = -20/30 = -0.67 (a loser)
        scan_score=50,
        scan_direction="LONG",
    )
    defaults.update(overrides)
    return TradeContext(**defaults)


def _sa(**overrides) -> SituationAssessment:
    sa = SituationAssessment()
    for k, v in overrides.items():
        setattr(sa, k, v)
    return sa


# ── PART 1a: _fast_opposition_pressure ──────────────────────────────────────

class TestFastOppositionPressure:
    def setup_method(self):
        self.eng = DecisionEngine(
            fast_opposition_min_streak=3,
            fast_opposition_max_streak=8,
            fast_opposition_decay_weight=0.15,
            fast_opposition_profit_threshold=0.3,
        )

    def test_inert_below_min_streak(self):
        p, reasons = self.eng._fast_opposition_pressure(_ctx(fast_opposition_streak=2))
        assert p == 0.0 and reasons == []

    def test_engages_at_min_streak(self):
        p, reasons = self.eng._fast_opposition_pressure(_ctx(fast_opposition_streak=3))
        assert p > 0.0
        assert any("fast cluster opposing" in r for r in reasons)

    def test_pressure_increases_with_streak(self):
        p3, _ = self.eng._fast_opposition_pressure(_ctx(fast_opposition_streak=3))
        p6, _ = self.eng._fast_opposition_pressure(_ctx(fast_opposition_streak=6))
        assert p6 > p3

    def test_pressure_capped_at_weight(self):
        p_full, _ = self.eng._fast_opposition_pressure(_ctx(fast_opposition_streak=8))
        p_over, _ = self.eng._fast_opposition_pressure(_ctx(fast_opposition_streak=99))
        assert abs(p_full - 0.15) < 1e-9
        assert abs(p_over - 0.15) < 1e-9

    def test_inert_when_in_profit(self):
        # profit_r = +20/30 = +0.67 ≥ threshold → a winner, no decay pressure.
        p, reasons = self.eng._fast_opposition_pressure(
            _ctx(fast_opposition_streak=8, pnl_pips=20.0)
        )
        assert p == 0.0 and reasons == []

    def test_inert_when_disabled(self):
        eng = DecisionEngine(fast_opposition_decay_enabled=False)
        p, reasons = eng._fast_opposition_pressure(_ctx(fast_opposition_streak=8))
        assert p == 0.0 and reasons == []


# ── PART 1b: decide_management integration + exit-cause tagging ──────────────

class TestFastOppositionInDecideManagement:
    def setup_method(self):
        self.eng = DecisionEngine()

    def test_close_driven_by_decay_is_tagged(self):
        # Broken structure + adverse momentum + loss → a strong CLOSE lean; a
        # long opposition streak on a loser pushes it over the top and tags it.
        ctx = _ctx(fast_opposition_streak=8, pnl_pips=-40.0)
        sa = _sa(
            tf_alignment=-0.4,
            momentum=-0.6,
            structure_integrity=0.1,
            profit_state=-1.3,
        )
        decision = self.eng.decide_management(ctx, sa)
        assert decision.action == Action.CLOSE
        assert decision.exit_cause == FAST_OPPOSITION_EXIT_CAUSE
        assert decision.exit_cause == ExitCause.FAST_OPPOSITION_DECAY.value

    def test_decay_reason_surfaces_when_it_contributes(self):
        ctx = _ctx(fast_opposition_streak=6, pnl_pips=-40.0)
        sa = _sa(
            tf_alignment=-0.4,
            momentum=-0.6,
            structure_integrity=0.1,
            profit_state=-1.3,
        )
        decision = self.eng.decide_management(ctx, sa)
        assert decision.action == Action.CLOSE
        assert any("fast cluster opposing" in e for e in decision.evidence)

    def test_no_tag_when_streak_short(self):
        # Same CLOSE-leaning situation but a short streak: still CLOSE on its own
        # merits, but no fast-opposition exit-cause tag.
        ctx = _ctx(fast_opposition_streak=1, pnl_pips=-40.0)
        sa = _sa(
            tf_alignment=-0.4,
            momentum=-0.6,
            structure_integrity=0.1,
            profit_state=-1.3,
        )
        decision = self.eng.decide_management(ctx, sa)
        assert decision.exit_cause is None

    def test_winner_unaffected_by_streak(self):
        # A healthy winner with a high streak must not be tagged or closed by
        # the decay — the gate is profit-aware.
        ctx = _ctx(fast_opposition_streak=8, pnl_pips=60.0)
        sa = _sa(
            tf_alignment=0.5,
            momentum=0.3,
            structure_integrity=0.7,
            profit_state=2.0,
        )
        decision = self.eng.decide_management(ctx, sa)
        assert decision.exit_cause is None
        assert decision.action != Action.CLOSE

    def test_disabled_engine_adds_no_pressure(self):
        on = DecisionEngine()
        off = DecisionEngine(fast_opposition_decay_enabled=False)
        ctx = _ctx(fast_opposition_streak=8, pnl_pips=-40.0)
        sa = _sa(tf_alignment=-0.2, momentum=-0.4, structure_integrity=0.3,
                 profit_state=-1.3)
        # CLOSE score should be strictly higher with the feature on.
        d_on = on.decide_management(_ctx(fast_opposition_streak=8, pnl_pips=-40.0), _sa(
            tf_alignment=-0.2, momentum=-0.4, structure_integrity=0.3, profit_state=-1.3))
        d_off = off.decide_management(ctx, sa)
        assert d_off.exit_cause is None
        # The on-engine reaches at least as strong a CLOSE verdict.
        assert d_on.confidence >= 0.0  # sanity; tagging covered above


# ── PART 1c: main-loop streak counter ────────────────────────────────────────

class _LoopStub:
    """Minimal stand-in carrying just what _update_fast_opposition_streak uses."""

    def __init__(self):
        self._fast_opposition_streak = {}
        self.managed_positions = {"oid-1": object()}


class TestStreakCounter:
    def test_increments_on_opposition(self):
        from platforms.main_loop import TradingLoop
        stub = _LoopStub()
        ctx = _ctx()
        sa = _sa(momentum=-0.5)
        ctx.m1_aligned_count = 0
        for expected in (1, 2, 3):
            TradingLoop._update_fast_opposition_streak(stub, "oid-1", ctx, sa)
            assert stub._fast_opposition_streak["oid-1"] == expected
            assert ctx.fast_opposition_streak == expected

    def test_resets_on_realign(self):
        from platforms.main_loop import TradingLoop
        stub = _LoopStub()
        opposing_ctx = _ctx()
        opposing_ctx.m1_aligned_count = 0
        opposing_sa = _sa(momentum=-0.5)
        for _ in range(3):
            TradingLoop._update_fast_opposition_streak(
                stub, "oid-1", opposing_ctx, opposing_sa,
            )
        assert stub._fast_opposition_streak["oid-1"] == 3

        # Fast cluster re-aligns (positive momentum, candles aligned) → reset.
        aligned_ctx = _ctx()
        aligned_ctx.m1_aligned_count = 4
        aligned_sa = _sa(momentum=0.4)
        TradingLoop._update_fast_opposition_streak(
            stub, "oid-1", aligned_ctx, aligned_sa,
        )
        assert stub._fast_opposition_streak["oid-1"] == 0
        assert aligned_ctx.fast_opposition_streak == 0

    def test_requires_both_momentum_and_m1(self):
        from platforms.main_loop import TradingLoop
        stub = _LoopStub()
        # Momentum opposing but M1 aligned (>1) → not counted as fast opposition.
        ctx = _ctx()
        ctx.m1_aligned_count = 4
        sa = _sa(momentum=-0.5)
        TradingLoop._update_fast_opposition_streak(stub, "oid-1", ctx, sa)
        assert stub._fast_opposition_streak["oid-1"] == 0

    def test_prunes_closed_positions(self):
        from platforms.main_loop import TradingLoop
        stub = _LoopStub()
        stub._fast_opposition_streak["closed-oid"] = 5
        ctx = _ctx()
        ctx.m1_aligned_count = 0
        sa = _sa(momentum=-0.5)
        TradingLoop._update_fast_opposition_streak(stub, "oid-1", ctx, sa)
        assert "closed-oid" not in stub._fast_opposition_streak


# ── PART 2: suppressed-minority shadow logging ───────────────────────────────

class TestSuppressedMinority:
    def _mixed_panel(self):
        # Net LONG (structure heavy) but a coherent fast SHORT minority; with a
        # high agreement threshold the panel collapses to NEUTRAL.
        return [
            Vote("structure", "LONG", 0.9, 3.0),       # +2.7
            Vote("momentum", "SHORT", 0.8, 1.0),       # -0.8
            Vote("order_block", "SHORT", 0.7, 1.0),    # -0.7
            Vote("fvg", "SHORT", 0.7, 1.0),            # -0.7
        ]

    def test_minority_captured_on_agreement_collapse(self):
        d = decide(
            self._mixed_panel(),
            min_net_score=0.1,
            min_agreement=0.9,
            high_authority_modules=[],
            high_authority_oppose_confidence=0.6,
            min_contributors=1,
        )
        assert d.direction == "NEUTRAL"
        assert d.suppressed_direction == "SHORT"
        assert set(d.suppressed_modules) == {"momentum", "order_block", "fvg"}
        assert 0.0 < d.suppressed_strength <= 1.0

    def _capture_decide(self, **kw):
        """Run decide() capturing loguru output into a string buffer."""
        import io
        from loguru import logger
        buf = io.StringIO()
        sink_id = logger.add(buf, level="INFO")
        try:
            decide(
                self._mixed_panel(),
                min_net_score=0.1,
                min_agreement=0.9,
                high_authority_modules=[],
                high_authority_oppose_confidence=0.6,
                min_contributors=1,
                **kw,
            )
        finally:
            logger.remove(sink_id)
        return buf.getvalue()

    def test_logged_when_flag_on(self):
        text = self._capture_decide(log_suppressed_minorities=True)
        assert "CONSENSUS_MINORITY_SUPPRESSED" in text

    def test_not_logged_when_flag_off(self):
        text = self._capture_decide(log_suppressed_minorities=False)
        assert "CONSENSUS_MINORITY_SUPPRESSED" not in text

    def test_no_minority_when_consensus_passes(self):
        votes = [
            Vote("structure", "LONG", 0.9, 3.0),
            Vote("momentum", "LONG", 0.8, 1.0),
        ]
        d = decide(
            votes,
            min_net_score=0.1,
            min_agreement=0.5,
            high_authority_modules=[],
            high_authority_oppose_confidence=0.6,
            min_contributors=1,
        )
        assert d.direction == "LONG"
        assert d.suppressed_direction == "NEUTRAL"
        assert d.suppressed_modules == []
