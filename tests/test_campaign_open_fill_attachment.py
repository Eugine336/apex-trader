from types import SimpleNamespace

from brain.campaign import CampaignRegistry, LEG_OPEN
from event_driven_bootstrap import EventDrivenSystem
from execution.intents import Intent


class _PlatformManager:
    def __init__(self, positions, *, confirmed=("mt5",), failed=()):
        self.snapshot = SimpleNamespace(
            positions=positions,
            confirmed_platforms=set(confirmed),
            failed_platforms=set(failed),
        )

    def get_open_positions_snapshot(self):
        return self.snapshot


def _system(*, positions, confirmed=("mt5",), failed=()):
    registry = CampaignRegistry(enabled=True)
    registry.observe_thesis(
        "EURUSD", "LONG", True, ev_over_flat=0.5,
        campaign_id="campaign-1", decision_id="decision-1",
    )
    registry.record_leg(
        "EURUSD", "LONG", LEG_OPEN, size=0.1, decision_id="decision-1",
    )
    system = EventDrivenSystem.__new__(EventDrivenSystem)
    system._ctx = SimpleNamespace(campaign_registry=registry)
    system._pm = _PlatformManager(positions, confirmed=confirmed, failed=failed)
    return system, registry


def _intent(order_type="MARKET"):
    return Intent.open(
        symbol="EURUSD",
        direction="LONG",
        lots=0.1,
        sl=1.0900,
        tp=1.1200,
        source="brain",
        reason="verified opportunity",
        decision_id="decision-1",
        campaign_id="campaign-1",
        opportunity_id="opportunity-1",
        order_type=order_type,
    )


def _result(*, order_status="FILLED", ticket="ticket-1", platform="mt5", success=True):
    response = SimpleNamespace(
        success=success,
        order_status=order_status,
        order_id=ticket,
        platform=platform,
    )
    return SimpleNamespace(
        success=success,
        rejected_by_gate=False,
        broker_response=response,
    )


def _position(ticket="ticket-1", platform="mt5"):
    return SimpleNamespace(
        order_id=ticket,
        platform=platform,
        symbol="EURUSD",
        direction="BUY",
    )


def test_successful_market_fill_attaches_exact_confirmed_ticket_to_campaign():
    system, registry = _system(positions=[_position()])

    system._handle_execution_result(_intent(), _result())

    live = registry.get_status()["live"]
    assert live[0]["open_ticket_bindings"] == [
        {"platform": "mt5", "ticket": "ticket-1"}
    ]


def test_pending_order_placement_is_not_misclassified_as_open_position():
    system, registry = _system(positions=[])

    system._handle_execution_result(_intent("LIMIT"), _result(order_status="PLACED"))

    assert registry.get_status()["live"][0]["open_ticket_bindings"] == []


def test_fill_is_not_attached_without_exact_broker_snapshot_match():
    system, registry = _system(positions=[_position(ticket="other-ticket")])

    system._handle_execution_result(_intent(), _result(ticket="ticket-1"))

    assert registry.get_status()["live"][0]["open_ticket_bindings"] == []


def test_fill_is_not_attached_when_platform_is_not_confirmed():
    system, registry = _system(
        positions=[_position()],
        confirmed=(),
        failed=("mt5",),
    )

    system._handle_execution_result(_intent(), _result())

    assert registry.get_status()["live"][0]["open_ticket_bindings"] == []


def test_explicit_broker_rejection_removes_the_pending_campaign_leg():
    system, registry = _system(positions=[])
    rejected = SimpleNamespace(
        success=False,
        rejected_by_gate=False,
        broker_response=SimpleNamespace(success=False, error="rejected"),
    )

    system._handle_execution_result(_intent(), rejected)

    assert registry.get_status()["live"][0]["open_ticket_bindings"] == []
    assert registry.get_status()["live"][0]["open_legs"] == 0
