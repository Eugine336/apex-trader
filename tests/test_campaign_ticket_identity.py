from brain.campaign import (
    LEG_CLOSE,
    LEG_OPEN,
    LEG_RE_ENTRY,
    LEG_SCALE_IN,
    Campaign,
    CampaignLeg,
)


def _leg(kind, ticket, *, exit_cause="", platform="mt5", decision_id=""):
    return CampaignLeg(
        kind=kind,
        at_iso="2026-11-11T00:00:00Z",
        at_epoch=1_000.0,
        ticket=ticket,
        platform=platform,
        decision_id=decision_id,
        exit_cause=exit_cause,
    )


def test_open_tickets_track_order_level_campaign_identity():
    campaign = Campaign(
        campaign_id="campaign-1",
        symbol="EURUSD",
        direction="LONG",
        legs=[
            _leg(LEG_OPEN, "ticket-1"),
            _leg(LEG_SCALE_IN, "ticket-2"),
            _leg(LEG_RE_ENTRY, "ticket-3"),
            _leg(LEG_CLOSE, "ticket-1", exit_cause="tp1_partial"),
        ],
    )

    assert campaign.open_tickets == ["ticket-1", "ticket-2", "ticket-3"]
    assert campaign.to_dict()["open_tickets"] == ["ticket-1", "ticket-2", "ticket-3"]


def test_terminal_close_removes_only_the_matching_ticket():
    campaign = Campaign(
        campaign_id="campaign-2",
        symbol="EURUSD",
        direction="SHORT",
        legs=[
            _leg(LEG_OPEN, "ticket-1"),
            _leg(LEG_OPEN, "ticket-2"),
            _leg(LEG_CLOSE, "ticket-1", exit_cause="stop_loss"),
            _leg(LEG_CLOSE, "ticket-2", exit_cause="tp1_partial"),
        ],
    )

    assert campaign.open_tickets == ["ticket-2"]


def test_missing_ticket_is_never_inferred_from_symbol_or_side():
    campaign = Campaign(
        campaign_id="campaign-3",
        symbol="EURUSD",
        direction="LONG",
        legs=[_leg(LEG_OPEN, "")],
    )

    assert campaign.open_tickets == []


def test_system_context_enriches_campaigns_by_exact_ticket_not_symbol_side():
    from pathlib import Path

    source = (Path(__file__).resolve().parents[1] / "core" / "system_context.py").read_text(
        encoding="utf-8"
    )
    assert "camp_by_ticket" in source
    assert 'c.get("open_ticket_bindings", [])' in source
    assert "camp_by_key.get((sym, side))" not in source


def test_campaign_registry_attaches_only_one_exact_pending_open_leg():
    from brain.campaign import CampaignRegistry

    registry = CampaignRegistry(enabled=True)
    registry.observe_thesis(
        "EURUSD", "LONG", True, ev_over_flat=0.5,
        campaign_id="campaign-fill", decision_id="decision-fill",
    )
    registry.record_leg(
        "EURUSD", "LONG", LEG_OPEN, size=0.1,
        decision_id="decision-fill",
    )

    assert registry.attach_ticket(
        "EURUSD", "LONG", "campaign-fill", "decision-fill", "broker-ticket", "mt5",
    ) is True
    live = registry.get_status()["live"]
    assert live[0]["open_ticket_bindings"] == [
        {"platform": "mt5", "ticket": "broker-ticket"}
    ]


def test_campaign_registry_rejects_ambiguous_or_wrong_decision_ticket_attachment():
    from brain.campaign import CampaignRegistry

    registry = CampaignRegistry(enabled=True)
    registry.observe_thesis(
        "EURUSD", "LONG", True, ev_over_flat=0.5,
        campaign_id="campaign-fill", decision_id="decision-fill",
    )
    registry.record_leg("EURUSD", "LONG", LEG_OPEN, size=0.1, decision_id="decision-fill")
    registry.record_leg("EURUSD", "LONG", LEG_OPEN, size=0.1, decision_id="decision-fill")

    assert registry.attach_ticket(
        "EURUSD", "LONG", "campaign-fill", "decision-fill", "ticket-1", "mt5",
    ) is False
    assert registry.attach_ticket(
        "EURUSD", "LONG", "wrong-campaign", "decision-fill", "ticket-1", "mt5",
    ) is False
    live = registry.get_status()["live"]
    assert len(live) == 1
    assert live[0]["open_ticket_bindings"] == []
    assert live[0]["open_legs"] == 2


def test_explicit_open_rejection_removes_only_the_unfilled_pending_leg():
    from brain.campaign import CampaignRegistry

    registry = CampaignRegistry(enabled=True)
    registry.observe_thesis(
        "EURUSD", "LONG", True, ev_over_flat=0.5,
        campaign_id="campaign-fill", decision_id="decision-fill",
    )
    registry.record_leg("EURUSD", "LONG", LEG_OPEN, size=0.1, decision_id="decision-fill")

    assert registry.reject_pending_leg(
        "EURUSD", "LONG", "campaign-fill", "decision-fill",
    ) is True
    assert registry.get_status()["live"] == []


def test_terminal_close_removes_ticket_only_from_the_matching_platform():
    campaign = Campaign(
        campaign_id="campaign-platform-close",
        symbol="EURUSD",
        direction="LONG",
        legs=[
            _leg(LEG_OPEN, "shared-ticket", platform="mt5"),
            _leg(LEG_OPEN, "shared-ticket", platform="deriv"),
            _leg(LEG_CLOSE, "shared-ticket", exit_cause="stop_loss", platform="mt5"),
        ],
    )

    assert campaign.open_ticket_bindings == [{"platform": "deriv", "ticket": "shared-ticket"}]


def test_campaign_registry_close_removes_only_the_matching_platform_ticket():
    from brain.campaign import CampaignRegistry

    registry = CampaignRegistry(enabled=True)
    registry.observe_thesis(
        "EURUSD", "LONG", True, ev_over_flat=0.5,
        campaign_id="campaign-close", decision_id="decision-open",
    )
    registry.record_leg(
        "EURUSD", "LONG", LEG_OPEN, ticket="shared-ticket",
        platform="mt5", decision_id="decision-open",
    )
    registry.record_leg(
        "EURUSD", "LONG", LEG_OPEN, ticket="shared-ticket",
        platform="deriv", decision_id="decision-open-2",
    )

    campaign = registry.observe_close(
        "EURUSD", "LONG", "stop_loss",
        ticket="shared-ticket", platform="mt5",
    )

    assert campaign is not None
    assert campaign.open_ticket_bindings == [
        {"platform": "deriv", "ticket": "shared-ticket"}
    ]
