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
    assert recovered.snapshot() == [_payload()]

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
        SimpleNamespace(symbol="XAUUSD", direction="BUY"),
    ]) == 1
    assert reservations.snapshot() == []
    store.close()


def test_reservation_fails_closed_without_durable_event_store():
    reservations = PortfolioAdmissionReservations(lambda: None)
    assert reservations.snapshot() == []
    assert reservations.reserve(_payload()) is False
