"""
Tests for the R-based profit protection ladder (DecisionEngine).

The legacy profit-protection gates key off dollar/pip economic profit
(thesis-secure: ≥$15 / ≥12 pips) or large R thresholds (TIGHTEN_SL: ≥1.5R),
which a min-lot micro position can never reach — so a winner that peaks at
+0.9R / +$0.39 rides all the way back to scratch while HOLD wins every cycle.

The ladder fires on R-multiples ONLY (``sa.profit_state``), so it protects
identically at 0.01 lots and 10 lots:

  Tier 1 (≥0.5R)  → MOVE_TO_BREAKEVEN  (can never become a loss)
  Tier 2 (≥1.0R)  → TIGHTEN_SL         (lock +0.3R, only when momentum fades)
  Tier 3 (≥0.8R)  → PARTIAL_CLOSE 50%  (only in an exhaustion regime)
  Tier 4          → thesis-secure R gate (micro-scale "is the reason valid?")

Every tier is additive (stacks onto the existing score) and never forces a
CLOSE. The whole ladder is inert when ``profit_protect_enabled`` is False.
"""

from decision.actions import Action
from decision.context import TradeContext
from decision.engine import DecisionEngine
from decision.situation import SituationAssessment


def _ctx(**overrides) -> TradeContext:
    defaults = dict(
        symbol="EURUSD",
        order_id="oid-1",
        direction="BUY",
        entry_type="APEX_ENTRY",
        entry_price=1.10000,
        current_price=1.10060,
        current_sl=1.09800,  # below entry → BE would improve the stop
        pnl_pips=6.0,
        pnl_dollars=0.40,  # micro economic profit (below the $15 gate)
        hold_minutes=20.0,
        at_breakeven=False,
        partial_closed=False,
        lots=0.01,
        original_risk_pips=20.0,
        score_history=[],
    )
    defaults.update(overrides)
    return TradeContext(**defaults)


def _sa(**overrides) -> SituationAssessment:
    defaults = dict(
        tf_alignment=0.0,
        structure_integrity=0.5,
        momentum=0.0,
        profit_state=0.6,  # R-multiple the ladder keys off
        urgency=0.0,
        read_confidence=1.0,
        primary_label="MIXED",
    )
    defaults.update(overrides)
    return SituationAssessment(**defaults)


def _engine(**kw) -> DecisionEngine:
    return DecisionEngine(**kw)


# ─────────────────────────────────────────────────────────────────────────
# Tier 1 — breakeven lock at ≥ 0.5R
# ─────────────────────────────────────────────────────────────────────────
class TestTier1BreakevenLock:
    def test_fires_at_threshold(self):
        eng = _engine()
        d = eng.decide_management(_ctx(), _sa(profit_state=0.5))
        assert d.action == Action.MOVE_TO_BREAKEVEN

    def test_fires_above_threshold(self):
        eng = _engine()
        d = eng.decide_management(_ctx(), _sa(profit_state=0.9))
        assert d.action == Action.MOVE_TO_BREAKEVEN

    def test_does_not_fire_below_threshold(self):
        eng = _engine()
        # 0.4R < 0.5R lock — HOLD stands, no breakeven move
        d = eng.decide_management(_ctx(), _sa(profit_state=0.4))
        assert d.action != Action.MOVE_TO_BREAKEVEN

    def test_skipped_when_already_at_breakeven(self):
        eng = _engine()
        d = eng.decide_management(_ctx(at_breakeven=True, current_sl=1.10000), _sa(profit_state=0.6))
        assert d.action != Action.MOVE_TO_BREAKEVEN

    def test_skipped_when_breakeven_would_loosen_stop(self):
        eng = _engine()
        # Stop already past entry in the profit direction → BE is no improvement
        d = eng.decide_management(_ctx(current_sl=1.10050), _sa(profit_state=0.6))
        assert d.action != Action.MOVE_TO_BREAKEVEN

    def test_short_breakeven_lock(self):
        eng = _engine()
        ctx = _ctx(direction="SELL", entry_price=1.10000, current_price=1.09940, current_sl=1.10200)
        d = eng.decide_management(ctx, _sa(profit_state=0.7))
        assert d.action == Action.MOVE_TO_BREAKEVEN


