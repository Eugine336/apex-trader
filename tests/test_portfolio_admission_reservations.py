from __future__ import annotations

from types import SimpleNamespace

from persistence.event_store import EventStore
from portfolio.admission_reservations import PortfolioAdmissionReservations


def _payload(reservation_id="idem-1", *, symbol="XAUUSD", direction="LONG"):
    return {
        "reservation_id": reservation_id,
        "account_key": "mt5:account-a",
        "symbol": symbol,
        "direction": direction,
        "risk_pct": 0.01,
        "max_loss": 10.0,
        "decision_id": "decision-1",
        "campaign_id": "campaign-1",
        "opportunity_id": "opportunity-1",
        "broker": "mt5",
    }


def test_reservation_is_durable_across_event_store_reopen(tmp_path):
    database = str(tmp_path / "portfolio-reservations.db")
    store = EventStore(db_path=database)
    reservations = PortfolioAdmissionReservations(lambda: store)

    assert reservations.reserve(_payload())
    assert reservations.snapshot() == [_payload()]

    store.close()
    reopened = EventStore(db_path=database)
    recovered = PortfolioAdmissionReservations(lambda: reopened)
    recovered_snapshot = recovered.snapshot()
    assert len(recovered_snapshot) == 1
    assert {
        key: recovered_snapshot[0][key] for key in _payload()
    } == _payload()

    assert recovered.release("idem-1", reason="explicit_rejection")
    assert recovered.snapshot() == []
    reopened.close()


def test_pending_order_link_and_terminal_release_are_durable(tmp_path):
    database = str(tmp_path / "portfolio-order-link.db")
    store = EventStore(db_path=database)
    reservations = PortfolioAdmissionReservations(lambda: store)

    assert reservations.reserve(_payload())
    assert reservations.link_order("idem-1", "broker-order-1")
    store.close()

    reopened = EventStore(db_path=database)
    recovered = PortfolioAdmissionReservations(lambda: reopened)
    active = recovered.snapshot()
    assert len(active) == 1
    assert active[0]["broker_order_id"] == "broker-order-1"
    assert recovered.release_by_order_id(
        "broker-order-1", reason="broker_confirmed_pending_terminal:cancelled",
    ) == 1
    assert recovered.snapshot() == []
    reopened.close()


def test_reservation_releases_only_after_matching_broker_position(tmp_path):
    database = str(tmp_path / "portfolio-reconcile.db")
    store = EventStore(db_path=database)
    reservations = PortfolioAdmissionReservations(lambda: store)
    assert reservations.reserve(_payload())

    assert reservations.reconcile([
        SimpleNamespace(symbol="EURUSD", direction="LONG"),
    ]) == 0
    assert len(reservations.snapshot()) == 1

    assert reservations.reconcile([
        SimpleNamespace(symbol="XAUUSD", direction="BUY", account_key="mt5:account-a"),
    ]) == 1
    assert reservations.snapshot() == []
    store.close()


def test_position_from_another_account_does_not_release_reservation(tmp_path):
    database = str(tmp_path / "portfolio-account-scope.db")
    store = EventStore(db_path=database)
    reservations = PortfolioAdmissionReservations(lambda: store)
    assert reservations.reserve(_payload())

    assert reservations.reconcile([
        SimpleNamespace(
            symbol="XAUUSD", direction="BUY", account_key="deriv:other",
        ),
    ]) == 0
    assert len(reservations.snapshot()) == 1

    assert reservations.reconcile([
        SimpleNamespace(
            symbol="XAUUSD", direction="BUY", account_key="mt5:account-a",
        ),
    ]) == 1
    assert reservations.snapshot() == []
    store.close()


def test_reservation_fails_closed_without_durable_event_store():
    reservations = PortfolioAdmissionReservations(lambda: None)
    assert reservations.snapshot() == []
    assert reservations.reserve(_payload()) is False


def test_event_driven_compliance_book_includes_durable_pending_reservations(tmp_path):
    from types import SimpleNamespace

    from event_driven_bootstrap import EventDrivenSystem

    database = str(tmp_path / "origination-compliance.db")
    store = EventStore(db_path=database)
    reservations = PortfolioAdmissionReservations(lambda: store)
    assert reservations.reserve(_payload())

    class _Compliance:
        def __init__(self):
            self.book = None

        def permit(self, candidate, book, account):
            self.book = book
            return SimpleNamespace(approved=True)

    compliance = _Compliance()
    system = EventDrivenSystem.__new__(EventDrivenSystem)
    system._ctx = SimpleNamespace(
        compliance=compliance,
        account_key=lambda symbol, platform_manager: "mt5:account-a",
    )
    system._pm = SimpleNamespace(
        get_platform_balance=lambda symbol: 1000.0,
        get_all_open_positions=lambda: [],
    )
    system._portfolio_admission_reservations = reservations

    assert system._compliance_permits("EURUSD", "LONG") is True
    assert len(compliance.book.open_positions) == 1
    pending = compliance.book.open_positions[0]
    assert pending.is_pending_reservation is True
    assert pending.symbol == "XAUUSD"
    assert pending.direction == "LONG"
    assert pending.account_key == "mt5:account-a"
    assert pending.reserved_risk_pct == 0.01
    assert pending.reserved_max_loss == 10.0
    store.close()


def test_pending_open_reservation_releases_only_after_broker_confirmed_cancel(tmp_path, monkeypatch):
    import threading
    from datetime import datetime, timezone
    from types import SimpleNamespace

    import event_driven_bootstrap as bootstrap_module
    from event_driven_bootstrap import EventDrivenSystem
    from persistence import event_store as event_store_module

    database = str(tmp_path / "pending-order-reservation.db")
    store = EventStore(db_path=database)
    monkeypatch.setattr(bootstrap_module, "get_event_store", lambda: store)
    monkeypatch.setattr(event_store_module, "get_event_store", lambda: store)
    reservations = PortfolioAdmissionReservations(lambda: store)
    assert reservations.reserve(_payload())

    system = EventDrivenSystem.__new__(EventDrivenSystem)
    system._pending_orders_lock = threading.Lock()
    system._pending_orders = {}
    system._portfolio_admission_reservations = reservations
    submitted_at = datetime.now(timezone.utc)
    intent = SimpleNamespace(
        symbol="XAUUSD",
        direction="BUY",
        order_type="LIMIT",
        lots=0.10,
        entry_price=2300.0,
        new_sl=2290.0,
        new_tp=2320.0,
        idempotency_key="idem-1",
        decision_id="decision-1",
        campaign_id="campaign-1",
        opportunity_id="opportunity-1",
        decision_feature_snapshot={"snapshot_id": "snapshot-1"},
        timestamp=submitted_at,
    )
    result = SimpleNamespace(
        success=True,
        broker_response=SimpleNamespace(
            order_id="BROKER-ORDER-1",
            order_status="PENDING",
            timestamp=submitted_at,
            platform="mt5",
            lots=0.10,
        ),
    )
    system._handle_open_result(intent, result)
    assert reservations.snapshot()[0]["broker_order_id"] == "BROKER-ORDER-1"

    system._pm = SimpleNamespace(
        reconcile_pending_order=lambda *args, **kwargs: {
            "status": "CANCELLED",
            "broker_confirmed": True,
            "record": {
                "ticket": "BROKER-ORDER-1",
                "state": 2,
                "volume_initial": 0.10,
                "volume_current": 0.10,
            },
        },
    )
    system._reconcile_pending_orders([])

    assert system._pending_orders == {}
    assert reservations.snapshot() == []
    store.close()
