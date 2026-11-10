from brain.campaign import (
    LEG_CLOSE,
    LEG_OPEN,
    LEG_RE_ENTRY,
    LEG_SCALE_IN,
    Campaign,
    CampaignLeg,
)


def _leg(kind, ticket, *, exit_cause=""):
    return CampaignLeg(
        kind=kind,
        at_iso="2026-11-11T00:00:00Z",
        at_epoch=1_000.0,
        ticket=ticket,
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
    assert 'c.get("open_tickets", [])' in source
    assert "camp_by_key.get((sym, side))" not in source
