"""Tests for the graded RiskGovernor — accumulated risk (#24) and graded
scale-in / near-catastrophe (#35), plus preservation of legacy first-breach
behaviour when ``graded_risk`` is off.
"""

from decision.actions import Action, EntryAction, EntryDecision, ManagementDecision
from decision.context import EntryContext, TradeContext
from decision.governor import RiskGovernor
from decision.situation import SituationAssessment


def _enter(**ctx_over) -> tuple[EntryDecision, EntryContext, SituationAssessment]:
    decision = EntryDecision(
        action=EntryAction.ENTER_MARKET,
        reason="enter",
        conviction=0.7,
        size_multiplier=1.0,
    )
    ctx = EntryContext(
        symbol="EURUSD",
        direction="LONG",
        open_trade_count=1,
        max_open_trades=5,
        portfolio_heat_pct=0.5,
        current_spread=1.0,
        typical_spread=1.0,
        risk_reward_2=2.0,
    )
    for k, v in ctx_over.items():
        setattr(ctx, k, v)
    return decision, ctx, SituationAssessment()


# ── #24 — graded entry review ────────────────────────────────────────────


def test_legacy_vetoes_on_first_analytical_breach():
    gov = RiskGovernor()  # graded_risk defaults off
    decision, ctx, sa = _enter(portfolio_heat_pct=1.9)  # >= 1.8 cap
    out = gov.review_entry(decision, ctx, sa)
    assert out.action == EntryAction.SKIP
    assert out.governor_vetoed


def test_graded_softens_heat_breach_into_dimmer():
    gov = RiskGovernor(graded_risk=True)
    decision, ctx, sa = _enter(portfolio_heat_pct=1.7)  # near 1.8, not at it
    out = gov.review_entry(decision, ctx, sa)
    # No longer killed — flows through as ENTER carrying a graded multiplier.
    assert out.should_enter
    assert out.risk_multiplier < 1.0
    assert "portfolio_heat" in out.risk_near_breaches


def test_graded_measures_all_dimensions_not_just_first():
    gov = RiskGovernor(graded_risk=True)
    # Heat over cap AND R:R below the floor — legacy would have stopped at heat.
    decision, ctx, sa = _enter(portfolio_heat_pct=1.9, risk_reward_2=0.7)
    out = gov.review_entry(decision, ctx, sa)
    assert out.should_enter
    assert "portfolio_heat" in out.risk_near_breaches
    assert "risk_reward" in out.risk_near_breaches
    assert out.risk_multiplier < 1.0


def test_graded_clear_setup_unchanged():
    gov = RiskGovernor(graded_risk=True)
    decision, ctx, sa = _enter()  # everything clear
    out = gov.review_entry(decision, ctx, sa)
    assert out.should_enter
    assert out.risk_multiplier == 1.0


def test_graded_keeps_position_limit_hard():
    gov = RiskGovernor(graded_risk=True)
    decision, ctx, sa = _enter(open_trade_count=5, max_open_trades=5)
    out = gov.review_entry(decision, ctx, sa)
    # Position limit is physics — still a hard veto even in graded mode.
    assert out.action == EntryAction.SKIP
    assert out.governor_vetoed


def test_skip_decision_passes_through_untouched():
    gov = RiskGovernor(graded_risk=True)
    skip = EntryDecision(action=EntryAction.SKIP, reason="de skip")
    _, ctx, sa = _enter()
    assert gov.review_entry(skip, ctx, sa).action == EntryAction.SKIP


# ── #35 — graded scale-in trim ─────────────────────────────────────────────


def _scale_in(scale_lots: float = 1.0) -> ManagementDecision:
    return ManagementDecision(
        action=Action.SCALE_IN, reason="add", scale_lots=scale_lots,
    )


def test_scale_in_hard_veto_over_heat_cap_both_modes():
    for gov in (RiskGovernor(), RiskGovernor(graded_risk=True)):
        ctx = TradeContext(symbol="EURUSD", direction="BUY", portfolio_heat_pct=1.6)
        out = gov.review(_scale_in(), ctx, SituationAssessment())
        assert out.action == Action.HOLD  # blocked above the 1.5 cap


def test_scale_in_trimmed_near_cap_in_graded_mode():
    gov = RiskGovernor(graded_risk=True)
    ctx = TradeContext(symbol="EURUSD", direction="BUY", portfolio_heat_pct=1.35)
    out = gov.review(_scale_in(1.0), ctx, SituationAssessment())
    assert out.action == Action.SCALE_IN
    assert 0.0 < out.scale_lots < 1.0  # sized down, not killed


def test_scale_in_not_trimmed_with_headroom():
    gov = RiskGovernor(graded_risk=True)
    ctx = TradeContext(symbol="EURUSD", direction="BUY", portfolio_heat_pct=1.0)
    out = gov.review(_scale_in(1.0), ctx, SituationAssessment())
    assert out.action == Action.SCALE_IN
    assert out.scale_lots == 1.0


# ── #35 — catastrophe stays hard, near-catastrophe escalates (graded) ───────


def test_catastrophe_hard_close_in_both_modes():
    sa = SituationAssessment(tf_alignment=-0.7, structure_integrity=0.1, profit_state=-1.5)
    for gov in (RiskGovernor(), RiskGovernor(graded_risk=True)):
        ctx = TradeContext(symbol="EURUSD", direction="BUY", current_sl=1.0)
        out = gov.review(ManagementDecision(action=Action.HOLD, reason="hold"), ctx, sa)
        assert out.action == Action.CLOSE


def test_near_catastrophe_escalates_only_in_graded_mode():
    # 2 of 3 conditions near their limit but not the hard all-three CLOSE.
    sa = SituationAssessment(tf_alignment=-0.55, structure_integrity=0.12, profit_state=-0.5)
    ctx = TradeContext(symbol="EURUSD", direction="BUY", current_sl=1.0)
    hold = ManagementDecision(action=Action.HOLD, reason="hold")

    legacy = RiskGovernor().review(hold, ctx, sa)
    assert legacy.action == Action.HOLD  # legacy leaves it alone

    graded = RiskGovernor(graded_risk=True).review(hold, ctx, sa)
    assert graded.action == Action.SET_PROTECTIVE_STOP  # protective de-risk


def test_single_soft_condition_does_not_escalate():
    sa = SituationAssessment(tf_alignment=-0.55, structure_integrity=0.9, profit_state=-0.1)
    ctx = TradeContext(symbol="EURUSD", direction="BUY", current_sl=1.0)
    out = RiskGovernor(graded_risk=True).review(
        ManagementDecision(action=Action.HOLD, reason="hold"), ctx, sa,
    )
    assert out.action == Action.HOLD
