"""Tests for entry.entry_gate — EntryGate."""

import math
from datetime import datetime, timedelta, timezone

import pytest

from entry.entry_gate import EntryGate
from entry.models import EntryConfig, EntryZone, ZoneType


def _make_zone(expires_in_seconds=600) -> EntryZone:
    now = datetime.now(timezone.utc)
    return EntryZone(
        symbol="EURUSD", direction="LONG", zone_type=ZoneType.FVG_MIDPOINT,
        top=1.0850, bottom=1.0840, midpoint=1.0845,
        invalidation_level=1.0835, conviction=85,
        created_at=now, expires_at=now + timedelta(seconds=expires_in_seconds),
        timeframe="M5",
    )


def _defaults(**overrides):
    """Valid default kwargs for validate_all."""
    base = dict(
        symbol="EURUSD", direction="LONG",
        entry_price=1.0845, stop_loss=1.0835,
        tp1=1.0860, tp2=1.0875,
        score=85, current_spread_pips=1.0,
        zone=_make_zone(),
        is_instrument_known=True, is_market_open=True,
        is_session_active=True, is_news_clear=True,
        is_drawdown_ok=True,
    )
    base.update(overrides)
    return base


class TestEntryGateAllPass:
    def test_all_gates_pass(self):
        gate = EntryGate()
        passed, results = gate.validate_all(**_defaults())
        assert passed is True
        assert all(r.passed for r in results)
        assert len(results) == 10

    def test_results_include_all_gate_names(self):
        gate = EntryGate()
        _, results = gate.validate_all(**_defaults())
        names = {r.gate_name for r in results}
        expected = {
            "instrument_known", "price_finite", "market_open",
            "session_active", "spread_ok", "news_clear",
            "drawdown_ok", "score_minimum", "risk_reward_ok",
            "zone_valid",
        }
        assert names == expected


class TestEntryGateInstrument:
    def test_unknown_instrument_fails(self):
        gate = EntryGate()
        passed, results = gate.validate_all(**_defaults(is_instrument_known=False))
        assert passed is False
        failed = [r for r in results if r.gate_name == "instrument_known"]
        assert not failed[0].passed


class TestEntryGatePriceFinite:
    def test_nan_entry_fails(self):
        gate = EntryGate()
        passed, _ = gate.validate_all(**_defaults(entry_price=float("nan")))
        assert passed is False

    def test_inf_sl_fails(self):
        gate = EntryGate()
        passed, _ = gate.validate_all(**_defaults(stop_loss=float("inf")))
        assert passed is False


class TestEntryGateMarket:
    def test_market_closed_fails(self):
        gate = EntryGate()
        passed, results = gate.validate_all(**_defaults(is_market_open=False))
        assert passed is False

    def test_session_inactive_fails(self):
        gate = EntryGate()
        passed, _ = gate.validate_all(**_defaults(is_session_active=False))
        assert passed is False


class TestEntryGateSpread:
    def test_wide_spread_fails(self):
        gate = EntryGate()
        passed, results = gate.validate_all(**_defaults(current_spread_pips=50.0))
        assert passed is False
        spread_gate = [r for r in results if r.gate_name == "spread_ok"][0]
        assert not spread_gate.passed

    def test_tight_spread_passes(self):
        gate = EntryGate()
        passed, _ = gate.validate_all(**_defaults(current_spread_pips=0.5))
        assert passed is True


class TestEntryGateNews:
    def test_news_block_fails(self):
        gate = EntryGate()
        passed, _ = gate.validate_all(**_defaults(is_news_clear=False))
        assert passed is False


class TestEntryGateDrawdown:
    def test_drawdown_block_fails(self):
        gate = EntryGate()
        passed, _ = gate.validate_all(**_defaults(is_drawdown_ok=False))
        assert passed is False


class TestEntryGateScore:
    def test_low_score_fails(self):
        cfg = EntryConfig(min_entry_score=80)
        gate = EntryGate(config=cfg)
        passed, _ = gate.validate_all(**_defaults(score=50))
        assert passed is False

    def test_score_at_threshold_passes(self):
        cfg = EntryConfig(min_entry_score=80)
        gate = EntryGate(config=cfg)
        passed, _ = gate.validate_all(**_defaults(score=80))
        assert passed is True


class TestEntryGateRiskReward:
    def test_rr_below_1_fails(self):
        gate = EntryGate()
        passed, _ = gate.validate_all(**_defaults(
            entry_price=1.0845, stop_loss=1.0835,
            tp1=1.0848,  # less than 1R
            tp2=1.0875,
        ))
        assert passed is False

    def test_zero_risk_distance_fails(self):
        gate = EntryGate()
        passed, _ = gate.validate_all(**_defaults(
            entry_price=1.0845, stop_loss=1.0845,
        ))
        assert passed is False


class TestEntryGateZone:
    def test_no_zone_fails(self):
        gate = EntryGate()
        passed, _ = gate.validate_all(**_defaults(zone=None))
        assert passed is False

    def test_expired_zone_fails(self):
        gate = EntryGate()
        zone = _make_zone(expires_in_seconds=-10)
        passed, _ = gate.validate_all(**_defaults(zone=zone))
        assert passed is False


class TestEntryGateMultipleFailures:
    def test_all_failures_reported(self):
        gate = EntryGate()
        passed, results = gate.validate_all(**_defaults(
            is_market_open=False,
            is_news_clear=False,
            score=10,
        ))
        assert passed is False
        failed = [r for r in results if not r.passed]
        assert len(failed) >= 3
