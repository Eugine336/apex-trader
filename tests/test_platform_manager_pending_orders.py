from datetime import datetime, timezone
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock

from platforms.platform_manager import PlatformManager
from platforms.base_connector import OrderResult
from platforms.mt5.mt5_connector import MT5Connector


class PendingConnector:
    def __init__(self, live=None, history=None, cancel_result=True, fail=False):
        self.live = list(live or [])
        self.history = list(history or [])
        self.cancel_result = cancel_result
        self.fail = fail
        self.deals = []
        self.deal_fail = False
        self.cancelled = []

    def get_pending_order_snapshots(self):
        if self.fail:
            raise RuntimeError("broker order book unavailable")
        return list(self.live)

    def get_order_history(self, start, end):
        if self.fail:
            raise RuntimeError("broker history unavailable")
        return list(self.history)

    def get_deal_history_checked(self, start, end):
        if self.deal_fail:
            raise RuntimeError("broker deal history unavailable")
        return list(self.deals)

    def cancel_pending_order(self, ticket):
        self.cancelled.append(str(ticket))
        return self.cancel_result


def _manager(connectors):
    manager = object.__new__(PlatformManager)
    manager.mt5_connectors = connectors
    manager._mt5_connected_flags = [True] * len(connectors)
    manager._broker_write_lock = threading.Lock()
    return manager


def _pending(ticket="901", symbol="EURUSD", **overrides):
    return {
        "ticket": ticket, "symbol": symbol, "source_verified": True,
        "volume_initial": 0.10, "volume_current": 0.10,
        **overrides,
    }


def test_cancel_pending_refuses_ambiguous_ticket_across_multiple_mt5_accounts():
    first = PendingConnector(live=[_pending()])
    second = PendingConnector(live=[_pending()])
    manager = _manager([first, second])

    assert manager.cancel_pending_order("901", symbol="EURUSD") is False
    assert first.cancelled == []
    assert second.cancelled == []


def test_cancel_pending_requires_source_verified_exact_symbol_and_ticket():
    connector = PendingConnector(live=[_pending(source_verified=False)])
    manager = _manager([connector])

    assert manager.cancel_pending_order("901", symbol="EURUSD") is False
    assert connector.cancelled == []


def test_cancel_pending_routes_to_only_unique_verified_account():
    first = PendingConnector(live=[_pending()])
    second = PendingConnector(live=[])
    manager = _manager([first, second])

    assert manager.cancel_pending_order("901", symbol="EURUSD") is True
    assert first.cancelled == ["901"]
    assert second.cancelled == []


def test_cancel_pending_refuses_when_any_mt5_account_is_disconnected():
    first = PendingConnector(live=[_pending()])
    second = PendingConnector(live=[])
    manager = _manager([first, second])
    manager._mt5_connected_flags = [True, False]

    assert manager.cancel_pending_order("901", symbol="EURUSD") is False
    assert first.cancelled == []
    assert second.cancelled == []


def test_pending_reconciliation_stays_unknown_when_account_set_is_incomplete():
    manager = _manager([
        PendingConnector(live=[_pending()]),
        PendingConnector(live=[], history=[]),
    ])
    manager._mt5_connected_flags = [True, False]
    outcome = manager.reconcile_pending_order(
        "901", symbol="EURUSD",
        submitted_at=datetime.now(timezone.utc),
    )
    assert outcome["status"] == "UNKNOWN"
    assert outcome["reason"] == "mt5_account_set_incomplete"
    assert outcome["broker_confirmed"] is False

def test_pending_reconciliation_duplicate_ticket_across_accounts_is_ambiguous():
    manager = _manager([
        PendingConnector(live=[_pending()]),
        PendingConnector(live=[_pending()]),
    ])
    outcome = manager.reconcile_pending_order(
        "901", symbol="EURUSD",
        submitted_at=datetime.now(timezone.utc),
    )
    assert outcome["status"] == "AMBIGUOUS"
    assert outcome["broker_confirmed"] is False