# ─────────────────────────────────────────────────────────────────────────
# Tier 2 — profit trail at ≥ 1.0R (lock +0.3R)
# ─────────────────────────────────────────────────────────────────────────
class TestTier2ProfitTrail:
    def test_fires_and_locks_profit_when_momentum_fades(self):
        eng = _engine()
        # at_breakeven so Tier 1 is out of the way → Tier 2 is the protector
        ctx = _ctx(at_breakeven=True, current_sl=1.10000)
        d = eng.decide_management(ctx, _sa(profit_state=1.2, momentum=-0.3))
        assert d.action == Action.TIGHTEN_SL
        # 0.3R of 20 pips = 6 pips from entry → 1.10000 + 0.0006
        assert abs(d.new_sl - 1.10060) < 1e-6
        assert d.new_sl > ctx.current_sl  # only ever tightens

    def test_short_profit_trail(self):
        eng = _engine()
        ctx = _ctx(direction="SELL", entry_price=1.10000, current_price=1.09800, current_sl=1.10000, at_breakeven=True)
        d = eng.decide_management(ctx, _sa(profit_state=1.3, momentum=-0.3))
        assert d.action == Action.TIGHTEN_SL
        assert abs(d.new_sl - 1.09940) < 1e-6  # entry - 6 pips

    def test_does_not_fire_below_threshold(self):
        eng = _engine()
        ctx = _ctx(at_breakeven=True, current_sl=1.10000)
        # 0.9R < 1.0R trail
        d = eng.decide_management(ctx, _sa(profit_state=0.9, momentum=-0.3))
        assert d.action != Action.TIGHTEN_SL

    def test_only_tightens_never_loosens(self):
        eng = _engine()
        # current_sl already tighter than the +0.3R lock target → no trail
        ctx = _ctx(at_breakeven=True, current_sl=1.10100)
        d = eng.decide_management(ctx, _sa(profit_state=1.5, momentum=-0.3))
        assert d.action != Action.TIGHTEN_SL

    def test_fires_in_exhaustion_even_with_flat_momentum(self):
        eng = _engine()
        ctx = _ctx(at_breakeven=True, current_sl=1.10000)
        d = eng.decide_management(ctx, _sa(profit_state=1.2, momentum=0.0, primary_label="TREND_EXHAUSTION"))
        assert d.action == Action.TIGHTEN_SL


# ─────────────────────────────────────────────────────────────────────────
# Tier 3 — partial close 50% at ≥ 0.8R in an exhaustion regime
# ─────────────────────────────────────────────────────────────────────────
class TestTier3PartialClose:
    def test_fires_in_exhaustion(self):
        eng = _engine()
        ctx = _ctx(at_breakeven=True, current_sl=1.10000)
        d = eng.decide_management(ctx, _sa(profit_state=0.9, momentum=-0.3, primary_label="TREND_EXHAUSTION"))
        assert d.action == Action.PARTIAL_CLOSE
        assert abs(d.partial_ratio - 0.5) < 1e-9

    def test_does_not_fire_below_threshold(self):
        eng = _engine()
        ctx = _ctx(at_breakeven=True, current_sl=1.10000)
        d = eng.decide_management(ctx, _sa(profit_state=0.7, momentum=-0.3, primary_label="TREND_EXHAUSTION"))
        assert d.action != Action.PARTIAL_CLOSE

    def test_does_not_fire_outside_exhaustion_when_required(self):
        eng = _engine()
        ctx = _ctx(at_breakeven=True, current_sl=1.10000)
        # RANGE regime, flat momentum → exhaustion proxy False → no partial
        d = eng.decide_management(ctx, _sa(profit_state=0.9, momentum=0.0, primary_label="RANGE"))
        assert d.action != Action.PARTIAL_CLOSE

    def test_fires_anywhere_when_regime_not_required(self):
        eng = _engine(partial_close_regime_required=False)
        ctx = _ctx(at_breakeven=True, current_sl=1.10000)
        d = eng.decide_management(ctx, _sa(profit_state=0.9, momentum=0.0, primary_label="RANGE"))
        assert d.action == Action.PARTIAL_CLOSE

    def test_fires_once_only(self):
        eng = _engine()
        ctx = _ctx(at_breakeven=True, current_sl=1.10000, partial_closed=True)
        d = eng.decide_management(ctx, _sa(profit_state=0.9, momentum=-0.3, primary_label="TREND_EXHAUSTION"))
        assert d.action != Action.PARTIAL_CLOSE


# ─────────────────────────────────────────────────────────────────────────
# Tier 4 — thesis-secure R gate (micro-scale)
# ─────────────────────────────────────────────────────────────────────────
class TestTier4ThesisSecureRGate:
    def test_micro_profit_secures_on_decay(self):
        eng = _engine()
        # at_breakeven so the ladder's Tier 1 is out of the way; profit 0.6R is
        # below Tier 2/3 → would-be HOLD → thesis-secure fires via the R gate
        # even though dollar/pip profit is far below the absolute gates.
        ctx = _ctx(
            at_breakeven=True,
            current_sl=1.10000,
            pnl_pips=3.0,
            pnl_dollars=0.40,
        )
        d = eng.decide_management(
            ctx,
            _sa(profit_state=0.6, structure_integrity=0.35, momentum=-0.6),
        )
        assert d.action in (Action.SET_PROTECTIVE_STOP, Action.MOVE_TO_BREAKEVEN)

    def test_micro_profit_below_r_gate_holds(self):
        eng = _engine()
        ctx = _ctx(
            at_breakeven=True,
            current_sl=1.10000,
            pnl_pips=2.0,
            pnl_dollars=0.20,
        )
        # 0.3R < 0.5R R gate, and dollar/pip below their gates → HOLD
        d = eng.decide_management(
            ctx,
            _sa(profit_state=0.3, structure_integrity=0.35, momentum=-0.6),
        )
        assert d.action == Action.HOLD


