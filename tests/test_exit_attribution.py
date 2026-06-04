"""
Tests for Phase 3 — True Exit Attribution.
Verifies MT5 deal-reason mapping, TradeManager attribution,
Deriv unknown-marking, reconciliation, and TRADE_CLOSE event emission.
"""

import json
import os
import tempfile
import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from platforms.exit_attribution import (
    ExitAttribution,
    attribute_from_mt5_deals,
    attribute_from_trade_manager,
    attribute_unknown,
    reconcile,
)
from persistence.domain_events import TRADE_CLOSE


# ── MT5 deal reason mapping ─────────────────────────────────────────────────


class TestMT5DealReasonMapping:
    """Verify every MT5 DEAL_REASON enum maps correctly."""

    def _make_deal(self, reason, comment=None, price=1.2345, time_val=1717500000, entry=1):
        return SimpleNamespace(
            reason=reason,
            comment=comment,
            price=price,
            time=time_val,
            entry=entry,
            profit=10.0,
            commission=-0.5,
            swap=0.0,
            fee=0.0,
            type=1,
        )

    def test_deal_reason_sl(self):
        deals = [self._make_deal(reason=4, comment="sl")]
        attr = attribute_from_mt5_deals(deals)
        assert attr.exit_reason == "SL"
        assert attr.exit_reason_source == "mt5_deal"
        assert attr.raw_broker_reason == 4

    def test_deal_reason_tp(self):
        deals = [self._make_deal(reason=5, comment="tp")]
        attr = attribute_from_mt5_deals(deals)
        assert attr.exit_reason == "TP"
        assert attr.exit_reason_source == "mt5_deal"
        assert attr.raw_broker_reason == 5

    def test_deal_reason_so_stop_out(self):
        deals = [self._make_deal(reason=6)]
        attr = attribute_from_mt5_deals(deals)
        assert attr.exit_reason == "STOP_OUT"
        assert attr.exit_reason_source == "mt5_deal"
        assert attr.raw_broker_reason == 6

    def test_deal_reason_client_manual(self):
        deals = [self._make_deal(reason=0)]
        attr = attribute_from_mt5_deals(deals)
        assert attr.exit_reason == "MANUAL"
        assert attr.exit_reason_source == "mt5_deal"

    def test_deal_reason_mobile_manual(self):
        deals = [self._make_deal(reason=1)]
        attr = attribute_from_mt5_deals(deals)
        assert attr.exit_reason == "MANUAL"

    def test_deal_reason_web_manual(self):
        deals = [self._make_deal(reason=2)]
        attr = attribute_from_mt5_deals(deals)
        assert attr.exit_reason == "MANUAL"

    def test_deal_reason_expert_algo(self):
        deals = [self._make_deal(reason=3)]
        attr = attribute_from_mt5_deals(deals)
        assert attr.exit_reason == "ALGO"
        assert attr.exit_reason_source == "mt5_deal"

    def test_unknown_reason_preserves_raw(self):
        deals = [self._make_deal(reason=999)]
        attr = attribute_from_mt5_deals(deals)
        assert attr.exit_reason == "BROKER_CLOSED_UNKNOWN"
        assert attr.raw_broker_reason == 999
        assert attr.exit_reason_source == "mt5_deal"

    def test_none_deals_returns_unknown(self):
        attr = attribute_from_mt5_deals(None)
        assert attr.exit_reason == "BROKER_CLOSED_UNKNOWN"
        assert attr.exit_reason_source == "unknown"

    def test_empty_deals_returns_unknown(self):
        attr = attribute_from_mt5_deals([])
        assert attr.exit_reason == "BROKER_CLOSED_UNKNOWN"
        assert attr.exit_reason_source == "unknown"

    def test_actual_fill_price_captured(self):
        deals = [self._make_deal(reason=4, price=1.09876)]
        attr = attribute_from_mt5_deals(deals)
        assert attr.actual_fill_price == 1.09876

    def test_actual_fill_time_captured(self):
        deals = [self._make_deal(reason=5, time_val=1717500123)]
        attr = attribute_from_mt5_deals(deals)
        assert attr.actual_fill_time == 1717500123.0

    def test_comment_captured(self):
        deals = [self._make_deal(reason=4, comment="sl")]
        attr = attribute_from_mt5_deals(deals)
        assert attr.raw_broker_comment == "sl"

    def test_selects_close_deal_by_entry_out(self):
        open_deal = self._make_deal(reason=3, entry=0)   # DEAL_ENTRY_IN
        close_deal = self._make_deal(reason=4, entry=1)  # DEAL_ENTRY_OUT
        attr = attribute_from_mt5_deals([open_deal, close_deal])
        assert attr.exit_reason == "SL"

    def test_fallback_to_last_deal_when_no_entry_out(self):
        deal1 = self._make_deal(reason=3, entry=0)
        deal2 = self._make_deal(reason=5, entry=0)
        attr = attribute_from_mt5_deals([deal1, deal2])
        assert attr.exit_reason == "TP"


# ── TradeManager attribution ────────────────────────────────────────────────


