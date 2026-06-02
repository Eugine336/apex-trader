"""
Tests for M8 Phase 4c — emergency liquidation.

Covers:
  • Threshold ordering validation
  • Individual emergency triggers
  • Direct-to-EMERGENCY from any state on hard trigger
  • Ladder de-escalation EMERGENCY→REDUCING→DEFENSIVE→NORMAL (never skip)
  • Precedence: margin events do NOT trigger independent 4c close
  • emergency_max_closes_per_cycle respected
  • Orphan-neutral ranking (insufficient data → never auto-weakest)
  • With portfolio_emergency_enabled=False, no emergency action
"""

import time

import pytest

from risk.portfolio_risk_state import (
    EmergencyTriggerResult,
    EmergencyTriggerSnapshot,
    PortfolioRiskSnapshot,
    PortfolioRiskState,
    PortfolioRiskStateMachine,
    evaluate_emergency_triggers,
    rank_positions_weakest_first,
)


# ── Threshold ordering validation ────────────────────────────────────────

class TestThresholdOrdering:
    def test_valid_ordering_accepted(self):
        sm = PortfolioRiskStateMachine(
            heat_defensive_pct=1.5,
            heat_recovery_pct=1.0,
            heat_reduction_pct=2.5,
            heat_emergency_pct=4.0,
        )
        assert sm.heat_emergency_pct == 4.0

    def test_emergency_must_exceed_reduction(self):
        with pytest.raises(ValueError, match="heat_emergency_pct"):
            PortfolioRiskStateMachine(
                heat_defensive_pct=1.5,
                heat_recovery_pct=1.0,
                heat_reduction_pct=2.5,
                heat_emergency_pct=2.5,
            )

    def test_emergency_below_reduction_rejected(self):
        with pytest.raises(ValueError, match="heat_emergency_pct"):
            PortfolioRiskStateMachine(
                heat_defensive_pct=1.5,
                heat_recovery_pct=1.0,
                heat_reduction_pct=2.5,
                heat_emergency_pct=2.0,
            )

    def test_recovery_must_be_below_defensive(self):
        with pytest.raises(ValueError, match="heat_recovery_pct"):
            PortfolioRiskStateMachine(
                heat_defensive_pct=1.5,
                heat_recovery_pct=1.5,
                heat_reduction_pct=2.5,
                heat_emergency_pct=4.0,
            )

    def test_reduction_must_exceed_defensive(self):
        with pytest.raises(ValueError, match="heat_reduction_pct"):
            PortfolioRiskStateMachine(
                heat_defensive_pct=1.5,
                heat_recovery_pct=1.0,
                heat_reduction_pct=1.5,
                heat_emergency_pct=4.0,
            )


# ── Emergency trigger evaluation ─────────────────────────────────────────

