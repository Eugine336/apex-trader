"""Tests for the CampaignRegistry (evolving market campaigns, not isolated trades).

Covers the lifecycle the mandate requires: a campaign is born when evidence
supports a thesis, refreshed while it keeps earning standing, faded to DORMANT
then INVALIDATED as evidence goes stale, flipped to REVERSED when the opposing
thesis takes over, and COMPLETED when the position closes on a realised target.
"""

import pytest

from brain.campaign import (
    LEG_CLOSE,
    LEG_OPEN,
    LEG_SCALE_IN,
    Campaign,
    CampaignRegistry,
    CampaignState,
)


def _reg(**kw) -> CampaignRegistry:
    params = dict(
        enabled=True,
        dormant_after_seconds=100.0,
        invalidate_after_seconds=300.0,
    )
    params.update(kw)
    return CampaignRegistry(**params)


# ── Birth + refresh ─────────────────────────────────────────────────────────

def test_observe_thesis_opens_active_campaign():
    reg = _reg()
    camp = reg.observe_thesis("EURUSD", "LONG", True, ev_over_flat=0.5, confidence=0.8)
    assert camp is not None
    assert camp.state == CampaignState.ACTIVE
    assert camp.direction == "LONG"
    assert camp.refresh_count == 1
    assert reg.get("EURUSD", "LONG") is camp


def test_refresh_increments_and_updates_evidence():
    reg = _reg()
    reg.observe_thesis("EURUSD", "LONG", True, ev_over_flat=0.3, confidence=0.5)
    camp = reg.observe_thesis("EURUSD", "LONG", True, ev_over_flat=0.7, confidence=0.9)
    assert camp.refresh_count == 2
    assert camp.ev_over_flat == pytest.approx(0.7)
    assert camp.confidence == pytest.approx(0.9)


def test_non_actionable_read_is_noop():
    reg = _reg()
    assert reg.observe_thesis("EURUSD", "FLAT", False) is None
    assert reg.observe_thesis("EURUSD", "LONG", False) is None
    assert reg.get("EURUSD", "LONG") is None


# ── Reversal ──────────────────────────────────────────────────────────────────

def test_opposing_actionable_read_reverses_prior_campaign():
    reg = _reg()
    long_c = reg.observe_thesis("EURUSD", "LONG", True, ev_over_flat=0.5)
    short_c = reg.observe_thesis("EURUSD", "SHORT", True, ev_over_flat=0.5)
    # The long campaign flipped to REVERSED; a fresh short campaign is live.
    assert long_c.state == CampaignState.REVERSED
    assert reg.get("EURUSD", "LONG") is None
    assert short_c.state == CampaignState.ACTIVE
    assert reg.get("EURUSD", "SHORT") is short_c


# ── Legs ──────────────────────────────────────────────────────────────────────

def test_record_leg_attaches_to_active_campaign():
    reg = _reg()
    reg.observe_thesis("EURUSD", "LONG", True, ev_over_flat=0.5)
    reg.record_leg("EURUSD", "LONG", LEG_OPEN, size=1.0, price=1.1)
    reg.record_leg("EURUSD", "LONG", LEG_SCALE_IN, size=0.5, price=1.11)
    camp = reg.get("EURUSD", "LONG")
    assert len(camp.legs) == 2
    assert camp.open_legs == 2
    assert camp.legs[0].kind == LEG_OPEN


def test_record_leg_noop_without_campaign():
    reg = _reg()
    reg.record_leg("EURUSD", "LONG", LEG_OPEN, size=1.0)  # no active campaign
    assert reg.get("EURUSD", "LONG") is None


# ── Closes ──────────────────────────────────────────────────────────────────

def test_partial_close_keeps_campaign_active():
    reg = _reg()
    reg.observe_thesis("EURUSD", "LONG", True, ev_over_flat=0.5)
    camp = reg.observe_close("EURUSD", "LONG", "tp1_partial", pnl=12.0, won=True)
    assert camp.state == CampaignState.ACTIVE
    assert camp.realized_pnl == pytest.approx(12.0)
    assert reg.get("EURUSD", "LONG") is camp
    assert camp.legs[-1].kind == LEG_CLOSE


