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


# ── Autonomous post-mortem (reasoning quality vs outcome) ────────────────────

def _pm_reg(**kw) -> CampaignRegistry:
    params = dict(
        enabled=True,
        dormant_after_seconds=100.0,
        invalidate_after_seconds=300.0,
        postmortem_enabled=True,
        sound_evidence_threshold=0.5,
        evidence_full_refreshes=2,
    )
    params.update(kw)
    return CampaignRegistry(**params)


def test_postmortem_validated_win_on_strong_evidence():
    reg = _pm_reg()
    reg.observe_thesis("EURUSD", "LONG", True, ev_over_flat=0.6, confidence=0.9)
    reg.observe_thesis("EURUSD", "LONG", True, ev_over_flat=0.7, confidence=0.9)
    camp = reg.observe_close("EURUSD", "LONG", "tp2_target", pnl=40.0, won=True)
    pm = camp.postmortem
    assert pm is not None
    assert pm.verdict == "validated"
    assert pm.outcome_won is True
    assert pm.reasoning_quality >= 0.5
    assert pm.confidence_delta > 0.0


def test_postmortem_lucky_win_on_thin_evidence_is_penalised():
    # Thin evidence: one low-confidence refresh, full-refresh bar of 5.
    reg = _pm_reg(evidence_full_refreshes=5)
    reg.observe_thesis("EURUSD", "LONG", True, ev_over_flat=0.2, confidence=0.1)
    camp = reg.observe_close("EURUSD", "LONG", "tp2_target", pnl=30.0, won=True)
    pm = camp.postmortem
    assert pm.verdict == "lucky"
    assert pm.outcome_won is True
    assert pm.confidence_delta <= 0.0  # a win must not be rewarded when reasoning was weak


def test_postmortem_sound_but_unlucky_loss_on_strong_evidence():
    reg = _pm_reg()
    reg.observe_thesis("EURUSD", "LONG", True, ev_over_flat=0.6, confidence=0.9)
    reg.observe_thesis("EURUSD", "LONG", True, ev_over_flat=0.7, confidence=0.9)
    camp = reg.observe_close("EURUSD", "LONG", "stop_loss", pnl=-20.0, won=False)
    pm = camp.postmortem
    assert pm.verdict == "sound_but_unlucky"
    assert pm.outcome_won is False
    assert pm.confidence_delta > 0.0  # sound process is mildly reinforced despite the loss


def test_postmortem_deserved_loss_on_thin_evidence():
    reg = _pm_reg(evidence_full_refreshes=5)
    reg.observe_thesis("EURUSD", "LONG", True, ev_over_flat=0.1, confidence=0.1)
    camp = reg.observe_close("EURUSD", "LONG", "stop_loss", pnl=-10.0, won=False)
    pm = camp.postmortem
    assert pm.verdict == "deserved_loss"
    assert pm.confidence_delta < 0.0


def test_postmortem_verdict_counts_in_status():
    reg = _pm_reg()
    reg.observe_thesis("EURUSD", "LONG", True, ev_over_flat=0.6, confidence=0.9)
    reg.observe_thesis("EURUSD", "LONG", True, ev_over_flat=0.7, confidence=0.9)
    reg.observe_close("EURUSD", "LONG", "tp2_target", pnl=40.0, won=True)
    status = reg.get_status()
    assert status["postmortem_verdicts"].get("validated") == 1


def test_avg_confidence_and_peak_tracked():
    reg = _pm_reg()
    reg.observe_thesis("EURUSD", "LONG", True, ev_over_flat=0.3, confidence=0.4)
    camp = reg.observe_thesis("EURUSD", "LONG", True, ev_over_flat=0.9, confidence=0.6)
    assert camp.peak_ev_over_flat == pytest.approx(0.9)
    assert camp.avg_confidence == pytest.approx(0.5)


def test_postmortem_can_be_disabled():
    reg = _pm_reg(postmortem_enabled=False)
    reg.observe_thesis("EURUSD", "LONG", True, ev_over_flat=0.6, confidence=0.9)
    camp = reg.observe_close("EURUSD", "LONG", "tp2_target", pnl=40.0, won=True)
    assert camp.postmortem is None



# ── Part XVIII — Brain-driven management, health & portfolio ─────────────────

def test_record_management_births_and_sets_health():
    reg = _reg()
    camp = reg.record_management(
        "XAUUSD", "LONG", "tighten_risk",
        confidence=0.72, ev_over_flat=1.5, uncertainty=0.2,
    )
    assert camp is not None
    assert camp.objective == "tighten_risk"
    assert camp.last_action == "tighten_risk"
    assert camp.refresh_count == 1
    assert reg.get("XAUUSD", "LONG") is camp