def test_cancelled_order_with_zero_remainder_uses_deals_to_preserve_partial_fill(monkeypatch):
    from platforms.mt5 import mt5_connector as mt5_adapter

    record = {
        "ticket": "901", "symbol": "EURUSD", "state": 2,
        "volume_initial": 0.10, "volume_current": 0.0,
        "source_verified": True,
    }
    connector = PendingConnector(history=[record])
    connector.deals = [{
        "ticket": "8001", "order": "901", "position_id": "902",
        "symbol": "EURUSD", "entry": 0, "volume": 0.04,
        "source_verified": True,
    }]
    manager = _manager([connector])
    monkeypatch.setattr(mt5_adapter, "mt5", SimpleNamespace(DEAL_ENTRY_IN=0, DEAL_ENTRY_INOUT=2))
    outcome = manager.reconcile_pending_order(
        "901", symbol="EURUSD",
        submitted_at=datetime.now(timezone.utc),
    )
    assert outcome["status"] == "PARTIALLY_FILLED_TERMINAL"
    assert outcome["record"]["position_id"] == "902"
    assert outcome["record"]["filled_volume"] == 0.04


def test_cancelled_order_with_incomplete_deal_history_remains_unknown():
    record = {
        "ticket": "901", "symbol": "EURUSD", "state": 2,
        "volume_initial": 0.10, "volume_current": 0.0,
        "source_verified": True,
    }
    connector = PendingConnector(history=[record])
    connector.deal_fail = True
    manager = _manager([connector])
    outcome = manager.reconcile_pending_order(
        "901", symbol="EURUSD",
        submitted_at=datetime.now(timezone.utc),
    )
    assert outcome["status"] == "UNKNOWN"
    assert outcome["reason"] == "terminal_order_deal_history_incomplete"

def test_pending_reconciliation_maps_verified_cancelled_history_state():
    record = {
        "ticket": "901", "symbol": "EURUSD", "state": 2,
        "volume_initial": 0.10, "volume_current": 0.10,
        "source_verified": True,
    }
    manager = _manager([PendingConnector(history=[record])])
    outcome = manager.reconcile_pending_order(
        "901", symbol="EURUSD",
        submitted_at=datetime.now(timezone.utc),
    )
    assert outcome["status"] == "CANCELLED"
    assert outcome["broker_confirmed"] is True


def test_pending_reconciliation_maps_verified_expired_history_state():
    record = {
        "ticket": "901", "symbol": "EURUSD", "state": 6,
        "volume_initial": 0.10, "volume_current": 0.10,
        "source_verified": True,
    }
    manager = _manager([PendingConnector(history=[record])])
    outcome = manager.reconcile_pending_order(
        "901", symbol="EURUSD",
        submitted_at=datetime.now(timezone.utc),
    )
    assert outcome["status"] == "EXPIRED"
    assert outcome["broker_confirmed"] is True


def test_cancel_pending_fill_race_does_not_report_cancellation():
    connector = PendingConnector(live=[_pending()], cancel_result=False)
    manager = _manager([connector])
    assert manager.cancel_pending_order("901", symbol="EURUSD") is False
    assert connector.cancelled == ["901"]

def test_pending_fill_deal_lookup_requires_exact_order_and_position_identity():
    matching = {
        "order": "901", "position_id": "902", "symbol": "EURUSD",
        "source_verified": True, "entry": 0, "price": 99.0, "volume": 0.04,
    }
    same_position_different_order = {
        "order": "903", "position_id": "902", "symbol": "EURUSD",
        "source_verified": True, "entry": 0, "price": 100.0, "volume": 0.03,
    }
    same_order_different_position = {
        "order": "901", "position_id": "904", "symbol": "EURUSD",
        "source_verified": True, "entry": 0, "price": 101.0, "volume": 0.02,
    }
    unverified = {
        "order": "901", "position_id": "902", "symbol": "EURUSD",
        "source_verified": False, "entry": 0, "price": 102.0, "volume": 0.01,
    }
    connector = SimpleNamespace(get_deal_history_checked=lambda *args: [
        matching, same_position_different_order, same_order_different_position, unverified,
    ])
    manager = object.__new__(PlatformManager)
    manager.mt5_connectors = [connector]

    rows = manager.get_pending_order_fill_deals(
        "901", "902", symbol="EURUSD", platform="mt5_0",
        from_dt=datetime.now(timezone.utc), to_dt=datetime.now(timezone.utc),
    )

    assert rows == [matching]