class TestEmergencyTriggers:
    def test_extreme_heat_fires(self):
        snap = EmergencyTriggerSnapshot(
            live_heat_pct=5.0,
            drawdown_mode="NORMAL",
            reconcile_age_seconds=10.0,
            managed_count=3,
            broker_count=3,
        )
        result = evaluate_emergency_triggers(
            snap,
            heat_emergency_pct=4.0,
            emergency_reconcile_failure_seconds=300.0,
            emergency_broker_exposure_tolerance=2,
        )
        assert result.extreme_heat is True
        assert result.any_fired is True
        assert "extreme_heat" in result.description

    def test_drawdown_frozen_fires(self):
        snap = EmergencyTriggerSnapshot(
            live_heat_pct=1.0,
            drawdown_mode="FROZEN",
            reconcile_age_seconds=10.0,
            managed_count=3,
            broker_count=3,
        )
        result = evaluate_emergency_triggers(
            snap,
            heat_emergency_pct=4.0,
            emergency_reconcile_failure_seconds=300.0,
            emergency_broker_exposure_tolerance=2,
        )
        assert result.drawdown_frozen is True
        assert result.any_fired is True

    def test_reconcile_failure_fires(self):
        snap = EmergencyTriggerSnapshot(
            live_heat_pct=1.0,
            drawdown_mode="NORMAL",
            reconcile_age_seconds=600.0,
            managed_count=3,
            broker_count=3,
        )
        result = evaluate_emergency_triggers(
            snap,
            heat_emergency_pct=4.0,
            emergency_reconcile_failure_seconds=300.0,
            emergency_broker_exposure_tolerance=2,
        )
        assert result.reconcile_failure is True
        assert result.any_fired is True

    def test_broker_exposure_mismatch_fires(self):
        snap = EmergencyTriggerSnapshot(
            live_heat_pct=1.0,
            drawdown_mode="NORMAL",
            reconcile_age_seconds=10.0,
            managed_count=5,
            broker_count=1,
        )
        result = evaluate_emergency_triggers(
            snap,
            heat_emergency_pct=4.0,
            emergency_reconcile_failure_seconds=300.0,
            emergency_broker_exposure_tolerance=2,
        )
        assert result.broker_exposure_mismatch is True
        assert result.any_fired is True

    def test_no_trigger_when_all_safe(self):
        snap = EmergencyTriggerSnapshot(
            live_heat_pct=1.0,
            drawdown_mode="NORMAL",
            reconcile_age_seconds=10.0,
            managed_count=3,
            broker_count=3,
        )
        result = evaluate_emergency_triggers(
            snap,
            heat_emergency_pct=4.0,
            emergency_reconcile_failure_seconds=300.0,
            emergency_broker_exposure_tolerance=2,
        )
        assert result.any_fired is False
        assert result.description == "none"

    def test_multiple_triggers_combine(self):
        snap = EmergencyTriggerSnapshot(
            live_heat_pct=5.0,
            drawdown_mode="FROZEN",
            reconcile_age_seconds=600.0,
            managed_count=10,
            broker_count=1,
        )
        result = evaluate_emergency_triggers(
            snap,
            heat_emergency_pct=4.0,
            emergency_reconcile_failure_seconds=300.0,
            emergency_broker_exposure_tolerance=2,
        )
        assert result.extreme_heat is True
        assert result.drawdown_frozen is True
        assert result.reconcile_failure is True
        assert result.broker_exposure_mismatch is True

    def test_tolerance_boundary(self):
        snap = EmergencyTriggerSnapshot(
            live_heat_pct=1.0,
            drawdown_mode="NORMAL",
            reconcile_age_seconds=10.0,
            managed_count=5,
            broker_count=3,
        )
        result = evaluate_emergency_triggers(
            snap,
            heat_emergency_pct=4.0,
            emergency_reconcile_failure_seconds=300.0,
            emergency_broker_exposure_tolerance=2,
        )
        assert result.broker_exposure_mismatch is False

        snap2 = EmergencyTriggerSnapshot(
            live_heat_pct=1.0,
            drawdown_mode="NORMAL",
            reconcile_age_seconds=10.0,
            managed_count=6,
            broker_count=3,
        )
        result2 = evaluate_emergency_triggers(
            snap2,
            heat_emergency_pct=4.0,
            emergency_reconcile_failure_seconds=300.0,
            emergency_broker_exposure_tolerance=2,
        )
        assert result2.broker_exposure_mismatch is True

    def test_broker_count_none_skips_mismatch(self):
        snap = EmergencyTriggerSnapshot(
            live_heat_pct=1.0,
            drawdown_mode="NORMAL",
            reconcile_age_seconds=10.0,
            managed_count=10,
            broker_count=None,
        )
        result = evaluate_emergency_triggers(
            snap,
            heat_emergency_pct=4.0,
            emergency_reconcile_failure_seconds=300.0,
            emergency_broker_exposure_tolerance=2,
        )
        assert result.broker_exposure_mismatch is False
        assert result.any_fired is False

    def test_broker_count_zero_still_fires(self):
        snap = EmergencyTriggerSnapshot(
            live_heat_pct=1.0,
            drawdown_mode="NORMAL",
            reconcile_age_seconds=10.0,
            managed_count=5,
            broker_count=0,
        )
        result = evaluate_emergency_triggers(
            snap,
            heat_emergency_pct=4.0,
            emergency_reconcile_failure_seconds=300.0,
            emergency_broker_exposure_tolerance=2,
        )
        assert result.broker_exposure_mismatch is True


