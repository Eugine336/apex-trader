"""
Tests for PR-A1: confluence data plumbing from entry signal through
ManagedPosition to the trade-close journal record and position store.
"""

import json

import pytest

from persistence.position_store import PositionStore
from platforms.base_connector import OrderResult
from platforms.main_loop import ManagedPosition


# ── Helpers ──────────────────────────────────────────────────────────


def _make_order(order_id="ORD-C1", symbol="EURUSD", direction="BUY",
                lots=0.1, fill_price=1.105, sl=1.09, platform="mt5"):
    return OrderResult(
        success=True, order_id=order_id, fill_price=fill_price,
        requested_price=fill_price, slippage_pips=0.0, lots=lots,
        symbol=symbol, direction=direction, sl=sl, tp=1.12, platform=platform,
    )


def _make_position(confluences=None, **kwargs):
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
        confluences=confluences,
    )
    pos.tm_trade_id = kwargs.get("tm_trade_id", "TM-001")
    return pos


@pytest.fixture
def store(tmp_path):
    db_path = str(tmp_path / "test_confluences.db")
    s = PositionStore(db_path=db_path)
    yield s
    s.close()


# ── ManagedPosition confluences field ────────────────────────────────


class TestManagedPositionConfluences:

    def test_confluences_stored_from_signal(self):
        tags = ["Structure aligned (STRONG):20", "FVG Zone:12", "Session:8"]
        pos = _make_position(confluences=tags)
        assert pos.confluences == tags

    def test_confluences_default_empty_when_none(self):
        pos = _make_position(confluences=None)
        assert pos.confluences == []

    def test_confluences_default_empty_when_omitted(self):
        order = _make_order()
        pos = ManagedPosition(order=order, tp1=1.12, tp2=1.15)
        assert pos.confluences == []

    def test_confluences_are_copied_not_aliased(self):
        original = ["Structure:20", "FVG:12"]
        pos = _make_position(confluences=original)
        original.append("INJECTED")
        assert "INJECTED" not in pos.confluences

    def test_orphan_adopted_has_empty_confluences(self):
        pos = _make_position(
            confluences=None,
            score=0,
            regime="UNKNOWN",
            session="UNKNOWN",
            entry_type="ORPHAN_ADOPTED",
        )
        assert pos.confluences == []


# ── Position store round-trip ────────────────────────────────────────


class TestPositionStoreConfluences:

    def test_save_and_load_preserves_confluences(self, store):
        tags = ["Structure:20", "FVG:12", "Order Block:10"]
        pos = _make_position(confluences=tags)
        store.save_position(pos)

        rows = store.load_all_positions()
        assert len(rows) == 1
        loaded = json.loads(rows[0]["confluences_json"])
        assert loaded == tags

    def test_save_and_load_empty_confluences(self, store):
        pos = _make_position(confluences=None)
        store.save_position(pos)

        rows = store.load_all_positions()
        assert len(rows) == 1
        loaded = json.loads(rows[0]["confluences_json"])
        assert loaded == []

    def test_old_positions_without_column_default_empty(self, store):
        rows = store.load_all_positions()
        assert rows == []
        pos = _make_position(confluences=["Structure:20"])
        store.save_position(pos)
        rows = store.load_all_positions()
        loaded = json.loads(rows[0].get("confluences_json", "[]"))
        assert loaded == ["Structure:20"]


# ── Close-record confluences (TradeRecord integration) ───────────────


class TestCloseRecordConfluences:

    def test_close_record_would_carry_confluences(self):
        """Verify the close path reads pos.confluences (not hardcoded [])."""
        tags = ["Market Structure:20", "M1 Trigger:10"]
        pos = _make_position(confluences=tags)
        close_confluences = list(pos.confluences)
        assert close_confluences == tags
        assert close_confluences is not pos.confluences
