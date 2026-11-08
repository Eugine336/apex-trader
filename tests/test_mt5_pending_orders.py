from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

import platforms.mt5.mt5_connector as mt5_module
from platforms.mt5.mt5_connector import MT5Connector


class FakeMT5:
    ORDER_TYPE_BUY = 0
    ORDER_TYPE_SELL = 1
    ORDER_TYPE_BUY_LIMIT = 2
    ORDER_TYPE_SELL_LIMIT = 3
    ORDER_TYPE_BUY_STOP = 4
    ORDER_TYPE_SELL_STOP = 5
    TRADE_ACTION_PENDING = 5
    TRADE_ACTION_REMOVE = 8
    ORDER_TIME_GTC = 0
    ORDER_FILLING_RETURN = 2
    TRADE_RETCODE_PLACED = 10008
    TRADE_RETCODE_DONE = 10009
    SYMBOL_TRADE_MODE_DISABLED = 0
    ORDER_STATE_CANCELED = 2
    ORDER_STATE_PARTIAL = 3
    ORDER_STATE_FILLED = 4
    ORDER_STATE_REJECTED = 5
    ORDER_STATE_EXPIRED = 6

    def __init__(self):
        self.orders = []
        self.requests = []
        self.retcode = self.TRADE_RETCODE_PLACED
        self.omit_confirmation = False
        self.lookup_unavailable = False
        self.history_orders = []
        self.history_deals = []
        self.history_unavailable = False

    @staticmethod
    def symbol_select(symbol, enabled):
        return True

    @staticmethod
    def symbol_info(symbol):
        return SimpleNamespace(
            volume_min=0.01, volume_max=10.0, volume_step=0.01,
            point=0.01, digits=2, trade_mode=4,
            trade_stops_level=0, trade_freeze_level=0,
        )

    @staticmethod
    def symbol_info_tick(symbol):
        return SimpleNamespace(
            bid=100.0, ask=100.02,
            time=int(datetime.now(timezone.utc).timestamp()),
        )

    @staticmethod
    def positions_get(*args, **kwargs):
        return []

    def orders_get(self, *args, **kwargs):
        if self.lookup_unavailable:
            return None
        return [] if self.omit_confirmation else list(self.orders)

    def history_orders_get(self, *args, **kwargs):
        return None if self.history_unavailable else list(self.history_orders)

    def history_deals_get(self, *args, **kwargs):
        return None if self.history_unavailable else list(self.history_deals)

    def order_send(self, request):
        self.requests.append(dict(request))
        if request["action"] == self.TRADE_ACTION_REMOVE:
            self.orders = [o for o in self.orders if o.ticket != request["order"]]
            return SimpleNamespace(retcode=self.TRADE_RETCODE_DONE, comment="removed")
        order = SimpleNamespace(
            ticket=901, symbol=request["symbol"],
            volume_current=request["volume"], volume_initial=request["volume"],
            price_open=request["price"], sl=request["sl"], tp=request["tp"],
            type=request["type"], comment=request["comment"], state=1,
        )
        self.orders.append(order)
        return SimpleNamespace(
            retcode=self.retcode, order=order.ticket,
            price=request["price"], comment="placed",
        )

    @staticmethod
    def last_error():
        return (0, "ok")


def _connector(monkeypatch):
    fake = FakeMT5()
    monkeypatch.setattr(mt5_module, "mt5", fake)
    connector = MT5Connector(max_tick_age_seconds=10)
    connector._connected = True
    connector.symbol_map = lambda symbol: symbol
    connector._deviation_points = lambda symbol, point: 10
    return connector, fake


def test_buy_limit_is_submitted_as_pending_and_verified_in_broker_book(monkeypatch):
    connector, fake = _connector(monkeypatch)

    result = connector.place_pending_order(
        symbol="EURUSD", direction="BUY", order_type="LIMIT",
        entry_price=99.0, lots=0.10, sl=98.0, tp=101.0,
        idempotency_key="a1b2c3d4e5f1",
    )

    assert result.success is True
    assert result.order_status == "PENDING"
    assert result.order_type == "LIMIT"
    assert result.order_id == "901"
    assert fake.requests[0]["action"] == fake.TRADE_ACTION_PENDING
    assert fake.requests[0]["type"] == fake.ORDER_TYPE_BUY_LIMIT
    assert fake.requests[0]["type_filling"] == fake.ORDER_FILLING_RETURN
    assert fake.requests[0]["comment"].startswith("APND-a1b2c3d4e5f1")


