from types import SimpleNamespace
import json

from cognition.contracts import DecisionType
from cognition.identity import IDENTITY_STATEMENT, IDENTITY_VERSION
from cognition.loop import CognitionLoop
from cognition.management_action_ledger import ManagementActionLedger
from cognition.memory import CampaignMemoryStore


class _UnusedBrain:
    pass


class _UnusedConsolidator:
    pass


def _output():
    return SimpleNamespace(decision=SimpleNamespace(
        decision_type=DecisionType.SCALE_OUT,
        symbol="EURUSD",
        confidence=0.8,
        thesis="completed concept milestone",
        decision_id="decision-1",
        campaign_id="campaign-1",
        identity_version=IDENTITY_VERSION,
        identity=IDENTITY_STATEMENT,
        questions_answered={
            "native_management_baseline": {
                "management_plan": {
                    "kind": "milestone_partial",
                    "milestone_id": "market_structure:H1:1:1.105",
                    "milestone_price": 1.105,
                    "partial_fraction": 0.5,
                    "action_id": "campaign-1:milestone:market_structure:H1:1:1.105",
                },
            },
        },
    ))


def _position():
    return SimpleNamespace(
        symbol="EURUSD",
        direction="LONG",
        campaign_id="campaign-1",
        opportunity_id="opportunity-1",
        lots=0.2,
        ticket="ticket-1",
    )



class _EventStore:
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

    def emit(
        self, event_type, severity, *, symbol=None, correlation_id=None,
        parent_id=None, source_module=None, payload=None, durable=False,
    ):
        self.seq += 1
        self.events.append({
            "event_type": event_type,
            "severity": severity,
            "symbol": symbol,
            "correlation_id": correlation_id,
            "seq": self.seq,
            "payload_json": json.dumps(payload or {}),
        })
        return f"event-{self.seq}"


def _store():
    store = CampaignMemoryStore(db_path=":memory:")
    store.record_open(
        symbol="EURUSD",
        direction="LONG",
        campaign_id="campaign-1",
        fingerprint={},
        spec={"thesis": "structural continuation"},
        now_ms=1_000,
    )
    return store


def _loop(store=None, ledger=None):
    loop = CognitionLoop(
        _UnusedBrain(), _UnusedConsolidator(), lambda: [],
        management_mode="live",
        management_action_ledger=ledger or ManagementActionLedger(_EventStore()),
    )
    loop._memory = store
    return loop


def test_milestone_is_persisted_before_sink_and_duplicate_submission_is_blocked():
    store = _store()
    calls = []
    loop = _loop(store)
    loop.set_management_sink(lambda action, position: (
        calls.append(action) or SimpleNamespace(
            success=True,
            broker_response=SimpleNamespace(success=True, deal=1234),
        )
    ))
    output, position = _output(), _position()

    loop._realise_management(output, position)
    loop._realise_management(output, position)

    assert len(calls) == 1
    progress = store.get_management_progress(
        "campaign-1", current_size=0.2, now_ms=10_000,
    )
    assert progress["pending_milestone"]["milestone_id"] == "market_structure:H1:1:1.105"
    assert loop.get_status()["manage_submitted"] == 1
    store.close()


def test_milestone_partial_is_suppressed_without_durable_campaign_memory():
    calls = []
    loop = _loop()
    loop.set_management_sink(lambda action, position: calls.append(action))

    loop._realise_management(_output(), _position())

    assert calls == []
    assert loop.get_status()["manage_submitted"] == 0
    assert loop.get_status()["manage_suppressed"] == 1


def test_explicit_sink_rejection_clears_write_ahead_marker_for_retry():
    store = _store()
    calls = []

    def sink(action, position):
        calls.append(action)
        return False if len(calls) == 1 else None

    loop = _loop(store)
    loop.set_management_sink(sink)
    loop._realise_management(_output(), _position())

    rejected = store.get_management_progress(
        "campaign-1", current_size=0.2, now_ms=10_000,
    )
    assert rejected["pending_milestone"] is None

    loop._realise_management(_output(), _position())
    accepted = store.get_management_progress(
        "campaign-1", current_size=0.2, now_ms=11_000,
    )
    assert len(calls) == 2
    assert accepted["pending_milestone"]["milestone_id"] == "market_structure:H1:1:1.105"
    store.close()


