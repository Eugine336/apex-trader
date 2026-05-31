"""
APEX TRADER — Position Store Tests
Verifies SQLite persistence: save, load, update, remove, and restart recovery.
"""

import os
import tempfile
from datetime import datetime, timezone

import pytest

from persistence.position_store import PositionStore
from platforms.base_connector import OrderResult
from platforms.main_loop import ManagedPosition


@pytest.fixture
def store(tmp_path):
    db_path = str(tmp_path / "test_positions.db")
    s = PositionStore(db_path=db_path)
    yield s
    s.close()


def _make_order(order_id="ORD-1", symbol="EURUSD", direction="BUY",
                lots=0.1, fill_price=1.105, sl=1.09, platform="mt5"):
    return OrderResult(
        success=True, order_id=order_id, fill_price=fill_price,
        requested_price=fill_price, slippage_pips=0.0, lots=lots,
        symbol=symbol, direction=direction, sl=sl, tp=1.12, platform=platform,
    )


def _make_position(**kwargs):
    order = _make_order(**{k: v for k, v in kwargs.items()
                           if k in ("order_id", "symbol", "direction", "lots",
                                    "fill_price", "sl", "platform")})
    pos = ManagedPosition(
        order=order,
        tp1=kwargs.get("tp1", 1.12),
        tp2=kwargs.get("tp2", 1.15),
        score=kwargs.get("score", 92),
        regime=kwargs.get("regime", "TRENDING"),
        session=kwargs.get("session", "LONDON"),
        entry_type=kwargs.get("entry_type", "FVG_OB"),
        stake_usd=kwargs.get("stake_usd", 0.0),
        multiplier=kwargs.get("multiplier", 100),
    )
    pos.tm_trade_id = kwargs.get("tm_trade_id", "TM-001")
    return pos


class TestPositionStoreSaveAndLoad:

    def test_save_and_load(self, store):
        pos = _make_position()
        store.save_position(pos)
        rows = store.load_all_positions()
        assert len(rows) == 1
        row = rows[0]
        assert row["order_id"] == "ORD-1"
        assert row["symbol"] == "EURUSD"
        assert row["direction"] == "BUY"
        assert row["lots"] == 0.1
        assert row["score"] == 92
        assert row["regime"] == "TRENDING"
        assert row["session"] == "LONDON"
        assert row["entry_type"] == "FVG_OB"
        assert row["tp1_hit"] == 0
        assert row["at_breakeven"] == 0
        assert row["trailing"] == 0
        assert row["tm_trade_id"] == "TM-001"

    def test_save_multiple(self, store):
        store.save_position(_make_position(order_id="A"))
        store.save_position(_make_position(order_id="B"))
        store.save_position(_make_position(order_id="C"))
        assert store.count() == 3

    def test_save_replaces_on_conflict(self, store):
        pos = _make_position(order_id="X", score=80)
        store.save_position(pos)
        pos2 = _make_position(order_id="X", score=95)
        store.save_position(pos2)
        rows = store.load_all_positions()
        assert len(rows) == 1
        assert rows[0]["score"] == 95

    def test_load_empty(self, store):
        assert store.load_all_positions() == []
        assert store.count() == 0


class TestPositionStoreUpdate:

    def test_update_sl(self, store):
        store.save_position(_make_position())
        store.update_position("ORD-1", sl=1.095)
        row = store.load_all_positions()[0]
        assert abs(row["sl"] - 1.095) < 1e-8

    def test_update_tp1_hit(self, store):
        store.save_position(_make_position())
        store.update_position("ORD-1", tp1_hit=True, at_breakeven=True)
        row = store.load_all_positions()[0]
        assert row["tp1_hit"] == 1
        assert row["at_breakeven"] == 1

    def test_update_lots_after_partial(self, store):
        store.save_position(_make_position(lots=0.1))
        store.update_position("ORD-1", lots=0.05, tp1_hit=True)
        row = store.load_all_positions()[0]
        assert row["lots"] == 0.05
        assert row["tp1_hit"] == 1

    def test_update_nonexistent_is_noop(self, store):
        store.update_position("GHOST", sl=9.99)
        assert store.count() == 0

    def test_update_disallowed_field_ignored(self, store):
        store.save_position(_make_position())
        store.update_position("ORD-1", symbol="HACKED")
        row = store.load_all_positions()[0]
        assert row["symbol"] == "EURUSD"


class TestPositionStoreRemove:

    def test_remove(self, store):
        store.save_position(_make_position(order_id="DEL"))
        assert store.count() == 1
        store.remove_position("DEL")
        assert store.count() == 0

    def test_remove_nonexistent_is_noop(self, store):
        store.remove_position("NOTHING")
        assert store.count() == 0


class TestPositionStoreMultiplier:

    def test_multiplier_persisted(self, store):
        pos = _make_position(multiplier=1000)
        store.save_position(pos)
        row = store.load_all_positions()[0]
        assert row["multiplier"] == 1000

    def test_multiplier_default(self, store):
        pos = _make_position()
        store.save_position(pos)
        row = store.load_all_positions()[0]
        assert row["multiplier"] == 100


class TestPositionStoreRestartRecovery:

    def test_recovery_across_connections(self, tmp_path):
        db_path = str(tmp_path / "restart.db")

        store1 = PositionStore(db_path=db_path)
        store1.save_position(_make_position(order_id="R1", symbol="GBPUSD"))
        store1.save_position(_make_position(order_id="R2", symbol="V75_1S",
                                            platform="deriv", stake_usd=5.0,
                                            multiplier=1000))
        store1.close()

        store2 = PositionStore(db_path=db_path)
        rows = store2.load_all_positions()
        assert len(rows) == 2
        by_id = {r["order_id"]: r for r in rows}
        assert by_id["R1"]["symbol"] == "GBPUSD"
        assert by_id["R2"]["symbol"] == "V75_1S"
        assert by_id["R2"]["stake_usd"] == 5.0
        assert by_id["R2"]["multiplier"] == 1000
        store2.close()

    def test_flush_and_reopen(self, tmp_path):
        db_path = str(tmp_path / "flush.db")
        store = PositionStore(db_path=db_path)
        store.save_position(_make_position(order_id="F1"))
        store.flush()
        store.close()

        store2 = PositionStore(db_path=db_path)
        assert store2.count() == 1
        store2.close()