def test_pending_reconciliation_live_and_history_ticket_collision_is_ambiguous():
    historical = {
        "ticket": "901", "symbol": "EURUSD", "state": 4,
        "volume_initial": 0.10, "volume_current": 0.0,
        "source_verified": True,
    }
    connector = PendingConnector(live=[_pending()], history=[historical])
    manager = _manager([connector])
    outcome = manager.reconcile_pending_order(
        "901", symbol="EURUSD",
        submitted_at=datetime.now(timezone.utc),
    )
    assert outcome["status"] == "AMBIGUOUS"
    assert outcome["broker_confirmed"] is False


def test_pending_reconciliation_rejects_invalid_terminal_volume_evidence():
    record = {
        "ticket": "901", "symbol": "EURUSD", "state": 2,
        "volume_initial": 0.10, "volume_current": 0.20,
        "source_verified": True,
    }
    manager = _manager([PendingConnector(history=[record])])
    outcome = manager.reconcile_pending_order(
        "901", symbol="EURUSD",
        submitted_at=datetime.now(timezone.utc),
    )
    assert outcome["status"] == "UNKNOWN"
    assert outcome["reason"] == "terminal_order_volume_evidence_invalid"

def test_pending_reconciliation_never_infers_cancelled_from_live_book_absence():
    manager = _manager([PendingConnector(live=[], history=[])])
    outcome = manager.reconcile_pending_order(
        "901", symbol="EURUSD",
        submitted_at=datetime.now(timezone.utc),
    )
    assert outcome["status"] == "UNKNOWN"
    assert outcome["broker_confirmed"] is False


def test_pending_reconciliation_requires_complete_account_books():
    manager = _manager([
        PendingConnector(live=[], history=[]),
        PendingConnector(fail=True),
    ])
    outcome = manager.reconcile_pending_order(
        "901", symbol="EURUSD",
        submitted_at=datetime.now(timezone.utc),
    )
    assert outcome["status"] == "UNKNOWN"
    assert outcome["reason"] == "broker_history_or_live_book_incomplete"


def test_pending_reconciliation_maps_verified_filled_history_state():
    record = {
        "ticket": "901", "symbol": "EURUSD", "state": 4,
        "position_id": 902, "source_verified": True,
    }
    manager = _manager([PendingConnector(history=[record])])
    outcome = manager.reconcile_pending_order(
        "901", symbol="EURUSD",
        submitted_at=datetime.now(timezone.utc),
    )
    assert outcome["status"] == "FILLED"
    assert outcome["broker_confirmed"] is True
    assert outcome["record"]["position_id"] == 902


def test_cancelled_history_order_preserves_confirmed_partial_fill_state():
    record = {
        "ticket": "901", "symbol": "EURUSD", "state": 2,
        "volume_initial": 0.10, "volume_current": 0.04,
        "position_id": "902", "source_verified": True,
    }
    manager = _manager([PendingConnector(history=[record])])
    outcome = manager.reconcile_pending_order(
        "901", symbol="EURUSD",
        submitted_at=datetime.now(timezone.utc),
    )
    assert outcome["status"] == "PARTIALLY_FILLED_TERMINAL"
    assert outcome["broker_confirmed"] is True
    assert outcome["record"]["position_id"] == "902"

def test_live_pending_snapshot_reports_partial_fill_without_losing_remaining_order():
    manager = _manager([
        PendingConnector(live=[_pending(volume_initial=0.10, volume_current=0.04)])
    ])
    outcome = manager.reconcile_pending_order(
        "901", symbol="EURUSD",
        submitted_at=datetime.now(timezone.utc),
    )
    assert outcome["status"] == "PARTIALLY_FILLED_PENDING"
    assert outcome["remaining_volume"] == 0.04
    assert outcome["initial_volume"] == 0.10