def test_ambiguous_management_sink_result_is_durable_and_never_replayed():
    store = _store()
    event_store = _EventStore()
    ledger = ManagementActionLedger(event_store)
    calls = []
    loop = _loop(store, ledger)

    def sink(action, position):
        calls.append(action)
        return SimpleNamespace(
            success=False,
            rejected_by_gate=False,
            error="broker response timed out",
            broker_response=None,
        )

    loop.set_management_sink(sink)
    output, position = _output(), _position()
    loop._realise_management(output, position)
    loop._realise_management(output, position)

    assert len(calls) == 1
    assert loop.get_status()["manage_ambiguous"] == 1
    assert ledger.status(calls[0].action_id, "EURUSD") == "AMBIGUOUS"
    progress = store.get_management_progress(
        "campaign-1", current_size=0.2, now_ms=10_000,
    )
    assert progress["pending_milestone"]["milestone_id"] == "market_structure:H1:1:1.105"
    store.close()


def test_verified_broker_reconciliation_resolves_ambiguous_milestone_and_memory():
    import time

    store = _store()
    ledger = ManagementActionLedger(_EventStore())
    loop = _loop(store, ledger)
    loop.set_management_sink(lambda action, position: None)
    output, position = _output(), _position()

    loop._realise_management(output, position)
    action_id = "campaign-1:milestone:market_structure:H1:1:1.105"
    assert ledger.status(action_id, "EURUSD") == "AMBIGUOUS"

    evidence = {
        "source": "mt5.position_snapshot",
        "source_timestamp_epoch": time.time(),
        "source_timestamp_verified": True,
        "symbol": "EURUSD",
        "position_ticket": "ticket-1",
        "book_complete": True,
        "platform_confirmed": True,
        "position_present": True,
        "current_lots": 0.1,
    }
    assert loop.reconcile_management_action(action_id, "EURUSD", evidence) is True
    assert ledger.status(action_id, "EURUSD") == "ACKNOWLEDGED"
    progress = store.get_management_progress(
        "campaign-1", current_size=0.1, now_ms=10_000,
    )
    assert progress["pending_milestone"] is None
    assert "market_structure:H1:1:1.105" in progress["completed_milestones"]
    assert action_id not in loop._management_unresolved_actions
    store.close()


def test_async_management_sink_queue_acceptance_is_not_broker_acknowledgement():
    store = _store()
    ledger = ManagementActionLedger(_EventStore())
    calls = []
    loop = _loop(store, ledger)
    loop.set_management_sink(lambda action, position: calls.append(action) or True)
    output, position = _output(), _position()

    assert loop._realise_management(output, position) is True
    action = calls[0]
    assert ledger.status(action.action_id, "EURUSD") == "DISPATCHED"
    assert loop.get_status()["manage_submitted"] == 1
    assert loop.get_status()["manage_ambiguous"] == 0
    assert loop._realise_management(output, position) is False
    assert len(calls) == 1
    store.close()


def test_broker_identifier_without_explicit_success_remains_dispatched():
    store = _store()
    ledger = ManagementActionLedger(_EventStore())
    calls = []
    loop = _loop(store, ledger)
    loop.set_management_sink(lambda action, position: calls.append(action) or SimpleNamespace(
        success=True,
        broker_response=SimpleNamespace(deal=4455),
    ))
    output, position = _output(), _position()

    assert loop._realise_management(output, position) is True
    action = calls[0]
    assert ledger.status(action.action_id, "EURUSD") == "DISPATCHED"
    assert ledger.pending_actions("EURUSD")
    store.close()


def test_actual_executor_broker_acknowledgement_resolves_dispatched_milestone():
    store = _store()
    ledger = ManagementActionLedger(_EventStore())
    loop = _loop(store, ledger)
    calls = []
    loop.set_management_sink(lambda action, position: calls.append(action) or True)
    loop._realise_management(_output(), _position())
    action = calls[0]
    assert ledger.status(action.action_id, "EURUSD") == "DISPATCHED"

    intent = SimpleNamespace(
        action_id=action.action_id,
        symbol=action.symbol,
        intent_type=SimpleNamespace(name="PARTIAL_CLOSE"),
        position_ticket="ticket-1",
        decision_id=action.decision_id,
        campaign_id=action.campaign_id,
        opportunity_id=action.opportunity_id,
        reason=f"milestone [milestone_id={action.milestone_id}]",
    )
    result = SimpleNamespace(
        success=True,
        rejected_by_gate=False,
        error="",
        broker_response=SimpleNamespace(
            success=True, order_id="deal-77", lots_closed=0.1,
        ),
    )
    assert loop.record_management_execution_result(intent, result) is True
    assert ledger.status(action.action_id, "EURUSD") == "ACKNOWLEDGED"
    assert action.action_id not in loop._management_unresolved_actions
    progress = store.get_management_progress(
        "campaign-1", current_size=0.1, now_ms=10_000,
    )
    assert progress["pending_milestone"] is None
    assert action.milestone_id in progress["completed_milestones"]
    store.close()


