"""Callback-level decision provenance lifecycle and restart recovery tests."""

import json
from types import SimpleNamespace

from event_driven_bootstrap import EventDrivenSystem
from persistence import domain_events as DE
from persistence import event_store as event_store_module
from persistence.event_store import EventStore


def test_entry_snapshot_is_recovered_by_exact_ticket_and_linked_to_close(
    tmp_path, monkeypatch,
):
    store = EventStore(db_path=str(tmp_path / "decision-provenance.db"))
    monkeypatch.setattr(event_store_module, "get_event_store", lambda: store)

    system = EventDrivenSystem.__new__(EventDrivenSystem)
    system._entry_context = {}
    system._known_open = {
        "broker-ticket-731": {
            "symbol": "XAUUSD",
            "direction": "BUY",
            "lots": 0.10,
        }
    }
    system._candidate_positions = {}
    system._zone_edge = SimpleNamespace(record_trade=lambda *args, **kwargs: None)
    system._calibration_engine = None
    system._pm = SimpleNamespace(get_deal_close_info=lambda ticket, platform: None)
    system._ctx = None
    system._safe_pip_size = lambda symbol: 0.01
    system._mark_booked = lambda ticket: True
    system._mgmt_store = SimpleNamespace(remove=lambda ticket: None)
    system._clear_inflight_manage_ticket = lambda ticket: None

    snapshot = {
        "schema": "apex.decision_feature_snapshot.v1",
        "snapshot_id": "decision-731:snapshot-1",
        "decision_id": "decision-731",
        "captured_at_decision": True,
        "snapshot_complete": True,
        "evidence_ids": ["evidence-1", "evidence-2"],
        "features": [
            {"evidence_id": "evidence-1", "source_timestamp_epoch": 100.0},
            {"evidence_id": "evidence-2", "source_timestamp_epoch": 101.0},
        ],
    }
    intent = SimpleNamespace(
        symbol="XAUUSD",
        direction="BUY",
        decision_id="decision-731",
        campaign_id="campaign-731",
        opportunity_id="opportunity-731",
        decision_feature_snapshot=snapshot,
        identity_version="canonical",
        timestamp=None,
    )

    system._handle_open_result(
        intent,
        SimpleNamespace(
            success=True,
            broker_response={"order_id": "broker-ticket-731"},
        ),
    )

    entry_rows = store.query_events(
        severity_min="DEBUG",
        event_types=[DE.DECISION_TRADE_ENTRY_SNAPSHOT],
        parent_id="broker-ticket-731",
        limit=10,
    )
    assert len(entry_rows) == 1
    entry_payload = json.loads(entry_rows[0]["payload_json"])
    assert entry_payload["decision_snapshot_complete"] is True
    assert entry_payload["snapshot_id"] == "decision-731:snapshot-1"

    # Simulate a process restart: retain only the durable event ledger and
    # broker's open-ticket record; discard all in-memory entry context.
    system._entry_context = {}
    close_intent = SimpleNamespace(
        position_ticket="broker-ticket-731",
        symbol="XAUUSD",
        direction="",
        source="test_close",
        reason="test close",
    )
    close_result = SimpleNamespace(
        success=True,
        broker_response=SimpleNamespace(
            platform="mt5",
            pnl=12.5,
            close_price=2310.0,
        ),
    )
    system._handle_close_result(close_intent, close_result)

    outcome_rows = store.query_events(
        severity_min="DEBUG",
        event_types=[DE.DECISION_TRADE_OUTCOME],
        parent_id="broker-ticket-731",
        limit=10,
    )
    assert len(outcome_rows) == 1
    outcome = json.loads(outcome_rows[0]["payload_json"])
    assert outcome["ticket"] == "broker-ticket-731"
    assert outcome["decision_id"] == "decision-731"
    assert outcome["campaign_id"] == "campaign-731"
    assert outcome["opportunity_id"] == "opportunity-731"
    assert outcome["snapshot_id"] == "decision-731:snapshot-1"
    assert outcome["decision_snapshot_complete"] is True
    assert outcome["entry_snapshot_present"] is True
    assert outcome["validation_status"] == "unvalidated"
    assert outcome["predictive_ability_demonstrated"] is False

    store.close()