@pytest.mark.parametrize(
    ("direction", "kind", "entry", "expected_error"),
    [
        ("BUY", "LIMIT", 100.03, "buy_limit_must_be_below_ask"),
        ("BUY", "STOP", 100.01, "buy_stop_must_be_above_ask"),
        ("SELL", "LIMIT", 99.99, "sell_limit_must_be_above_bid"),
        ("SELL", "STOP", 100.01, "sell_stop_must_be_below_bid"),
    ],
)
def test_pending_order_wrong_side_of_quote_is_rejected_without_send(
    monkeypatch, direction, kind, entry, expected_error,
):
    connector, fake = _connector(monkeypatch)
    stop, target = (entry - 1.0, entry + 1.0) if direction == "BUY" else (entry + 1.0, entry - 1.0)

    result = connector.place_pending_order(
        symbol="EURUSD", direction=direction, order_type=kind,
        entry_price=entry, lots=0.10, sl=stop, tp=target,
    )

    assert result.success is False
    assert expected_error in result.error
    assert fake.requests == []


def test_pending_order_never_inflates_or_rounds_owner_sized_volume(monkeypatch):
    connector, fake = _connector(monkeypatch)
    result = connector.place_pending_order(
        symbol="EURUSD", direction="BUY", order_type="LIMIT",
        entry_price=99.0, lots=0.105, sl=98.0, tp=101.0,
    )
    assert result.success is False
    assert "volume_not_exactly_broker_aligned" in result.error
    assert fake.requests == []


def test_accepted_but_unobserved_pending_order_is_ambiguous_and_not_retryable(monkeypatch):
    connector, fake = _connector(monkeypatch)
    fake.omit_confirmation = True

    result = connector.place_pending_order(
        symbol="EURUSD", direction="BUY", order_type="LIMIT",
        entry_price=99.0, lots=0.10, sl=98.0, tp=101.0,
    )

    assert result.success is False
    assert result.order_status == "UNKNOWN"
    assert result.error.startswith("AMBIGUOUS_PENDING_ORDER_ACK:")
    assert len(fake.requests) == 1


def test_pending_idempotency_retry_returns_existing_order_without_second_send(monkeypatch):
    connector, fake = _connector(monkeypatch)
    first = connector.place_pending_order(
        symbol="EURUSD", direction="BUY", order_type="LIMIT",
        entry_price=99.0, lots=0.10, sl=98.0, tp=101.0,
        idempotency_key="a1b2c3d4e5f6",
    )
    second = connector.place_pending_order(
        symbol="EURUSD", direction="BUY", order_type="LIMIT",
        entry_price=99.0, lots=0.10, sl=98.0, tp=101.0,
        idempotency_key="a1b2c3d4e5f6",
    )
    assert first.success and second.success
    assert second.order_status == "PENDING"
    assert second.order_id == first.order_id
    assert len(fake.requests) == 1


def test_partial_pending_idempotency_retry_returns_same_order_without_resubmission(monkeypatch):
    connector, fake = _connector(monkeypatch)
    first = connector.place_pending_order(
        symbol="EURUSD", direction="BUY", order_type="LIMIT",
        entry_price=99.0, lots=0.10, sl=98.0, tp=101.0,
        idempotency_key="a1b2c3d4e5f2",
    )
    fake.orders[0].volume_current = 0.04

    second = connector.place_pending_order(
        symbol="EURUSD", direction="BUY", order_type="LIMIT",
        entry_price=99.0, lots=0.10, sl=98.0, tp=101.0,
        idempotency_key="a1b2c3d4e5f2",
    )

    assert first.success is True
    assert second.success is True
    assert second.order_status == "PARTIALLY_FILLED_PENDING"
    assert second.order_id == first.order_id
    assert len(fake.requests) == 1

def test_pending_idempotency_history_fill_keeps_order_and_position_tickets_distinct(monkeypatch):
    connector, fake = _connector(monkeypatch)
    fake.history_orders = [SimpleNamespace(
        ticket=7001, symbol="EURUSD", state=fake.ORDER_STATE_FILLED,
        volume_initial=0.10, volume_current=0.0, price_open=99.0,
        sl=98.0, tp=101.0, position_id=7002, comment="APND-a1b2c3d4e5f4",
    )]

    found = connector.find_pending_order_by_idempotency_key("a1b2c3d4e5f4")

    assert found.order_status == "FILLED"
    assert str(found.ticket) == "7001"
    assert found.position_id == "7002"


