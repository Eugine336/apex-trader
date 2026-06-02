"""
Tests for the Portfolio Risk State Machine (M8 Phase 4a + 4b).

Covers:
  1. compute_position_risk_dollars — MT5, Deriv, breakeven, fallback
  2. compute_live_heat_pct — normal, zero-equity guard
  3. PortfolioRiskStateMachine — transitions, hysteresis, dwell, REDUCING
  4. is_position_data_insufficient — orphan-neutral helper
  5. is_eligible_for_defensive_breakeven — eligibility rules
  6. rank_positions_weakest_first — composite ranking, orphan-neutral
"""

import time
from datetime import datetime, timezone

import pytest

from risk.portfolio_risk_state import (
    PortfolioRiskStateMachine,
    PortfolioRiskState,
    PortfolioRiskSnapshot,
    PositionRisk,
    PositionWeakness,
    StateTransition,
    compute_position_risk_dollars,
    compute_live_heat_pct,
    is_eligible_for_defensive_breakeven,
    is_position_data_insufficient,
    rank_positions_weakest_first,
)


# ── compute_position_risk_dollars ───────────────────────────────────────────

class TestComputePositionRiskDollars:

    def test_mt5_long_normal(self):
        risk, fallback = compute_position_risk_dollars(
            direction="BUY",
            entry_price=1.1000,
            sl=1.0980,
            lots=0.10,
            pip_size=0.0001,
            pip_value_per_lot=10.0,
            at_breakeven=False,
        )
        # 20 pips * 10.0 pip_val * 0.10 lots = 20.0
        assert risk == 20.0
        assert fallback is False

    def test_mt5_short_normal(self):
        risk, fallback = compute_position_risk_dollars(
            direction="SELL",
            entry_price=1.1000,
            sl=1.1030,
            lots=0.05,
            pip_size=0.0001,
            pip_value_per_lot=10.0,
            at_breakeven=False,
        )
        # 30 pips * 10.0 * 0.05 = 15.0
        assert risk == 15.0
        assert fallback is False

    def test_at_breakeven_returns_zero(self):
        risk, fallback = compute_position_risk_dollars(
            direction="BUY",
            entry_price=1.1000,
            sl=1.0900,
            lots=1.0,
            pip_size=0.0001,
            pip_value_per_lot=10.0,
            at_breakeven=True,
        )
        assert risk == 0.0
        assert fallback is False

    def test_sl_beyond_entry_returns_zero(self):
        """Long with SL above entry → no capital at risk."""
        risk, fallback = compute_position_risk_dollars(
            direction="BUY",
            entry_price=1.1000,
            sl=1.1050,
            lots=0.10,
            pip_size=0.0001,
            pip_value_per_lot=10.0,
            at_breakeven=False,
        )
        assert risk == 0.0

    def test_invalid_inputs_returns_fallback(self):
        risk, fallback = compute_position_risk_dollars(
            direction="BUY",
            entry_price=0.0,
            sl=1.0900,
            lots=0.10,
            pip_size=0.0001,
            pip_value_per_lot=10.0,
            at_breakeven=False,
        )
        assert fallback is True

    def test_deriv_stake_based(self):
        risk, fallback = compute_position_risk_dollars(
            direction="BUY",
            entry_price=100.0,
            sl=95.0,
            lots=0.0,
            pip_size=0.01,
            pip_value_per_lot=1.0,
            at_breakeven=False,
            stake_usd=50.0,
            multiplier=100,
            is_deriv_stake=True,
        )
        # risk = 50 * 100 * (5/100) = 250, capped at stake=50
        assert risk == 50.0
        assert fallback is False

    def test_deriv_small_sl_distance(self):
        risk, fallback = compute_position_risk_dollars(
            direction="BUY",
            entry_price=100.0,
            sl=99.5,
            lots=0.0,
            pip_size=0.01,
            pip_value_per_lot=1.0,
            at_breakeven=False,
            stake_usd=50.0,
            multiplier=100,
            is_deriv_stake=True,
        )
        # risk = 50 * 100 * (0.5/100) = 25.0
        assert risk == 25.0
        assert fallback is False


# ── compute_live_heat_pct ───────────────────────────────────────────────────

