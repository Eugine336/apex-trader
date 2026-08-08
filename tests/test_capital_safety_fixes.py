"""
Regression tests for MT5 idempotency dedup history scan + normalisation.

Fixtures use real MT5 object field names:
  - TradeDeal  : ticket, price, volume, profit, symbol, comment  (NO price_open/sl/tp)
  - TradeOrder : ticket, price_open, sl, tp, volume_initial, volume_current, symbol, comment  (NO volume)
  - Position   : ticket, price_open, volume, sl, tp, symbol, comment
"""

import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

# Provide a fake MetaTrader5 module so the connector imports cleanly.
_fake_mt5 = MagicMock()
_fake_mt5.ORDER_TYPE_BUY = 0
_fake_mt5.ORDER_TYPE_SELL = 1
_fake_mt5.TRADE_ACTION_DEAL = 1
_fake_mt5.last_error.return_value = (0, "ok")
sys.modules.setdefault("MetaTrader5", _fake_mt5)

from platforms.mt5.mt5_connector import MT5Connector  # noqa: E402


# ── realistic MT5 object factories ───────────────────────────────────

def _mt5_position(ticket=12345, price_open=1.10500, volume=0.05,
                  sl=1.10000, tp=1.11500, symbol="EURUSD",
                  comment="APEX|abc12|BUY"):
    return SimpleNamespace(
        ticket=ticket, price_open=price_open, volume=volume,
        sl=sl, tp=tp, symbol=symbol, comment=comment,
        type=0, price_current=1.10600, profit=5.0, swap=0.0,
        time=1717800000,
    )


def _mt5_pending_order(ticket=22222, price_open=1.12000,
                       volume_initial=0.10, volume_current=0.10,
                       sl=1.11000, tp=1.14000, symbol="EURUSD",
                       comment="APEX|abc12|BUY"):
    return SimpleNamespace(
        ticket=ticket, price_open=price_open,
        volume_initial=volume_initial, volume_current=volume_current,
        sl=sl, tp=tp, symbol=symbol, comment=comment,
    )


def _mt5_deal(ticket=33333, price=1.10450, volume=0.05,
              profit=12.5, symbol="EURUSD",
              comment="APEX|abc12|BUY"):
    """Real TradeDeal — NO price_open, sl, tp."""
    return SimpleNamespace(
        ticket=ticket, price=price, volume=volume,
        profit=profit, symbol=symbol, comment=comment,
        time=1717800100, fee=0.0, swap=0.0,
    )


def _mt5_history_order(ticket=44444, price_open=1.10500,
                       volume_initial=0.05, volume_current=0.05,
                       sl=1.10000, tp=1.11500, symbol="EURUSD",
                       comment="APEX|abc12|BUY"):
    """Real TradeOrder from history — NO .volume attribute."""
    return SimpleNamespace(
        ticket=ticket, price_open=price_open,
        volume_initial=volume_initial, volume_current=volume_current,
        sl=sl, tp=tp, symbol=symbol, comment=comment,
        time_setup=1717800000, time_done=1717800050,
    )


# ── helpers ──────────────────────────────────────────────────────────

@pytest.fixture
def connector():
    mock_mt5 = MagicMock()
    mock_mt5.ORDER_TYPE_BUY = 0
    mock_mt5.ORDER_TYPE_SELL = 1
    mock_mt5.TRADE_ACTION_DEAL = 1
    mock_mt5.last_error.return_value = (0, "ok")
    with patch("platforms.mt5.mt5_connector.mt5", mock_mt5):
        c = MT5Connector.__new__(MT5Connector)
        c.connected = True
        c._connection_time = None
        c._mapper = MagicMock()
        c._mapper.to_broker.return_value = "EURUSD"
        c._symbol_cache = {}
        yield c, mock_mt5


IDEM_KEY = "abc12"


# ── _find_order_by_idem_key normalisation ────────────────────────────