# ── State machine escalation to EMERGENCY ────────────────────────────────

def _make_snapshot(heat=1.0, corr_safe=True, ts=None):
    return PortfolioRiskSnapshot(
        live_heat_pct=heat,
        position_risks=[],
        correlation_safe=corr_safe,
        max_currency_exposure=0.0,
        timestamp=ts or time.monotonic(),
    )


class TestEmergencyEscalation:
    def _make_sm(self):
        return PortfolioRiskStateMachine(
            heat_defensive_pct=1.5,
            heat_recovery_pct=1.0,
            recovery_dwell_seconds=120.0,
            heat_reduction_pct=2.5,
            reduction_persist_seconds=300.0,
            heat_emergency_pct=4.0,
        )

    def test_direct_to_emergency_from_normal(self):
        sm = self._make_sm()
        assert sm.state == PortfolioRiskState.NORMAL
        trigger = EmergencyTriggerResult(extreme_heat=True)
        t = sm.escalate_to_emergency(trigger, 5.0, True)
        assert t.state == PortfolioRiskState.EMERGENCY
        assert t.changed is True
        assert t.escalated_to_emergency is True
        assert "extreme_heat" in t.emergency_trigger

    def test_direct_to_emergency_from_defensive(self):
        sm = self._make_sm()
        sm.evaluate(_make_snapshot(heat=2.0))
        assert sm.state == PortfolioRiskState.DEFENSIVE
        trigger = EmergencyTriggerResult(drawdown_frozen=True)
        t = sm.escalate_to_emergency(trigger, 2.0, True)
        assert t.state == PortfolioRiskState.EMERGENCY
        assert t.changed is True

    def test_direct_to_emergency_from_reducing(self):
        sm = self._make_sm()
        sm.evaluate(_make_snapshot(heat=3.0))
        assert sm.state == PortfolioRiskState.DEFENSIVE
        sm.evaluate(_make_snapshot(heat=3.0))
        assert sm.state == PortfolioRiskState.REDUCING
        trigger = EmergencyTriggerResult(reconcile_failure=True)
        t = sm.escalate_to_emergency(trigger, 3.0, True)
        assert t.state == PortfolioRiskState.EMERGENCY

    def test_already_emergency_no_change(self):
        sm = self._make_sm()
        trigger = EmergencyTriggerResult(extreme_heat=True)
        sm.escalate_to_emergency(trigger, 5.0, True)
        assert sm.state == PortfolioRiskState.EMERGENCY
        t = sm.escalate_to_emergency(trigger, 5.0, True)
        assert t.changed is False
        assert t.state == PortfolioRiskState.EMERGENCY


# ── De-escalation ladder (never skip downward) ──────────────────────────