class TestComputeLiveHeatPct:

    def test_normal_calculation(self):
        risks = [
            PositionRisk("a", "EURUSD", "BUY", 50.0, False, False),
            PositionRisk("b", "GBPUSD", "SELL", 30.0, True, False),
        ]
        heat = compute_live_heat_pct(risks, 10000.0)
        assert abs(heat - 0.8) < 0.01  # (50+30)/10000*100 = 0.8%

    def test_zero_equity_returns_max(self):
        risks = [PositionRisk("a", "EURUSD", "BUY", 50.0, False, False)]
        assert compute_live_heat_pct(risks, 0.0) == 100.0

    def test_empty_positions(self):
        assert compute_live_heat_pct([], 10000.0) == 0.0


# ── is_position_data_insufficient (orphan-neutral) ──────────────────────────

class TestOrphanNeutral:

    def test_orphan_adopted_is_insufficient(self):
        assert is_position_data_insufficient(0, "UNKNOWN", "ORPHAN_ADOPTED") is True

    def test_normal_trade_is_sufficient(self):
        assert is_position_data_insufficient(85, "TRENDING", "ENTRY") is False

    def test_score_zero_unknown_regime(self):
        assert is_position_data_insufficient(0, "UNKNOWN", "") is True

    def test_score_zero_but_known_regime(self):
        assert is_position_data_insufficient(0, "TRENDING", "ENTRY") is False


# ── is_eligible_for_defensive_breakeven ─────────────────────────────────────

class TestBreakevenEligibility:

    def test_already_at_breakeven_not_eligible(self):
        assert is_eligible_for_defensive_breakeven(
            direction="BUY", entry_price=1.1, sl=1.09,
            current_price=1.12, tp1_hit=True, partial_closed=False,
            at_breakeven=True,
        ) is False

    def test_tp1_hit_is_eligible(self):
        assert is_eligible_for_defensive_breakeven(
            direction="BUY", entry_price=1.1, sl=1.09,
            current_price=1.11, tp1_hit=True, partial_closed=False,
            at_breakeven=False,
        ) is True

    def test_r_multiple_excursion_eligible(self):
        assert is_eligible_for_defensive_breakeven(
            direction="BUY", entry_price=1.1000, sl=1.0900,
            current_price=1.1100, tp1_hit=False, partial_closed=False,
            at_breakeven=False, be_eligible_r_multiple=1.0,
        ) is True

    def test_insufficient_excursion_not_eligible(self):
        assert is_eligible_for_defensive_breakeven(
            direction="BUY", entry_price=1.1000, sl=1.0900,
            current_price=1.1050, tp1_hit=False, partial_closed=False,
            at_breakeven=False, be_eligible_r_multiple=1.0,
        ) is False

    def test_short_r_multiple(self):
        assert is_eligible_for_defensive_breakeven(
            direction="SELL", entry_price=1.1000, sl=1.1100,
            current_price=1.0900, tp1_hit=False, partial_closed=False,
            at_breakeven=False, be_eligible_r_multiple=1.0,
        ) is True


# ── PortfolioRiskStateMachine ───────────────────────────────────────────────

