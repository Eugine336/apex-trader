from datetime import datetime, timezone
import threading
from types import SimpleNamespace

from platforms.platform_manager import PlatformManager


class PendingConnector:
    def __init__(self, live=None, history=None, cancel_result=True, fail=False):
        self.live = list(live or [])
        self.history = list(history or [])
        self.cancel_result = cancel_result
        self.fail = fail
        self.cancelled = []

    def get_pending_order_snapshots(self):
        if self.fail:
            raise RuntimeError("broker order book unavailable")
        return list(self.live)

    def get_order_history(self, start, end):
        if self.fail:
            raise RuntimeError("broker history unavailable")
        return list(self.history)

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