def test_pending_idempotency_cancelled_history_preserves_partial_fill(monkeypatch):
    connector, fake = _connector(monkeypatch)
    fake.history_orders = [SimpleNamespace(
        ticket=7003, symbol="EURUSD", state=fake.ORDER_STATE_CANCELED,
        volume_initial=0.10, volume_current=0.06, price_open=99.0,
        sl=98.0, tp=101.0, position_id=7004, comment="APND-a1b2c3d4e5f5",
    )]

    found = connector.find_pending_order_by_idempotency_key("a1b2c3d4e5f5")

    assert found.order_status == "PARTIALLY_FILLED_TERMINAL"
    assert str(found.ticket) == "7003"
    assert found.position_id == "7004"


def test_pending_idempotency_deal_only_evidence_is_unknown_not_filled(monkeypatch):
    connector, fake = _connector(monkeypatch)
    fake.history_deals = [SimpleNamespace(
        order=7005, position_id=7006, symbol="EURUSD",
        comment="APND-a1b2c3d4e5f7", price=99.0, volume=0.04,
    )]

    found = connector.find_pending_order_by_idempotency_key("a1b2c3d4e5f7")

    assert found.order_status == "UNKNOWN"
    assert str(found.ticket) == "7005"
    assert found.position_id == "7006"


def test_pending_idempotency_history_lookup_failure_stays_unknown(monkeypatch):
    connector, fake = _connector(monkeypatch)
    fake.history_unavailable = True

    found = connector.find_pending_order_by_idempotency_key("a1b2c3d4e5f8")

    assert found.order_status == "UNKNOWN"

def test_pending_retry_stays_unknown_when_broker_order_book_lookup_fails(monkeypatch):
    connector, fake = _connector(monkeypatch)
    first = connector.place_pending_order(
        symbol="EURUSD", direction="BUY", order_type="LIMIT",
        entry_price=99.0, lots=0.10, sl=98.0, tp=101.0,
        idempotency_key="a1b2c3d4e5f3",
    )
    fake.lookup_unavailable = True

    second = connector.place_pending_order(
        symbol="EURUSD", direction="BUY", order_type="LIMIT",
        entry_price=99.0, lots=0.10, sl=98.0, tp=101.0,
        idempotency_key="a1b2c3d4e5f3",
    )

    assert first.success is True
    assert second.success is False
    assert second.order_status == "UNKNOWN"
    assert second.error.startswith("AMBIGUOUS_PENDING_ORDER_ACK:")
    assert len(fake.requests) == 1

def test_cancel_pending_order_removes_only_the_named_ticket(monkeypatch):
    connector, fake = _connector(monkeypatch)
    fake.orders = [SimpleNamespace(ticket=901, symbol="EURUSD")]
    assert connector.cancel_pending_order("901") is True
    assert fake.orders == []
    assert fake.requests[-1] == {"action": fake.TRADE_ACTION_REMOVE, "order": 901}


def test_pending_order_rejects_prices_not_aligned_to_broker_tick_size(monkeypatch):
    connector, fake = _connector(monkeypatch)
    result = connector.place_pending_order(
        symbol="EURUSD", direction="BUY", order_type="LIMIT",
        entry_price=99.005, lots=0.10, sl=98.005, tp=101.005,
    )
    assert result.success is False
    assert result.error == "pending_order_price_not_tick_aligned"
    assert fake.requests == []


def test_pending_order_respects_symbol_direction_trade_mode(monkeypatch):
    connector, fake = _connector(monkeypatch)
    fake.symbol_info = lambda symbol: SimpleNamespace(
        volume_min=0.01, volume_max=10.0, volume_step=0.01,
        point=0.01, trade_tick_size=0.01, digits=2,
        trade_mode=2, trade_stops_level=0, trade_freeze_level=0,
    )
    result = connector.place_pending_order(
        symbol="EURUSD", direction="BUY", order_type="LIMIT",
        entry_price=99.0, lots=0.10, sl=98.0, tp=101.0,
    )
    assert result.success is False
    assert result.error == "pending_order_symbol_constraints_invalid"
    assert fake.requests == []


def test_pending_order_rejects_unrepresentable_idempotency_key(monkeypatch):
    connector, fake = _connector(monkeypatch)
    result = connector.place_pending_order(
        symbol="EURUSD", direction="BUY", order_type="LIMIT",
        entry_price=99.0, lots=0.10, sl=98.0, tp=101.0,
        idempotency_key="not-a-valid-key-length",
    )
    assert result.success is False
    assert result.error == "pending_order_idempotency_key_must_be_12_hex_characters"
    assert fake.requests == []
