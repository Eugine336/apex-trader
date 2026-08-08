"""
Regression tests for the Tier 1–4 hardening pass:

  Tier 2 #8/#9 — per-account unrealized drawdown halt + flatten threshold
  Tier 2 #10  — decision-engine active loss-response (act on a losing,
                deteriorating trade; still hold a healthy pullback)
  Tier 2 #16  — correlation engine no longer fails open for unmapped symbols
  Tier 2 #17  — AccountRiskManager.restore_state survives a corrupt payload

These exercise pure logic and have no broker/IO dependencies.
"""

from brain.correlation_engine import CorrelationEngine, OpenTrade
from decision.actions import Action
from decision.context import TradeContext
from decision.engine import DecisionEngine
from decision.situation import SituationAssessment
from risk.account_risk import AccountRiskManager


# ──────────────────────────────────────────────────────────────────────
# Tier 2 #8 / #9 — unrealized drawdown halt + flatten threshold
# ──────────────────────────────────────────────────────────────────────

def _mk_account() -> AccountRiskManager:
    am = AccountRiskManager(
        daily_loss_cap_pct=3.0,
        daily_loss_recovery_pct=1.5,
        heat_block_pct=2.0,
        daily_loss_flatten_pct=5.0,
    )
    am.update_balance("acct", 1000.0)
    return am


class TestUnrealizedDrawdownHalt:
    def test_open_loss_trips_entry_halt(self):
        am = _mk_account()
        assert not am.daily_loss_halted("acct")
        # -4% purely UNREALIZED (no closed trades) must trip the -3% halt.
        am.update_unrealized("acct", -40.0)
        assert am.daily_loss_halted("acct")
        assert not am.flatten_breached("acct")  # 4% < 5% flatten cap

    def test_realized_plus_unrealized_combine(self):
        am = _mk_account()
        am.register_realized("acct", -20.0)   # -2%
        am.update_unrealized("acct", -20.0)   # -2%  → combined -4%
        assert round(am.combined_pnl_pct("acct"), 2) == -4.0
        assert am.daily_loss_halted("acct")

    def test_flatten_threshold(self):
        am = _mk_account()
        am.update_unrealized("acct", -60.0)   # -6% combined
        assert am.flatten_breached("acct")

    def test_reset_clears_unrealized(self):
        am = _mk_account()
        am.update_unrealized("acct", -60.0)
        assert am.flatten_breached("acct")
        am.reset_daily()
        assert am.combined_pnl_pct("acct") == 0.0
        assert not am.daily_loss_halted("acct")
        assert not am.flatten_breached("acct")

    def test_unknown_balance_never_halts_or_flattens(self):
        am = AccountRiskManager(daily_loss_flatten_pct=5.0)
        am.update_unrealized("no_balance", -999.0)
        assert not am.daily_loss_halted("no_balance")
        assert not am.flatten_breached("no_balance")


# ──────────────────────────────────────────────────────────────────────
# Tier 2 #17 — corrupt persisted state must not raise (and start fresh)
# ──────────────────────────────────────────────────────────────────────

class TestRestoreStateResilience:
    def test_corrupt_payload_does_not_raise(self):
        am = _mk_account()
        am.restore_state({"daily_pnl": {"acct": "not-a-number"}, "halted": {}})
        # Corrupt payload ignored → fresh state, no halt.
        assert not am.daily_loss_halted("acct")

    def test_valid_payload_round_trip(self):
        am = _mk_account()
        am.register_realized("acct", -35.0)  # halts (-3.5% < -3%)
        state = am.to_state()
        am2 = _mk_account()
        am2.restore_state(state)
        assert am2.daily_pnl("acct") == -35.0


# ──────────────────────────────────────────────────────────────────────
# Tier 2 #16 — exposure no longer fails open for unmapped symbols
# ──────────────────────────────────────────────────────────────────────

class TestCorrelationFailClosed:
    def test_unmapped_symbol_caps_same_instrument_stacking(self):
        eng = CorrelationEngine(max_correlated_trades=3)
        sym = "ZZZUSD"  # in neither CURRENCY_PAIRS nor ASSET_CLUSTER
        existing = [OpenTrade(pair=sym, direction="LONG", risk_pct=0.02) for _ in range(3)]
        ok, _ = eng.can_open_trade(sym, "LONG", existing, risk_pct=0.02)
        assert ok is False  # would previously fail open (always allowed)

    def test_unmapped_symbol_allows_below_cap(self):
        eng = CorrelationEngine(max_correlated_trades=3)
        sym = "ZZZUSD"
        existing = [OpenTrade(pair=sym, direction="LONG", risk_pct=0.02)]
        ok, _ = eng.can_open_trade(sym, "LONG", existing, risk_pct=0.02)
        assert ok is True


# ──────────────────────────────────────────────────────────────────────
# Tier 2 #10 — decision-engine active loss-response
# ──────────────────────────────────────────────────────────────────────

def _mk_ctx() -> TradeContext:
    return TradeContext(
        symbol="BTCUSD", order_id="t1", direction="BUY",
        entry_price=64510.0, current_price=64242.0, current_sl=64000.0,
        original_risk_pips=100.0,
    )


class TestActiveLossResponse:
    def test_deep_loss_with_broken_read_closes(self):
        eng = DecisionEngine()
        sa = SituationAssessment(
            tf_alignment=0.1,           # trend not yet fully reversed
            momentum=-0.5,              # momentum against
            structure_integrity=0.15,   # structure broken
            profit_state=-1.5,          # deep loss
        )
        decision = eng.decide_management(_mk_ctx(), sa)
        assert decision.action == Action.CLOSE
        assert "active loss-response" in decision.reason

    def test_healthy_pullback_still_holds(self):
        eng = DecisionEngine()
        sa = SituationAssessment(
            tf_alignment=0.5,           # trend still supportive
            momentum=0.2,               # momentum still positive
            structure_integrity=0.7,    # structure intact
            profit_state=-1.2,          # losing, but a normal pullback
        )
        decision = eng.decide_management(_mk_ctx(), sa)
        assert decision.action == Action.HOLD