class TestFindOrderByIdemKeyNormalisation:

    def test_open_position_normalised(self, connector):
        c, mock_mt5 = connector
        mock_mt5.positions_get.return_value = [_mt5_position()]
        mock_mt5.orders_get.return_value = None

        result = c._find_order_by_idem_key(IDEM_KEY)

        assert result is not None
        assert result.ticket == 12345
        assert result.price == 1.10500
        assert result.volume == 0.05
        assert result.sl == 1.10000
        assert result.tp == 1.11500

    def test_pending_order_normalised(self, connector):
        c, mock_mt5 = connector
        mock_mt5.positions_get.return_value = None
        mock_mt5.orders_get.return_value = [_mt5_pending_order()]

        result = c._find_order_by_idem_key(IDEM_KEY)

        assert result is not None
        assert result.ticket == 22222
        assert result.price == 1.12000
        assert result.volume == 0.10
        assert result.sl == 1.11000
        assert result.tp == 1.14000

    def test_history_deal_normalised_no_sl_tp(self, connector):
        c, mock_mt5 = connector
        mock_mt5.positions_get.return_value = None
        mock_mt5.orders_get.return_value = None
        mock_mt5.history_deals_get.return_value = [_mt5_deal()]
        mock_mt5.history_orders_get.return_value = None

        result = c._find_order_by_idem_key(IDEM_KEY)

        assert result is not None
        assert result.ticket == 33333
        assert result.price == 1.10450
        assert result.volume == 0.05
        assert result.sl == 0.0, "deals carry no SL"
        assert result.tp == 0.0, "deals carry no TP"

    def test_history_order_normalised_volume_current(self, connector):
        c, mock_mt5 = connector
        mock_mt5.positions_get.return_value = None
        mock_mt5.orders_get.return_value = None
        mock_mt5.history_deals_get.return_value = None
        mock_mt5.history_orders_get.return_value = [_mt5_history_order()]

        result = c._find_order_by_idem_key(IDEM_KEY)

        assert result is not None
        assert result.ticket == 44444
        assert result.price == 1.10500
        assert result.volume == 0.05
        assert result.sl == 1.10000
        assert result.tp == 1.11500

    def test_no_match_returns_none(self, connector):
        c, mock_mt5 = connector
        mock_mt5.positions_get.return_value = None
        mock_mt5.orders_get.return_value = None
        mock_mt5.history_deals_get.return_value = None
        mock_mt5.history_orders_get.return_value = None

        assert c._find_order_by_idem_key(IDEM_KEY) is None

    def test_history_deals_exception_non_fatal(self, connector):
        c, mock_mt5 = connector
        mock_mt5.positions_get.return_value = None
        mock_mt5.orders_get.return_value = None
        mock_mt5.history_deals_get.side_effect = RuntimeError("broker down")
        mock_mt5.history_orders_get.return_value = [_mt5_history_order()]

        result = c._find_order_by_idem_key(IDEM_KEY)
        assert result is not None
        assert result.ticket == 44444

    def test_history_orders_exception_non_fatal(self, connector):
        c, mock_mt5 = connector
        mock_mt5.positions_get.return_value = None
        mock_mt5.orders_get.return_value = None
        mock_mt5.history_deals_get.return_value = None
        mock_mt5.history_orders_get.side_effect = RuntimeError("broker down")

        assert c._find_order_by_idem_key(IDEM_KEY) is None


# ── real MT5 shapes must not raise AttributeError ────────────────────