class IdempotencyConnector(PendingConnector):
    def __init__(self, candidate, live=None, history=None, fail=False):
        super().__init__(live=live, history=history, fail=fail)
        self.candidate = candidate
        self.lookup_calls = []

    def find_order_by_idempotency_key(self, key, *, since=None):
        self.lookup_calls.append((key, since))
        return self.candidate


def test_ambiguous_ack_reconciles_by_idempotency_key_to_exact_pending_ticket():
    candidate = SimpleNamespace(
        ticket="901", symbol="EURUSD", order_status="PENDING",
    )
    connector = IdempotencyConnector(candidate, live=[_pending()])
    manager = _manager([connector])
    outcome = manager.reconcile_pending_order(
        "", symbol="EURUSD", submitted_at=datetime.now(timezone.utc),
        idempotency_key="a1b2c3d4e5f6",
    )
    assert outcome["status"] == "PENDING"
    assert outcome["order_id"] == "901"
    assert connector.lookup_calls[0][0] == "a1b2c3d4e5f6"


def test_ambiguous_ack_idempotency_match_across_accounts_stays_ambiguous():
    candidate = SimpleNamespace(
        ticket="901", symbol="EURUSD", order_status="PENDING",
    )
    manager = _manager([
        IdempotencyConnector(candidate, live=[_pending()]),
        IdempotencyConnector(candidate, live=[_pending()]),
    ])
    outcome = manager.reconcile_pending_order(
        "", symbol="EURUSD", submitted_at=datetime.now(timezone.utc),
        idempotency_key="a1b2c3d4e5f6",
    )
    assert outcome["status"] == "AMBIGUOUS"
    assert outcome["broker_confirmed"] is False


def test_ambiguous_ack_filled_idempotency_match_returns_verified_position_identity():
    candidate = SimpleNamespace(
        ticket="902", symbol="EURUSD", order_status="FILLED", position_id="902",
    )
    manager = _manager([IdempotencyConnector(candidate)])
    outcome = manager.reconcile_pending_order(
        "", symbol="EURUSD", submitted_at=datetime.now(timezone.utc),
        idempotency_key="a1b2c3d4e5f6",
    )
    assert outcome["status"] == "FILLED"
    assert outcome["order_id"] == "902"
    assert outcome["record"]["position_id"] == "902"


def test_ambiguous_pending_submission_is_persisted_and_sent_to_runtime_tracker(monkeypatch):
    from persistence import event_store as event_store_module

    connector = object.__new__(MT5Connector)
    connector.place_pending_order = lambda **kwargs: OrderResult(
        success=False, order_id="", fill_price=0.0,
        requested_price=99.0, slippage_pips=0.0, lots=0.1,
        symbol="EURUSD", direction="BUY", sl=98.0, tp=101.0,
        platform="mt5", error="AMBIGUOUS_PENDING_ORDER_ACK: response lost",
        order_status="UNKNOWN", order_type="LIMIT",
    )
    manager = object.__new__(PlatformManager)
    manager.get_connector = lambda symbol: connector
    manager._broker_write_lock = threading.Lock()
    callback_rows = []
    manager.pending_order_callback = lambda result, context: callback_rows.append((result, context))
    lifecycle_store = SimpleNamespace(is_degraded=False, emit=MagicMock(return_value="event-id"))
    monkeypatch.setattr(event_store_module, "get_existing_event_store", lambda: lifecycle_store)
    monkeypatch.setattr(event_store_module, "get_event_store", lambda: lifecycle_store)

    result = manager.execute_pending_entry(
        symbol="EURUSD", direction="BUY", order_type="LIMIT",
        entry_price=99.0, lots=0.1, sl=98.0, tp=101.0,
        idempotency_key="a1b2c3d4e5f6", decision_id="decision-1",
    )

    assert result.success is False
    assert len(callback_rows) == 1
    assert callback_rows[0][1]["status"] == "UNKNOWN"
    assert callback_rows[0][1]["idempotency_key"] == "a1b2c3d4e5f6"
    assert callback_rows[0][1]["reconciliation_required"] is True
