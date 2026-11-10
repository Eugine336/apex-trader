"""Brain management must bind to one confirmed broker ticket, not symbol/side."""

from types import SimpleNamespace

from cognition.brain import PositionView
from cognition.identity import IDENTITY_STATEMENT, IDENTITY_VERSION
from cognition.management_translator import ManagementAction
from event_driven_bootstrap import EventDrivenSystem


class _Aggregator:
    def __init__(self):
        self.submitted = []

    def submit(self, intents):
        self.submitted.extend(intents)


class _PlatformManager:
    def __init__(self, positions, *, confirmed=("mt5",), failed=()):
        self.snapshot = SimpleNamespace(
            positions=positions,
            confirmed_platforms=set(confirmed),
            failed_platforms=set(failed),
        )

    def get_open_positions_snapshot(self):
        return self.snapshot

    def get_all_open_positions(self):
        raise AssertionError("management must use confirmation-aware broker snapshots")


def _broker_position(ticket, *, platform="mt5", symbol="EURUSD", direction="BUY"):
    return SimpleNamespace(
        order_id=ticket,
        platform=platform,
        symbol=symbol,
        direction=direction,
        open_price=1.1000,
        current_price=1.1050,
        sl=1.0900,
        tp=1.1200,
        lots=0.1,
    )


def _position_view(ticket, *, platform="mt5", symbol="EURUSD", direction="LONG"):
    return PositionView(
        symbol=symbol,
        direction=direction,
        ticket=ticket,
        platform=platform,
    )


def _action(*, identity=IDENTITY_STATEMENT, identity_version=IDENTITY_VERSION):
    return ManagementAction(
        symbol="EURUSD",
        kind="close",
        direction="LONG",
        reason="Brain invalidated the held opportunity",
        decision_id="decision-1",
        campaign_id="campaign-1",
        opportunity_id="opportunity-1",
        action_id="action-1",
        identity_version=identity_version,
        identity=identity,
    )


def _sink(positions, *, confirmed=("mt5",), failed=()):
    system = EventDrivenSystem.__new__(EventDrivenSystem)
    system._pm = _PlatformManager(positions, confirmed=confirmed, failed=failed)
    system._aggregator = _Aggregator()
    return system._make_management_sink(), system._aggregator


def test_management_resolves_exact_ticket_when_same_symbol_and_side_have_multiple_positions():
    sink, aggregator = _sink([
        _broker_position("ticket-1"),
        _broker_position("ticket-2"),
    ])

    accepted = sink(_action(), _position_view("ticket-2"))

    assert accepted is True
    assert len(aggregator.submitted) == 1
    assert aggregator.submitted[0].position_ticket == "ticket-2"
    assert aggregator.submitted[0].decision_id == "decision-1"


def test_management_fails_closed_when_brain_position_has_no_ticket_or_platform():
    sink, aggregator = _sink([_broker_position("ticket-1")])

    accepted = sink(_action(), _position_view(""))

    assert accepted is False
    assert aggregator.submitted == []


def test_management_fails_closed_when_position_platform_is_unconfirmed():
    sink, aggregator = _sink(
        [_broker_position("ticket-1")],
        confirmed=(),
        failed=("mt5",),
    )

    accepted = sink(_action(), _position_view("ticket-1"))

    assert accepted is False
    assert aggregator.submitted == []


def test_management_fails_closed_when_exact_broker_identity_is_ambiguous():
    sink, aggregator = _sink([
        _broker_position("ticket-1"),
        _broker_position("ticket-1"),
    ])

    accepted = sink(_action(), _position_view("ticket-1"))

    assert accepted is False
    assert aggregator.submitted == []


def test_management_fails_closed_when_platform_or_side_does_not_match():
    sink, aggregator = _sink([
        _broker_position("ticket-1", platform="deriv", direction="BUY"),
    ])

    accepted = sink(_action(), _position_view("ticket-1", platform="mt5"))

    assert accepted is False
    assert aggregator.submitted == []


def test_management_rejects_noncanonical_or_unidentified_decisions():
    sink, aggregator = _sink([_broker_position("ticket-1")])

    bad_identity = sink(
        _action(identity="legacy identity"),
        _position_view("ticket-1"),
    )
    missing_decision = sink(
        ManagementAction(
            symbol="EURUSD",
            kind="close",
            direction="LONG",
            action_id="action-1",
            identity_version=IDENTITY_VERSION,
            identity=IDENTITY_STATEMENT,
        ),
        _position_view("ticket-1"),
    )

    assert bad_identity is False
    assert missing_decision is False
    assert aggregator.submitted == []