class TestNoAttributeErrorOnRealShapes:
    """The exact bug this fix addresses: raw deal/order shapes must
    never raise AttributeError inside the dedup consumer."""

    def test_deal_shape_has_no_price_open(self):
        deal = _mt5_deal()
        assert not hasattr(deal, "price_open"), "real deals have no price_open"
        assert not hasattr(deal, "sl"), "real deals have no sl"
        assert not hasattr(deal, "tp"), "real deals have no tp"

    def test_history_order_shape_has_no_volume(self):
        ho = _mt5_history_order()
        assert not hasattr(ho, "volume"), "real history orders have no .volume"

    def test_deal_dedup_does_not_raise(self, connector):
        """Simulate the exact lost-ack scenario: order filled then closed,
        matched in history_deals_get. Must produce a valid OrderResult."""
        c, mock_mt5 = connector
        mock_mt5.positions_get.return_value = None
        mock_mt5.orders_get.return_value = None
        mock_mt5.history_deals_get.return_value = [_mt5_deal()]
        mock_mt5.history_orders_get.return_value = None

        result = c._find_order_by_idem_key(IDEM_KEY)
        assert result is not None
        assert result.price == 1.10450
        assert result.sl == 0.0
        assert result.tp == 0.0

    def test_history_order_dedup_does_not_raise(self, connector):
        c, mock_mt5 = connector
        mock_mt5.positions_get.return_value = None
        mock_mt5.orders_get.return_value = None
        mock_mt5.history_deals_get.return_value = None
        mock_mt5.history_orders_get.return_value = [_mt5_history_order()]

        result = c._find_order_by_idem_key(IDEM_KEY)
        assert result is not None
        assert result.volume == 0.05


# ── place_order consumer integration ─────────────────────────────────

class TestPlaceOrderDedupConsumer:

    def _run_place_order_with_dup(self, connector, dup_source):
        c, mock_mt5 = connector
        mock_mt5.positions_get.return_value = None
        mock_mt5.orders_get.return_value = None
        mock_mt5.history_deals_get.return_value = None
        mock_mt5.history_orders_get.return_value = None

        if dup_source == "position":
            mock_mt5.positions_get.return_value = [_mt5_position()]
        elif dup_source == "deal":
            mock_mt5.history_deals_get.return_value = [_mt5_deal()]
        elif dup_source == "history_order":
            mock_mt5.history_orders_get.return_value = [_mt5_history_order()]

        c._require_connection = MagicMock()
        c.symbol_map = MagicMock(return_value="EURUSD")

        result = c.place_order(
            symbol="EURUSD", direction="BUY", lots=0.05,
            sl=1.10000, tp=1.11500, comment="APEX",
            idempotency_key=IDEM_KEY,
        )
        return result

    def test_position_dup_returns_success(self, connector):
        r = self._run_place_order_with_dup(connector, "position")
        assert r.success is True
        assert r.order_id == "12345"
        assert r.fill_price == 1.10500
        assert r.lots == 0.05
        assert r.sl == 1.10000
        assert r.tp == 1.11500

    def test_deal_dup_returns_success_zero_sl_tp(self, connector):
        r = self._run_place_order_with_dup(connector, "deal")
        assert r.success is True
        assert r.order_id == "33333"
        assert r.fill_price == 1.10450
        assert r.lots == 0.05
        assert r.sl == 0.0
        assert r.tp == 0.0

    def test_history_order_dup_returns_success(self, connector):
        r = self._run_place_order_with_dup(connector, "history_order")
        assert r.success is True
        assert r.order_id == "44444"
        assert r.fill_price == 1.10500
        assert r.lots == 0.05
        assert r.sl == 1.10000
        assert r.tp == 1.11500

    def test_no_dup_does_not_short_circuit(self, connector):
        c, mock_mt5 = connector
        mock_mt5.positions_get.return_value = None
        mock_mt5.orders_get.return_value = None
        mock_mt5.history_deals_get.return_value = None
        mock_mt5.history_orders_get.return_value = None
        mock_mt5.symbol_info_tick.return_value = None
        c._require_connection = MagicMock()
        c.symbol_map = MagicMock(return_value="EURUSD")
        c._fail_order = MagicMock(return_value=SimpleNamespace(success=False))

        r = c.place_order(
            symbol="EURUSD", direction="BUY", lots=0.05,
            sl=1.10000, tp=1.11500, idempotency_key=IDEM_KEY,
        )
        assert r.success is False