class TestTradeManagerAttribution:

    def test_stall_exit(self):
        attr = attribute_from_trade_manager("Stall exit — 75min (limit 60), 2.3pip")
        assert attr.exit_reason == "Stall exit — 75min (limit 60), 2.3pip"
        assert attr.exit_reason_source == "trade_manager"

    def test_structure_exit(self):
        attr = attribute_from_trade_manager("Structure exit — bearish shift after 45min")
        assert attr.exit_reason == "Structure exit — bearish shift after 45min"
        assert attr.exit_reason_source == "trade_manager"

    def test_none_reason(self):
        attr = attribute_from_trade_manager(None)
        assert attr.exit_reason == "CLOSED"
        assert attr.exit_reason_source == "trade_manager"

    def test_empty_reason(self):
        attr = attribute_from_trade_manager("")
        assert attr.exit_reason == "CLOSED"
        assert attr.exit_reason_source == "trade_manager"


# ── Deriv unknown marking ───────────────────────────────────────────────────


class TestDerivUnknown:

    def test_deriv_returns_unknown_source(self):
        attr = attribute_unknown("deriv")
        assert attr.exit_reason_source == "unknown"
        assert attr.exit_reason == "BROKER_CLOSED_UNKNOWN"
        assert "pending" in attr.raw_broker_comment.lower()

    def test_mt5_returns_unknown_source(self):
        attr = attribute_unknown("mt5")
        assert attr.exit_reason_source == "unknown"


# ── Reconciliation ──────────────────────────────────────────────────────────


class TestReconciliation:

    def test_broker_only(self):
        broker = ExitAttribution(exit_reason="SL", exit_reason_source="mt5_deal", raw_broker_reason=4)
        result = reconcile(broker, None)
        assert result.exit_reason == "SL"
        assert result.exit_reason_source == "mt5_deal"

    def test_manager_only(self):
        mgr = ExitAttribution(exit_reason="Stall exit", exit_reason_source="trade_manager")
        result = reconcile(None, mgr)
        assert result.exit_reason == "Stall exit"
        assert result.exit_reason_source == "trade_manager"

    def test_both_none(self):
        result = reconcile(None, None)
        assert result.exit_reason == "UNKNOWN"
        assert result.exit_reason_source == "unknown"

    def test_discrepancy_flagged_when_broker_sl_but_manager_stall(self):
        broker = ExitAttribution(exit_reason="SL", exit_reason_source="mt5_deal", raw_broker_reason=4)
        mgr = ExitAttribution(exit_reason="Stall exit", exit_reason_source="trade_manager")
        result = reconcile(broker, mgr)
        assert result.discrepancy is True
        assert result.exit_reason == "SL"

    def test_no_discrepancy_when_manager_is_closed(self):
        broker = ExitAttribution(exit_reason="SL", exit_reason_source="mt5_deal", raw_broker_reason=4)
        mgr = ExitAttribution(exit_reason="CLOSED", exit_reason_source="trade_manager")
        result = reconcile(broker, mgr)
        assert result.discrepancy is False


# ── TRADE_CLOSE event emission ──────────────────────────────────────────────


class TestTradeCloseEvent:

    def test_trade_close_event_carries_correlation_id(self):
        from persistence.event_store import EventStore

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test_events.db")
            store = EventStore(db_path=db_path)
            try:
                test_cycle_id = "cycle-test-123"
                store.emit(
                    event_type=TRADE_CLOSE,
                    severity="INFO",
                    symbol="EURUSD",
                    correlation_id=test_cycle_id,
                    source_module="platforms.main_loop",
                    payload={
                        "order_id": "12345",
                        "direction": "BUY",
                        "exit_reason": "SL",
                        "exit_reason_source": "mt5_deal",
                        "raw_broker_reason": 4,
                        "discrepancy": False,
                    },
                )
                time.sleep(0.5)
                rows = store.query(event_type=TRADE_CLOSE, correlation_id=test_cycle_id)
                assert len(rows) == 1
                row = rows[0]
                assert row["correlation_id"] == test_cycle_id
                assert row["event_type"] == TRADE_CLOSE
                payload = json.loads(row["payload_json"])
                assert payload["exit_reason"] == "SL"
                assert payload["exit_reason_source"] == "mt5_deal"
                assert payload["raw_broker_reason"] == 4
                assert payload["discrepancy"] is False
            finally:
                store.close()

    def test_trade_close_event_with_unknown_attribution(self):
        from persistence.event_store import EventStore

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test_events.db")
            store = EventStore(db_path=db_path)
            try:
                store.emit(
                    event_type=TRADE_CLOSE,
                    severity="INFO",
                    symbol="BTCUSD",
                    source_module="platforms.main_loop",
                    payload={
                        "exit_reason": "BROKER_CLOSED_UNKNOWN",
                        "exit_reason_source": "unknown",
                        "raw_broker_reason": None,
                    },
                )
                time.sleep(0.5)
                rows = store.query(event_type=TRADE_CLOSE)
                assert len(rows) == 1
                payload = json.loads(rows[0]["payload_json"])
                assert payload["exit_reason_source"] == "unknown"
            finally:
                store.close()


# ── Rollover / split / vmargin edge cases ────────────────────────────────────


class TestMT5EdgeCases:

    def _make_deal(self, reason, entry=1):
        return SimpleNamespace(
            reason=reason, comment=None, price=1.0, time=0, entry=entry,
            profit=0, commission=0, swap=0, fee=0, type=1,
        )

    def test_rollover(self):
        attr = attribute_from_mt5_deals([self._make_deal(reason=7)])
        assert attr.exit_reason == "ROLLOVER"

    def test_vmargin(self):
        attr = attribute_from_mt5_deals([self._make_deal(reason=8)])
        assert attr.exit_reason == "VMARGIN"

    def test_split(self):
        attr = attribute_from_mt5_deals([self._make_deal(reason=9)])
        assert attr.exit_reason == "SPLIT"