class TestPortfolioRiskStateMachine:

    def _make_snapshot(self, heat=0.5, corr_safe=True, ts=None):
        return PortfolioRiskSnapshot(
            live_heat_pct=heat,
            position_risks=[],
            correlation_safe=corr_safe,
            max_currency_exposure=0.02,
            timestamp=ts or time.monotonic(),
        )

    def test_init_is_normal(self):
        sm = PortfolioRiskStateMachine(heat_defensive_pct=1.5, heat_recovery_pct=1.0)
        assert sm.state == PortfolioRiskState.NORMAL

    def test_invalid_thresholds_raises(self):
        with pytest.raises(ValueError):
            PortfolioRiskStateMachine(heat_defensive_pct=1.0, heat_recovery_pct=1.5)

    def test_heat_breach_transitions_to_defensive(self):
        sm = PortfolioRiskStateMachine(heat_defensive_pct=1.5, heat_recovery_pct=1.0)
        result = sm.evaluate(self._make_snapshot(heat=2.0))
        assert result.state == PortfolioRiskState.DEFENSIVE
        assert result.changed is True

    def test_correlation_breach_transitions_to_defensive(self):
        sm = PortfolioRiskStateMachine(heat_defensive_pct=1.5, heat_recovery_pct=1.0)
        result = sm.evaluate(self._make_snapshot(heat=0.5, corr_safe=False))
        assert result.state == PortfolioRiskState.DEFENSIVE
        assert result.changed is True

    def test_stays_normal_when_safe(self):
        sm = PortfolioRiskStateMachine(heat_defensive_pct=1.5, heat_recovery_pct=1.0)
        result = sm.evaluate(self._make_snapshot(heat=0.5))
        assert result.state == PortfolioRiskState.NORMAL
        assert result.changed is False

    def test_stays_defensive_when_still_breached(self):
        sm = PortfolioRiskStateMachine(heat_defensive_pct=1.5, heat_recovery_pct=1.0)
        sm.evaluate(self._make_snapshot(heat=2.0))
        result = sm.evaluate(self._make_snapshot(heat=1.6))
        assert result.state == PortfolioRiskState.DEFENSIVE
        assert result.changed is False

    def test_hysteresis_no_immediate_recovery(self):
        """Even if heat drops below recovery, must dwell first."""
        sm = PortfolioRiskStateMachine(
            heat_defensive_pct=1.5,
            heat_recovery_pct=1.0,
            recovery_dwell_seconds=60.0,
        )
        t0 = 1000.0
        sm.evaluate(self._make_snapshot(heat=2.0, ts=t0))
        result = sm.evaluate(self._make_snapshot(heat=0.5, ts=t0 + 1))
        assert result.state == PortfolioRiskState.DEFENSIVE
        assert result.changed is False

    def test_recovery_after_dwell(self):
        sm = PortfolioRiskStateMachine(
            heat_defensive_pct=1.5,
            heat_recovery_pct=1.0,
            recovery_dwell_seconds=10.0,
        )
        t0 = 1000.0
        sm.evaluate(self._make_snapshot(heat=2.0, ts=t0))
        sm.evaluate(self._make_snapshot(heat=0.5, ts=t0 + 1))
        result = sm.evaluate(self._make_snapshot(heat=0.5, ts=t0 + 12))
        assert result.state == PortfolioRiskState.NORMAL
        assert result.changed is True

    def test_dwell_resets_on_re_breach(self):
        sm = PortfolioRiskStateMachine(
            heat_defensive_pct=1.5,
            heat_recovery_pct=1.0,
            recovery_dwell_seconds=10.0,
        )
        t0 = 1000.0
        sm.evaluate(self._make_snapshot(heat=2.0, ts=t0))
        sm.evaluate(self._make_snapshot(heat=0.5, ts=t0 + 1))
        sm.evaluate(self._make_snapshot(heat=1.2, ts=t0 + 5))
        sm.evaluate(self._make_snapshot(heat=0.5, ts=t0 + 6))
        result = sm.evaluate(self._make_snapshot(heat=0.5, ts=t0 + 12))
        assert result.state == PortfolioRiskState.DEFENSIVE

    def test_recovery_requires_both_heat_and_corr(self):
        sm = PortfolioRiskStateMachine(
            heat_defensive_pct=1.5,
            heat_recovery_pct=1.0,
            recovery_dwell_seconds=5.0,
        )
        t0 = 1000.0
        sm.evaluate(self._make_snapshot(heat=2.0, ts=t0))
        sm.evaluate(self._make_snapshot(heat=0.5, corr_safe=False, ts=t0 + 1))
        result = sm.evaluate(self._make_snapshot(heat=0.5, corr_safe=False, ts=t0 + 10))
        assert result.state == PortfolioRiskState.DEFENSIVE

    def test_reset(self):
        sm = PortfolioRiskStateMachine(heat_defensive_pct=1.5, heat_recovery_pct=1.0)
        sm.evaluate(self._make_snapshot(heat=2.0))
        assert sm.state == PortfolioRiskState.DEFENSIVE
        sm.reset()
        assert sm.state == PortfolioRiskState.NORMAL


# ── Phase 4b: REDUCING state tests ─────────────────────────────────────────