def test_stop_loss_close_invalidates_campaign():
    reg = _reg()
    reg.observe_thesis("EURUSD", "LONG", True, ev_over_flat=0.5)
    camp = reg.observe_close("EURUSD", "LONG", "stop_loss", pnl=-20.0, won=False)
    assert camp.state == CampaignState.INVALIDATED
    assert reg.get("EURUSD", "LONG") is None


def test_target_close_completes_campaign():
    reg = _reg()
    reg.observe_thesis("EURUSD", "LONG", True, ev_over_flat=0.5)
    camp = reg.observe_close("EURUSD", "LONG", "tp2_target", pnl=40.0, won=True)
    assert camp.state == CampaignState.COMPLETED
    assert reg.get("EURUSD", "LONG") is None


def test_reversal_close_marks_reversed():
    reg = _reg()
    reg.observe_thesis("EURUSD", "LONG", True, ev_over_flat=0.5)
    camp = reg.observe_close("EURUSD", "LONG", "thesis_reversal", pnl=5.0)
    assert camp.state == CampaignState.REVERSED


def test_observe_close_noop_without_campaign():
    reg = _reg()
    assert reg.observe_close("EURUSD", "LONG", "stop_loss", pnl=-1.0) is None


# ── Time-based decay ─────────────────────────────────────────────────────────

def test_decay_active_to_dormant_then_invalidated():
    reg = _reg(dormant_after_seconds=100.0, invalidate_after_seconds=300.0)
    reg.observe_thesis("EURUSD", "LONG", True, ev_over_flat=0.5, now=1_000.0)
    # Not yet stale.
    reg.decay(now=1_050.0)
    assert reg.get("EURUSD", "LONG").state == CampaignState.ACTIVE
    # Past dormancy but not invalidation.
    reg.decay(now=1_150.0)
    assert reg.get("EURUSD", "LONG").state == CampaignState.DORMANT
    # Past invalidation — moved to terminal history.
    reg.decay(now=1_400.0)
    assert reg.get("EURUSD", "LONG") is None


def test_fresh_evidence_revives_dormant_campaign():
    reg = _reg(dormant_after_seconds=100.0, invalidate_after_seconds=300.0)
    reg.observe_thesis("EURUSD", "LONG", True, ev_over_flat=0.5, now=1_000.0)
    reg.decay(now=1_150.0)
    assert reg.get("EURUSD", "LONG").state == CampaignState.DORMANT
    camp = reg.observe_thesis("EURUSD", "LONG", True, ev_over_flat=0.6, now=1_160.0)
    assert camp.state == CampaignState.ACTIVE


def test_invalidate_after_is_clamped_up_to_dormant():
    # Misconfigured: invalidate < dormant → clamped up so invalidation never
    # precedes dormancy.
    reg = CampaignRegistry(
        enabled=True, dormant_after_seconds=200.0, invalidate_after_seconds=50.0
    )
    assert reg.invalidate_after_seconds >= reg.dormant_after_seconds


# ── Status + housekeeping ────────────────────────────────────────────────────

def test_get_status_shape():
    reg = _reg()
    reg.observe_thesis("EURUSD", "LONG", True, ev_over_flat=0.5)
    reg.observe_thesis("GBPUSD", "SHORT", True, ev_over_flat=0.4)
    reg.observe_close("GBPUSD", "SHORT", "stop_loss", pnl=-10.0, won=False)
    status = reg.get_status()
    assert status["enabled"] is True
    assert status["live_campaigns"] == 1
    assert status["active"] == 1
    assert status["terminal_counts"].get("invalidated") == 1
    assert isinstance(status["live"], list)


def test_state_is_terminal():
    assert CampaignState.INVALIDATED.is_terminal
    assert CampaignState.REVERSED.is_terminal
    assert CampaignState.COMPLETED.is_terminal
    assert not CampaignState.ACTIVE.is_terminal
    assert not CampaignState.DORMANT.is_terminal


def test_reset_clears_all():
    reg = _reg()
    reg.observe_thesis("EURUSD", "LONG", True, ev_over_flat=0.5)
    reg.reset()
    assert reg.get_status()["live_campaigns"] == 0


def test_disabled_registry_still_functions_but_reports_disabled():
    # ``enabled`` only gates the bootstrap feed, not the registry API itself.
    reg = CampaignRegistry(enabled=False)
    camp = reg.observe_thesis("EURUSD", "LONG", True, ev_over_flat=0.5)
    assert isinstance(camp, Campaign)
    assert reg.get_status()["enabled"] is False