def test_record_management_refreshes_existing_campaign():
    reg = _reg()
    reg.observe_thesis("EURUSD", "LONG", True, ev_over_flat=0.3, confidence=0.6)
    camp = reg.record_management("EURUSD", "LONG", "hold", confidence=0.7)
    assert camp.refresh_count == 2       # birth + management refresh
    assert camp.objective == "hold"


def test_campaign_health_reports_expected_fields():
    reg = _reg()
    reg.record_management("GBPUSD", "SHORT", "hold", confidence=0.8, ev_over_flat=2.0)
    h = reg.get("GBPUSD", "SHORT").health()
    for key in ("thesis", "ev_over_flat", "confidence", "uncertainty",
                "opportunity_strength", "evidence_quality", "giveback_r",
                "state", "open_legs"):
        assert key in h
    assert h["ev_over_flat"] == pytest.approx(2.0)
    assert h["thesis"] == "hold"


def test_live_campaigns_snapshot():
    reg = _reg()
    reg.record_management("EURUSD", "LONG", "hold", confidence=0.6)
    reg.record_management("USDJPY", "SHORT", "hold", confidence=0.6)
    live = reg.live_campaigns()
    assert {c.symbol for c in live} == {"EURUSD", "USDJPY"}


def test_portfolio_assessment_flags_correlated_usd_cluster():
    reg = _reg()
    # Three campaigns all sharing USD as a leg → concentrated correlated book.
    reg.record_management("EURUSD", "LONG", "hold", confidence=0.6)
    reg.record_management("GBPUSD", "LONG", "hold", confidence=0.6)
    reg.record_management("AUDUSD", "LONG", "hold", confidence=0.6)
    pa = reg.portfolio_assessment()
    assert pa["campaign_count"] == 3
    usd = [c for c in pa["correlated_clusters"] if c["component"] == "USD"]
    assert usd and usd[0]["count"] == 3
    assert pa["concentration"] == pytest.approx(1.0)   # USD in every campaign
    assert pa["concentration_warning"] is True


def test_portfolio_assessment_empty_below_two():
    reg = _reg()
    reg.record_management("EURUSD", "LONG", "hold", confidence=0.6)
    pa = reg.portfolio_assessment()
    assert pa["campaign_count"] == 1
    assert pa["correlated_clusters"] == []


def test_symbol_components_split():
    from brain.campaign import symbol_components
    assert symbol_components("EURUSD") == {"EUR", "USD"}
    assert symbol_components("XAUUSD") == {"XAU", "USD"}
    assert symbol_components("GER40") == {"GER40"}


# ── Part XVIII Art 11 — portfolio reallocation + scale saturation ────────────

def test_reallocation_trims_weakest_correlated_campaign():
    reg = _reg()
    reg.record_management("EURUSD", "LONG", "hold", confidence=0.9, ev_over_flat=2.0)
    reg.record_management("GBPUSD", "LONG", "hold", confidence=0.8, ev_over_flat=1.0)
    reg.record_management("AUDUSD", "LONG", "hold", confidence=0.2, ev_over_flat=0.0)
    targets = reg.reallocation_targets(max_per_component=2, concentration_limit=0.6)
    assert len(targets) == 1
    assert targets[0].symbol == "AUDUSD"        # weakest of the USD cluster
    assert targets[0].kind == "partial_close"
    assert 0.0 < targets[0].fraction < 1.0


def test_reallocation_none_when_within_allowance():
    reg = _reg()
    # USD carried by exactly two campaigns == the allowance → nothing to trim.
    reg.record_management("EURUSD", "LONG", "hold", confidence=0.8, ev_over_flat=1.0)
    reg.record_management("USDJPY", "SHORT", "hold", confidence=0.8, ev_over_flat=1.0)
    assert reg.reallocation_targets(max_per_component=2, concentration_limit=0.6) == []


def test_reallocation_respects_concentration_limit():
    reg = _reg()
    for s in ("EURUSD", "GBPUSD", "AUDUSD"):
        reg.record_management(s, "LONG", "hold", confidence=0.6, ev_over_flat=1.0)
    # Concentration is 1.0 (USD everywhere); a limit above that suppresses action.
    assert reg.reallocation_targets(max_per_component=2, concentration_limit=1.5) == []


def test_is_component_saturated():
    reg = _reg()
    for s in ("EURUSD", "GBPUSD", "AUDUSD"):
        reg.record_management(s, "LONG", "hold", confidence=0.6, ev_over_flat=1.0)
    assert reg.is_component_saturated("NZDUSD", max_per_component=2) is True   # USD x3 > 2
    assert reg.is_component_saturated("EURGBP", max_per_component=2) is False  # EUR x1, GBP x1