class TestReducingState:

    def _make_snapshot(self, heat=0.5, corr_safe=True, ts=None):
        return PortfolioRiskSnapshot(
            live_heat_pct=heat,
            position_risks=[],
            correlation_safe=corr_safe,
            max_currency_exposure=0.02,
            timestamp=ts or time.monotonic(),
        )

    def test_invalid_reduction_threshold_raises(self):
        with pytest.raises(ValueError, match="heat_reduction_pct"):
            PortfolioRiskStateMachine(
                heat_defensive_pct=1.5,
                heat_recovery_pct=1.0,
                heat_reduction_pct=1.0,
            )

    def test_escalate_on_secondary_heat(self):
        sm = PortfolioRiskStateMachine(
            heat_defensive_pct=1.5, heat_recovery_pct=1.0,
            heat_reduction_pct=2.5,
        )
        t0 = 1000.0
        sm.evaluate(self._make_snapshot(heat=2.0, ts=t0))
        assert sm.state == PortfolioRiskState.DEFENSIVE
        result = sm.evaluate(self._make_snapshot(heat=3.0, ts=t0 + 1))
        assert result.state == PortfolioRiskState.REDUCING
        assert result.changed is True
        assert result.escalated_to_reducing is True

    def test_escalate_on_persistence(self):
        sm = PortfolioRiskStateMachine(
            heat_defensive_pct=1.5, heat_recovery_pct=1.0,
            heat_reduction_pct=10.0,
            reduction_persist_seconds=60.0,
            heat_emergency_pct=15.0,
        )
        t0 = 1000.0
        sm.evaluate(self._make_snapshot(heat=2.0, ts=t0))
        assert sm.state == PortfolioRiskState.DEFENSIVE
        result = sm.evaluate(self._make_snapshot(heat=1.8, ts=t0 + 30))
        assert result.state == PortfolioRiskState.DEFENSIVE
        result = sm.evaluate(self._make_snapshot(heat=1.8, ts=t0 + 61))
        assert result.state == PortfolioRiskState.REDUCING
        assert result.changed is True

    def test_no_escalate_if_breach_cleared(self):
        sm = PortfolioRiskStateMachine(
            heat_defensive_pct=1.5, heat_recovery_pct=1.0,
            heat_reduction_pct=10.0,
            reduction_persist_seconds=60.0,
            heat_emergency_pct=15.0,
        )
        t0 = 1000.0
        sm.evaluate(self._make_snapshot(heat=2.0, ts=t0))
        sm.evaluate(self._make_snapshot(heat=0.5, ts=t0 + 1))
        result = sm.evaluate(self._make_snapshot(heat=0.5, ts=t0 + 70))
        assert result.state != PortfolioRiskState.REDUCING

    def test_deescalate_reducing_to_defensive(self):
        sm = PortfolioRiskStateMachine(
            heat_defensive_pct=1.5, heat_recovery_pct=1.0,
            heat_reduction_pct=2.5,
        )
        t0 = 1000.0
        sm.evaluate(self._make_snapshot(heat=2.0, ts=t0))
        sm.evaluate(self._make_snapshot(heat=3.0, ts=t0 + 1))
        assert sm.state == PortfolioRiskState.REDUCING
        result = sm.evaluate(self._make_snapshot(heat=1.2, ts=t0 + 2))
        assert result.state == PortfolioRiskState.DEFENSIVE
        assert result.changed is True

    def test_never_reducing_to_normal_directly(self):
        sm = PortfolioRiskStateMachine(
            heat_defensive_pct=1.5, heat_recovery_pct=1.0,
            heat_reduction_pct=2.5, recovery_dwell_seconds=5.0,
        )
        t0 = 1000.0
        sm.evaluate(self._make_snapshot(heat=2.0, ts=t0))
        sm.evaluate(self._make_snapshot(heat=3.0, ts=t0 + 1))
        assert sm.state == PortfolioRiskState.REDUCING
        result = sm.evaluate(self._make_snapshot(heat=0.5, ts=t0 + 2))
        assert result.state == PortfolioRiskState.DEFENSIVE
        result = sm.evaluate(self._make_snapshot(heat=0.5, ts=t0 + 3))
        assert result.state == PortfolioRiskState.DEFENSIVE
        result = sm.evaluate(self._make_snapshot(heat=0.5, ts=t0 + 10))
        assert result.state == PortfolioRiskState.NORMAL

    def test_stays_reducing_while_breached(self):
        sm = PortfolioRiskStateMachine(
            heat_defensive_pct=1.5, heat_recovery_pct=1.0,
            heat_reduction_pct=2.5,
        )
        t0 = 1000.0
        sm.evaluate(self._make_snapshot(heat=3.0, ts=t0))
        sm.evaluate(self._make_snapshot(heat=3.0, ts=t0 + 1))
        result = sm.evaluate(self._make_snapshot(heat=2.0, ts=t0 + 2))
        assert result.state == PortfolioRiskState.REDUCING
        assert result.changed is False


