"""
Tests for the thesis-deterioration secure (roadmap G).

The exit brain's profit-securing actions (MOVE_TO_BREAKEVEN, TIGHTEN_SL) are
all gated on profit_state (an R-multiple). Adopted/orphan trades carry a
reconstructed risk, so R is detached from real economic profit — a winner can
run +$X while profit_state ≈ 0.2R, below every R-gate, and HOLD wins by default
because nothing else can score. This layer asks "is the reason still valid?"
and secures profit when meaningful economic profit exists AND the thesis is
deteriorating, unless it's a healthy pullback.
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
        entry_type="ORPHAN_ADOPTED",
        entry_price=1.08500,
        current_price=1.08620,
        current_sl=1.08200,        # below entry → not yet protected
        pnl_pips=12.0,
        pnl_dollars=18.0,
        hold_minutes=45.0,         # past the 10-min adopted observation window
        at_breakeven=False,
        tp1_hit=False,
        trailing=False,
        lots=0.5,
        original_risk_pips=60.0,   # wide reconstructed risk → profit_state tiny
        scan_score=55,
        scan_direction="LONG",
        score_history=[70, 60, 48],
        open_trade_count=1,
        max_open_trades=5,
    )
    defaults.update(overrides)
    return TradeContext(**defaults)


def _sa(**overrides) -> SituationAssessment:
    defaults = dict(
        tf_alignment=-0.2,
        structure_integrity=0.35,
        momentum=-0.6,
        profit_state=0.2,          # the R the manager sees — below every R-gate
        urgency=0.0,
        read_confidence=0.5,
    )
    defaults.update(overrides)
    return SituationAssessment(**defaults)


def _engine(**kw) -> DecisionEngine:
    return DecisionEngine(**kw)


class TestThesisSecureFires:
    def test_decay_in_profit_locks_protective_stop(self):
        eng = _engine()
        d = eng.decide_management(_ctx(), _sa())
        assert d.action == Action.SET_PROTECTIVE_STOP
        # locks thesis_lock_fraction (0.5) of +12 pips → entry + 6 pips
        assert abs(d.new_sl - 1.08560) < 1e-6
        assert d.new_sl > 1.08200  # improves on the current stop

    def test_short_decay_in_profit_locks_protective_stop(self):
        eng = _engine()
        ctx = _ctx(
            direction="SELL", entry_price=1.08500, current_price=1.08380,
            current_sl=1.08800,
        )
        d = eng.decide_management(ctx, _sa())
        assert d.action == Action.SET_PROTECTIVE_STOP
        assert abs(d.new_sl - 1.08440) < 1e-6  # entry - 6 pips
        assert d.new_sl < 1.08800

    def test_already_at_breakeven_escalates_lock(self):
        eng = _engine()
        # current_sl already at entry (BE); secure should lock further in profit
        d = eng.decide_management(_ctx(at_breakeven=True, current_sl=1.08500), _sa())
        assert d.action == Action.SET_PROTECTIVE_STOP
        assert d.new_sl > 1.08500


class TestThesisSecureHolds:
    def test_healthy_pullback_holds(self):
        eng = _engine()
        # structure intact AND momentum non-negative → healthy pullback
        d = eng.decide_management(_ctx(), _sa(structure_integrity=0.7, momentum=0.1))
        assert d.action == Action.HOLD

    def test_mild_decay_below_threshold_holds(self):
        eng = _engine()
        # momentum barely negative, structure fine, no conviction drop → low score
        d = eng.decide_management(
            _ctx(score_history=[60, 60, 60]),
            _sa(structure_integrity=0.58, momentum=-0.05, tf_alignment=0.0),
        )
        assert d.action == Action.HOLD

    def test_no_economic_profit_holds(self):
        eng = _engine()
        d = eng.decide_management(
            _ctx(pnl_pips=1.0, pnl_dollars=2.0),
            _sa(),
        )
        assert d.action == Action.HOLD

    def test_disabled_holds(self):
        eng = _engine(thesis_secure_enabled=False)
        d = eng.decide_management(_ctx(), _sa())
        assert d.action == Action.HOLD


class TestThesisSecureDoesNotOverrideStrongerVerdict:
    def test_structure_break_still_closes(self):
        eng = _engine()
        # Fully broken structure in a loss → normal scoring already wants CLOSE;
        # the secure override must NOT replace it.
        ctx = _ctx(pnl_pips=-20.0, pnl_dollars=-30.0)
        d = eng.decide_management(ctx, _sa(structure_integrity=0.05, profit_state=-1.5))
        assert d.action == Action.CLOSE


class TestSevereDecayHardClose:
    # Severe decay while in profit, but normal scoring would still HOLD
    # (structure ≥ 0.25 so no normal CLOSE, no opposing scan, in profit so
    # loss-gated branches don't fire). The thesis layer should hard-CLOSE.
    def _severe_sa(self):
        return _sa(structure_integrity=0.26, momentum=-1.0, tf_alignment=-0.29)

    def _severe_ctx(self):
        return _ctx(score_history=[95, 40, 5])  # full conviction collapse

    def test_severe_decay_closes(self):
        eng = _engine()
        d = eng.decide_management(self._severe_ctx(), self._severe_sa())
        assert d.action == Action.CLOSE

    def test_severe_decay_respects_disable(self):
        # close threshold above 1.0 disables the hard-close tier → secures instead
        eng = _engine(thesis_close_threshold=1.5)
        d = eng.decide_management(self._severe_ctx(), self._severe_sa())
        assert d.action == Action.SET_PROTECTIVE_STOP

    def test_severe_decay_without_profit_does_not_close_here(self):
        # No economic profit → thesis layer bows out (normal scoring handles it)
        eng = _engine()
        ctx = _ctx(pnl_pips=1.0, pnl_dollars=2.0, score_history=[95, 40, 5])
        d = eng.decide_management(ctx, self._severe_sa())
        assert d.action != Action.CLOSE

    def test_moderate_decay_still_secures_not_closes(self):
        eng = _engine()
        d = eng.decide_management(_ctx(), _sa())  # deterioration ≈ 0.55
        assert d.action == Action.SET_PROTECTIVE_STOP


class TestDeteriorationScore:
    def test_conviction_collapse_contributes(self):
        eng = _engine()
        # structure/momentum neutral-ish; rely on score-history collapse
        score, ev = eng._thesis_deterioration_score(
            _ctx(score_history=[80, 60, 45]),
            _sa(structure_integrity=0.6, momentum=0.0, tf_alignment=0.0),
        )
        assert score > 0.0
        assert any("conviction" in e for e in ev)

    def test_profit_lock_never_moves_backwards(self):
        eng = _engine()
        # current_sl already above the lock target → no improvement → None
        assert eng._compute_profit_lock_sl(_ctx(current_sl=1.08900)) is None
