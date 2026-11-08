from __future__ import annotations

import json
from types import SimpleNamespace

from cognition.management_action_ledger import ManagementActionLedger


class _Store:
    def __init__(self):
        self.events = []
        self.seq = 0
        self.is_degraded = False

    def query_events(self, *, event_types=None, symbol=None, correlation_id=None, **kwargs):
        return [
            row for row in self.events
            if (not event_types or row["event_type"] in event_types)
            and (symbol is None or row["symbol"] == symbol)
            and (correlation_id is None or row["correlation_id"] == correlation_id)
        ]

    def emit(self, event_type, severity, *, symbol=None, correlation_id=None,
             parent_id=None, source_module=None, payload=None, durable=False):
        self.seq += 1
        self.events.append({
            "event_type": event_type,
            "symbol": symbol,
            "correlation_id": correlation_id,
            "seq": self.seq,
            "payload_json": json.dumps(payload or {}),
        })
        return f"event-{self.seq}"


def _action():
    return SimpleNamespace(
        action_id="decision-1:close:ticket-1",
        symbol="EURUSD",
        kind="close",
        decision_id="decision-1",
        campaign_id="campaign-1",
        opportunity_id="opportunity-1",
        to_dict=lambda: {
            "action_id": "decision-1:close:ticket-1",
            "symbol": "EURUSD",
            "kind": "close",
            "decision_id": "decision-1",
            "campaign_id": "campaign-1",
            "opportunity_id": "opportunity-1",
        },
    )


def test_action_ledger_write_ahead_record_prevents_duplicate_dispatch():
    store = _Store()
    ledger = ManagementActionLedger(store)
    action = _action()
    position = SimpleNamespace(ticket="ticket-1", lots=0.2)

    assert ledger.begin(action, position) == (True, "SUBMITTED")
    assert ledger.begin(action, position) == (False, "SUBMITTED")
    assert len(store.events) == 1


def test_ambiguous_action_is_not_replayed_until_broker_acknowledgement():
    store = _Store()
    ledger = ManagementActionLedger(store)
    action = _action()
    position = SimpleNamespace(ticket="ticket-1", lots=0.2)

    assert ledger.begin(action, position)[0] is True
    assert ledger.finish(
        action, "AMBIGUOUS", error="broker response timed out",
    ) is True
    assert ledger.status(action.action_id, action.symbol) == "AMBIGUOUS"
    assert ledger.begin(action, position) == (False, "AMBIGUOUS")


def test_explicit_rejection_allows_a_new_attempt_but_acknowledgement_does_not():
    store = _Store()
    ledger = ManagementActionLedger(store)
    action = _action()
    position = SimpleNamespace(ticket="ticket-1", lots=0.2)

    assert ledger.begin(action, position)[0] is True
    assert ledger.finish(action, "REJECTED", error="invalid request") is True
    assert ledger.begin(action, position)[0] is True
    assert ledger.finish(
        action, "ACKNOWLEDGED",
        response=SimpleNamespace(success=True, order_id="deal-22"),
    ) is True
    assert ledger.status(action.action_id, action.symbol) == "ACKNOWLEDGED"
    assert ledger.begin(action, position) == (False, "ACKNOWLEDGED")


def test_ledger_fails_closed_when_durable_event_store_is_degraded():
    store = _Store()
    store.is_degraded = True
    ledger = ManagementActionLedger(store)
    assert ledger.begin(_action(), SimpleNamespace(ticket="ticket-1", lots=0.2)) == (
        None, "management_action_ledger_unavailable",
    )