# ─────────────────────────────────────────────────────────────────────────
# Config off / interaction guarantees
# ─────────────────────────────────────────────────────────────────────────
class TestLadderDisabledAndInteractions:
    def test_disabled_is_inert(self):
        eng = _engine(profit_protect_enabled=False)
        # Same context that fires Tier 1 when enabled; with the ladder off and a
        # healthy read the thesis layer stands down → HOLD (no breakeven move).
        d = eng.decide_management(_ctx(), _sa(profit_state=0.6, momentum=0.1, structure_integrity=0.6))
        assert d.action != Action.MOVE_TO_BREAKEVEN

    def test_enabled_protects_same_scenario(self):
        eng = _engine()
        d = eng.decide_management(_ctx(), _sa(profit_state=0.6, momentum=0.1, structure_integrity=0.6))
        assert d.action == Action.MOVE_TO_BREAKEVEN

    def test_ladder_never_fires_in_loss(self):
        eng = _engine()
        # In a loss none of the profit tiers may add score
        d = eng.decide_management(
            _ctx(pnl_pips=-12.0, pnl_dollars=-2.0),
            _sa(profit_state=-1.0, structure_integrity=0.6, momentum=-0.1),
        )
        assert d.action not in (Action.MOVE_TO_BREAKEVEN, Action.PARTIAL_CLOSE)

    def test_real_close_still_wins_over_ladder(self):
        eng = _engine()
        # Broken structure + loss → a real CLOSE; the ladder is silent in loss
        # so it cannot suppress the exit.
        d = eng.decide_management(
            _ctx(pnl_pips=-20.0, pnl_dollars=-3.0),
            _sa(profit_state=-1.5, structure_integrity=0.05, momentum=-0.5),
        )
        assert d.action == Action.CLOSE

    def test_breakeven_preferred_over_partial_when_both_eligible(self):
        eng = _engine()
        # Not at breakeven, profit ≥0.8R in exhaustion → both Tier 1 and Tier 3
        # are eligible; the safer breakeven lock (higher score) must win first.
        d = eng.decide_management(
            _ctx(),
            _sa(profit_state=0.9, momentum=-0.3, primary_label="TREND_EXHAUSTION"),
        )
        assert d.action == Action.MOVE_TO_BREAKEVEN


# ─────────────────────────────────────────────────────────────────────────
# Live-replay: the GBPUSD micro scenario from production
# ─────────────────────────────────────────────────────────────────────────
class TestGbpusdMicroScenario:
    """The exact micro-lot scenario the ladder was built to fix.

    GBPUSD SHORT, ~4.5-pip reconstructed risk, peaks at +0.9R / +$0.39, regime
    oscillating MIXED ↔ TREND_EXHAUSTION. Pre-ladder this returned HOLD every
    cycle and gave the move back; the ladder must protect it.
    """

    def _ctx(self, **kw):
        d = dict(
            symbol="GBPUSD",
            order_id="gbp-1",
            direction="SELL",
            entry_type="APEX_ENTRY",
            entry_price=1.32219,
            current_price=1.32180,
            current_sl=1.32264,
            pnl_pips=3.9,
            pnl_dollars=0.39,
            lots=0.01,
            original_risk_pips=4.5,
            at_breakeven=False,
            partial_closed=False,
        )
        d.update(kw)
        return TradeContext(**d)

    def test_breakeven_locks_at_half_r(self):
        eng = _engine()
        d = eng.decide_management(
            self._ctx(),
            _sa(profit_state=0.5, momentum=-0.1, primary_label="MIXED"),
        )
        assert d.action == Action.MOVE_TO_BREAKEVEN

    def test_partial_banks_in_exhaustion(self):
        eng = _engine()
        d = eng.decide_management(
            self._ctx(at_breakeven=True, current_sl=1.32219),
            _sa(profit_state=0.8, momentum=-0.3, primary_label="TREND_EXHAUSTION"),
        )
        assert d.action == Action.PARTIAL_CLOSE
        assert abs(d.partial_ratio - 0.5) < 1e-9