def test_explicit_executor_gate_rejection_resolves_dispatched_milestone_for_retry():
    store = _store()
    ledger = ManagementActionLedger(_EventStore())
    loop = _loop(store, ledger)
    calls = []
    loop.set_management_sink(lambda action, position: calls.append(action) or True)
    loop._realise_management(_output(), _position())
    action = calls[0]
    intent = SimpleNamespace(
        action_id=action.action_id,
        symbol=action.symbol,
        intent_type=SimpleNamespace(name="PARTIAL_CLOSE"),
        position_ticket="ticket-1",
        decision_id=action.decision_id,
        campaign_id=action.campaign_id,
        opportunity_id=action.opportunity_id,
        reason=f"milestone [milestone_id={action.milestone_id}]",
    )
    result = SimpleNamespace(
        success=False,
        rejected_by_gate=True,
        error="risk gate rejected",
        broker_response=None,
    )
    assert loop.record_management_execution_result(intent, result) is True
    assert ledger.status(action.action_id, "EURUSD") == "REJECTED"
    assert action.action_id not in loop._management_unresolved_actions
    progress = store.get_management_progress(
        "campaign-1", current_size=0.2, now_ms=10_000,
    )
    assert progress["pending_milestone"] is None
    store.close()


def test_gate_rejection_does_not_clear_an_earlier_ambiguous_management_action():
    store = _store()
    ledger = ManagementActionLedger(_EventStore())
    loop = _loop(store, ledger)
    loop.set_management_sink(lambda action, position: None)
    loop._realise_management(_output(), _position())
    action_id = "campaign-1:milestone:market_structure:H1:1:1.105"
    intent = SimpleNamespace(
        action_id=action_id, symbol="EURUSD",
        intent_type=SimpleNamespace(name="PARTIAL_CLOSE"),
        position_ticket="ticket-1", decision_id="decision-1",
        campaign_id="campaign-1", opportunity_id="opportunity-1",
    )
    result = SimpleNamespace(
        success=False, rejected_by_gate=True, error="risk gate rejected",
        broker_response=None,
    )

    assert loop.record_management_execution_result(intent, result) is False
    assert ledger.status(action_id, "EURUSD") == "AMBIGUOUS"
    assert action_id in loop._management_unresolved_actions
    assert store.get_management_progress("campaign-1", current_size=0.2, now_ms=10_000)["pending_milestone"] is not None
    store.close()


def test_explicit_broker_rejection_resolves_an_ambiguous_management_action():
    store = _store()
    ledger = ManagementActionLedger(_EventStore())
    loop = _loop(store, ledger)
    loop.set_management_sink(lambda action, position: None)
    loop._realise_management(_output(), _position())
    action_id = "campaign-1:milestone:market_structure:H1:1:1.105"
    intent = SimpleNamespace(
        action_id=action_id, symbol="EURUSD",
        intent_type=SimpleNamespace(name="PARTIAL_CLOSE"),
        position_ticket="ticket-1", decision_id="decision-1",
        campaign_id="campaign-1", opportunity_id="opportunity-1",
    )
    result = SimpleNamespace(
        success=False, rejected_by_gate=False, error="broker rejected request",
        broker_response=SimpleNamespace(success=False, retcode=10016, error="invalid stops"),
    )

    assert loop.record_management_execution_result(intent, result) is True
    assert ledger.status(action_id, "EURUSD") == "REJECTED"
    assert action_id not in loop._management_unresolved_actions
    assert store.get_management_progress("campaign-1", current_size=0.2, now_ms=10_000)["pending_milestone"] is None
    store.close()

def test_fast_flush_callback_ack_cannot_be_overwritten_by_late_queue_return():
    store = _store()
    ledger = ManagementActionLedger(_EventStore())
    loop = _loop(store, ledger)
    calls = []

    def sink(action, position):
        calls.append(action)
        intent = SimpleNamespace(
            action_id=action.action_id,
            symbol=action.symbol,
            intent_type=SimpleNamespace(name="PARTIAL_CLOSE"),
            position_ticket="ticket-1",
            decision_id=action.decision_id,
            campaign_id=action.campaign_id,
            opportunity_id=action.opportunity_id,
            reason=f"milestone [milestone_id={action.milestone_id}]",
        )
        result = SimpleNamespace(
            success=True,
            rejected_by_gate=False,
            error="",
            broker_response=SimpleNamespace(
                success=True, order_id="deal-fast", lots_closed=0.1,
            ),
        )
        assert loop.record_management_execution_result(intent, result) is True
        return True

    loop.set_management_sink(sink)
    output, position = _output(), _position()
    assert loop._realise_management(output, position) is True
    action = calls[0]
    assert ledger.status(action.action_id, "EURUSD") == "ACKNOWLEDGED"
    assert action.action_id not in loop._management_unresolved_actions
    assert loop._realise_management(output, position) is False
    assert len(calls) == 1
    store.close()