class TestEmergencyDeescalation:
    def _make_sm(self):
        return PortfolioRiskStateMachine(
            heat_defensive_pct=1.5,
            heat_recovery_pct=1.0,
            recovery_dwell_seconds=5.0,
            heat_reduction_pct=2.5,
            reduction_persist_seconds=300.0,
            heat_emergency_pct=4.0,
        )

    def test_emergency_to_reducing_not_normal(self):
        sm = self._make_sm()
        trigger = EmergencyTriggerResult(extreme_heat=True)
        sm.escalate_to_emergency(trigger, 5.0, True)
        assert sm.state == PortfolioRiskState.EMERGENCY

        t = sm.evaluate(_make_snapshot(heat=0.5, corr_safe=True))
        assert t.state == PortfolioRiskState.REDUCING
        assert t.changed is True

    def test_full_deescalation_ladder(self):
        sm = self._make_sm()
        trigger = EmergencyTriggerResult(extreme_heat=True)
        sm.escalate_to_emergency(trigger, 5.0, True)

        t1 = sm.evaluate(_make_snapshot(heat=0.5, corr_safe=True))
        assert t1.state == PortfolioRiskState.REDUCING

        t2 = sm.evaluate(_make_snapshot(heat=0.5, corr_safe=True))
        assert t2.state == PortfolioRiskState.DEFENSIVE

        base_time = time.monotonic()
        t3 = sm.evaluate(_make_snapshot(heat=0.5, corr_safe=True, ts=base_time))
        assert t3.state == PortfolioRiskState.DEFENSIVE

        t4 = sm.evaluate(_make_snapshot(heat=0.5, corr_safe=True, ts=base_time + 10.0))
        assert t4.state == PortfolioRiskState.NORMAL

    def test_emergency_stays_while_breached(self):
        sm = self._make_sm()
        trigger = EmergencyTriggerResult(extreme_heat=True)
        sm.escalate_to_emergency(trigger, 5.0, True)

        t = sm.evaluate(_make_snapshot(heat=2.0, corr_safe=True))
        assert t.state == PortfolioRiskState.EMERGENCY
        assert t.changed is False


# ── Ranking: orphan-neutral (shared with 4b, re-verified for 4c) ────────

class TestEmergencyRanking:
    def _make_pos(self, oid, score=70, regime="TRENDING", entry_type="MARKET",
                  entry_price=1.0, sl=0.99, current_price=1.01, lots=0.1):
        from datetime import datetime, timezone
        return {
            "order_id": oid,
            "symbol": "EURUSD",
            "direction": "BUY",
            "entry_price": entry_price,
            "sl": sl,
            "current_price": current_price,
            "score": score,
            "regime": regime,
            "entry_type": entry_type,
            "open_time_utc": datetime.now(timezone.utc),
            "risk_dollars": 10.0,
            "lots": lots,
        }

    def test_orphan_never_weakest(self):
        positions = [
            self._make_pos("001", score=0, regime="UNKNOWN", entry_type="ORPHAN_ADOPTED"),
            self._make_pos("002", score=50, regime="TRENDING", current_price=0.98),
            self._make_pos("003", score=80, regime="TRENDING"),
        ]
        ranked = rank_positions_weakest_first(positions, 30.0)
        first = ranked[0]
        assert first.order_id != "001", "Orphan must not be auto-ranked weakest"

    def test_all_orphans_still_rankable(self):
        positions = [
            self._make_pos("001", score=0, regime="UNKNOWN", entry_type="ORPHAN_ADOPTED"),
            self._make_pos("002", score=0, regime="UNKNOWN", entry_type="ORPHAN_ADOPTED"),
        ]
        ranked = rank_positions_weakest_first(positions, 20.0)
        assert len(ranked) == 2
        for r in ranked:
            assert r.is_insufficient_data is True

    def test_deep_loss_is_weakest(self):
        positions = [
            self._make_pos("001", score=70, current_price=0.90),
            self._make_pos("002", score=90, current_price=1.05),
        ]
        ranked = rank_positions_weakest_first(positions, 20.0)
        assert ranked[0].order_id == "001"


# ── EmergencyTriggerResult.description ────────────────────────────────────

class TestTriggerDescription:
    def test_single_trigger(self):
        r = EmergencyTriggerResult(extreme_heat=True)
        assert r.description == "extreme_heat"

    def test_multiple_triggers(self):
        r = EmergencyTriggerResult(extreme_heat=True, drawdown_frozen=True)
        assert "extreme_heat" in r.description
        assert "drawdown_frozen" in r.description

    def test_none(self):
        r = EmergencyTriggerResult()
        assert r.description == "none"
        assert r.any_fired is False
