"""
Tests for F3: intentional-hedge flag (narrow bypass).

Proves:
  (a) Flag OFF (default) — hedge conflict still blocks (regression guard).
  (b) Flag ON — hedge scenario returns (True, ...); non-hedge violations
      still return (False, ...) — bypass is hedge-only.
  (c) Flag ON — calculate_exposure(...).is_safe is still False for a hedged
      book — defensive subsystem is not blinded.
"""

from brain.correlation_engine import CorrelationEngine, OpenTrade


# ═══════════════════════════════════════════════════════════════════════════
# Helpers — shared hedge scenarios
# ═══════════════════════════════════════════════════════════════════════════

def _hedge_scenario_shared_base():
    """LONG EURUSD + candidate SHORT EURGBP = opposing on shared base EUR."""
    existing = [OpenTrade(pair="EURUSD", direction="LONG", risk_pct=0.02)]
    return "EURGBP", "SHORT", existing


def _hedge_scenario_synthetic_cross():
    """LONG EURUSD + candidate SHORT GBPUSD = synthetic EUR/GBP cross via USD."""
    existing = [OpenTrade(pair="EURUSD", direction="LONG", risk_pct=0.02)]
    return "GBPUSD", "SHORT", existing


# ═══════════════════════════════════════════════════════════════════════════
# (a) Flag OFF — regression guard: hedge conflict blocks
# ═══════════════════════════════════════════════════════════════════════════

class TestFlagOffBlocksHedge:
    def test_shared_base_blocked_by_default(self):
        engine = CorrelationEngine()
        pair, direction, existing = _hedge_scenario_shared_base()
        ok, reason = engine.can_open_trade(pair, direction, existing)
        assert not ok
        assert "Hedge conflict" in reason

    def test_synthetic_cross_blocked_by_default(self):
        engine = CorrelationEngine()
        pair, direction, existing = _hedge_scenario_synthetic_cross()
        ok, reason = engine.can_open_trade(pair, direction, existing)
        assert not ok
        assert "Hedge conflict" in reason

    def test_explicit_false_still_blocks(self):
        engine = CorrelationEngine(allow_intentional_hedge=False)
        pair, direction, existing = _hedge_scenario_shared_base()
        ok, reason = engine.can_open_trade(pair, direction, existing)
        assert not ok
        assert "Hedge conflict" in reason


# ═══════════════════════════════════════════════════════════════════════════
# (b) Flag ON — hedge bypassed; non-hedge violations still block
# ═══════════════════════════════════════════════════════════════════════════

class TestFlagOnPermitsHedge:
    def test_shared_base_permitted(self):
        engine = CorrelationEngine(allow_intentional_hedge=True)
        pair, direction, existing = _hedge_scenario_shared_base()
        ok, reason = engine.can_open_trade(pair, direction, existing)
        assert ok

    def test_synthetic_cross_permitted(self):
        engine = CorrelationEngine(allow_intentional_hedge=True)
        pair, direction, existing = _hedge_scenario_synthetic_cross()
        ok, reason = engine.can_open_trade(pair, direction, existing)
        assert ok

    def test_cluster_violation_still_blocks(self):
        engine = CorrelationEngine(
            allow_intentional_hedge=True,
            max_cluster_same_direction=2,
        )
        existing = [
            OpenTrade(pair="GER40", direction="LONG", risk_pct=0.01),
            OpenTrade(pair="US500", direction="LONG", risk_pct=0.01),
        ]
        ok, reason = engine.can_open_trade("AUS200", "LONG", existing)
        assert not ok
        assert "equity_risk_on" in reason

    def test_correlated_trades_limit_still_blocks(self):
        engine = CorrelationEngine(
            allow_intentional_hedge=True,
            max_correlated_trades=1,
        )
        existing = [
            OpenTrade(pair="EURUSD", direction="LONG", risk_pct=0.02),
        ]
        ok, reason = engine.can_open_trade("GBPUSD", "LONG", existing)
        assert not ok
        assert "correlated" in reason.lower()

    def test_currency_exposure_limit_still_blocks(self):
        engine = CorrelationEngine(
            allow_intentional_hedge=True,
            max_single_currency_exposure=0.01,
        )
        existing = [
            OpenTrade(pair="EURUSD", direction="LONG", risk_pct=0.02),
        ]
        ok, reason = engine.can_open_trade("EURJPY", "LONG", existing)
        assert not ok
        assert "exposure limit" in reason.lower()


# ═══════════════════════════════════════════════════════════════════════════
# (c) Flag ON — is_safe still False (defensive subsystem not blinded)
# ═══════════════════════════════════════════════════════════════════════════

class TestDefensiveSubsystemNotBlinded:
    def test_is_safe_false_with_hedge_flag_on(self):
        engine = CorrelationEngine(allow_intentional_hedge=True)
        trades = [
            OpenTrade(pair="EURUSD", direction="LONG", risk_pct=0.02),
            OpenTrade(pair="EURGBP", direction="SHORT", risk_pct=0.02),
        ]
        exposure = engine.calculate_exposure(trades)
        assert exposure.hedge_conflicts, "Expected hedge conflicts to be detected"
        assert not exposure.is_safe, (
            "is_safe must remain False when hedge conflicts exist, "
            "even with allow_intentional_hedge=True"
        )

    def test_hedge_conflicts_still_detected(self):
        engine = CorrelationEngine(allow_intentional_hedge=True)
        trades = [
            OpenTrade(pair="EURUSD", direction="LONG", risk_pct=0.02),
            OpenTrade(pair="GBPUSD", direction="SHORT", risk_pct=0.02),
        ]
        exposure = engine.calculate_exposure(trades)
        assert len(exposure.hedge_conflicts) > 0, (
            "_detect_hedge_conflicts must still identify conflicts "
            "regardless of the flag"
        )