# ── Phase 4b: rank_positions_weakest_first ──────────────────────────────────

class TestRankPositions:

    def _make_pos(
        self, oid="a", score=80, r_mult=0.0, minutes_ago=30,
        risk_dollars=50.0, regime="TRENDING", entry_type="ENTRY",
        direction="BUY", entry_price=1.10, sl=1.09, lots=0.1,
    ):
        now = datetime.now(timezone.utc)
        from datetime import timedelta
        open_time = now - timedelta(minutes=minutes_ago)
        current_price = entry_price + r_mult * abs(entry_price - sl) * (
            1 if direction.upper() in ("BUY", "LONG") else -1
        )
        return {
            "order_id": oid,
            "symbol": "EURUSD",
            "direction": direction,
            "entry_price": entry_price,
            "sl": sl,
            "current_price": current_price,
            "score": score,
            "regime": regime,
            "entry_type": entry_type,
            "open_time_utc": open_time,
            "risk_dollars": risk_dollars,
            "lots": lots,
        }

    def test_orphan_is_neutral_not_weakest(self):
        positions = [
            self._make_pos(oid="weak", score=40, r_mult=-0.8),
            self._make_pos(oid="orphan", score=0, regime="UNKNOWN", entry_type="ORPHAN_ADOPTED"),
            self._make_pos(oid="strong", score=95, r_mult=2.0),
        ]
        ranked = rank_positions_weakest_first(positions, 150.0)
        oids = [r.order_id for r in ranked]
        assert oids[0] == "weak"
        assert oids[-1] == "strong"
        orphan = next(r for r in ranked if r.order_id == "orphan")
        assert orphan.is_insufficient_data is True

    def test_deep_loss_ranks_weakest(self):
        positions = [
            self._make_pos(oid="loser", score=70, r_mult=-1.0),
            self._make_pos(oid="winner", score=90, r_mult=2.0),
        ]
        ranked = rank_positions_weakest_first(positions, 100.0)
        assert ranked[0].order_id == "loser"
        assert ranked[-1].order_id == "winner"

    def test_stagnant_position_ranks_weaker(self):
        positions = [
            self._make_pos(oid="stale", score=70, r_mult=0.1, minutes_ago=200),
            self._make_pos(oid="fresh", score=70, r_mult=0.1, minutes_ago=10),
        ]
        ranked = rank_positions_weakest_first(positions, 100.0)
        assert ranked[0].order_id == "stale"

    def test_deterministic_tiebreak(self):
        positions = [
            self._make_pos(oid="b", score=70, r_mult=0.5),
            self._make_pos(oid="a", score=70, r_mult=0.5),
        ]
        ranked1 = rank_positions_weakest_first(positions, 100.0)
        ranked2 = rank_positions_weakest_first(list(reversed(positions)), 100.0)
        assert [r.order_id for r in ranked1] == [r.order_id for r in ranked2]

    def test_empty_positions(self):
        assert rank_positions_weakest_first([], 0.0) == []

    def test_all_orphans(self):
        positions = [
            self._make_pos(oid="o1", score=0, regime="UNKNOWN", entry_type="ORPHAN_ADOPTED"),
            self._make_pos(oid="o2", score=0, regime="UNKNOWN", entry_type="ORPHAN_ADOPTED"),
        ]
        ranked = rank_positions_weakest_first(positions, 100.0)
        assert len(ranked) == 2
        assert all(r.is_insufficient_data for r in ranked)
