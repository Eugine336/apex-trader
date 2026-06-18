"""Tests for execution/intents.py — Intent dataclass and IntentType enum."""

import pytest
from datetime import datetime, timezone

from execution.intents import Intent, IntentType


class TestIntentType:
    def test_priority_ordering(self):
        assert IntentType.CLOSE > IntentType.PARTIAL_CLOSE
        assert IntentType.PARTIAL_CLOSE > IntentType.MODIFY_SL
        assert IntentType.MODIFY_SL > IntentType.MODIFY_TP

    def test_close_is_highest(self):
        assert IntentType.CLOSE == max(IntentType)

    def test_all_types_exist(self):
        assert len(IntentType) == 4


class TestIntent:
    def test_frozen(self):
        intent = Intent.close(
            symbol="EURUSD", ticket="123", source="test", reason="test",
        )
        with pytest.raises(AttributeError):
            intent.symbol = "GBPUSD"

    def test_close_factory(self):
        ts = datetime(2025, 1, 1, tzinfo=timezone.utc)
        intent = Intent.close(
            symbol="EURUSD", ticket="T1", source="stop_loss",
            reason="SL hit", timestamp=ts,
        )
        assert intent.intent_type == IntentType.CLOSE
        assert intent.symbol == "EURUSD"
        assert intent.position_ticket == "T1"
        assert intent.source == "stop_loss"
        assert intent.reason == "SL hit"
        assert intent.timestamp == ts
        assert intent.priority == IntentType.CLOSE
        assert intent.new_sl is None
        assert intent.new_tp is None
        assert intent.close_fraction is None

    def test_modify_sl_factory(self):
        intent = Intent.modify_sl(
            symbol="GBPUSD", ticket="T2", new_sl=1.25,
            source="trailing", reason="trail",
        )
        assert intent.intent_type == IntentType.MODIFY_SL
        assert intent.new_sl == 1.25
        assert intent.priority == IntentType.MODIFY_SL

    def test_modify_tp_factory(self):
        intent = Intent.modify_tp(
            symbol="XAUUSD", ticket="T3", new_tp=2050.0,
            source="tp_adjust", reason="extended",
        )
        assert intent.intent_type == IntentType.MODIFY_TP
        assert intent.new_tp == 2050.0

    def test_partial_close_factory(self):
        intent = Intent.partial_close(
            symbol="USDJPY", ticket="T4", fraction=0.5,
            source="tp1_partial", reason="TP1",
        )
        assert intent.intent_type == IntentType.PARTIAL_CLOSE
        assert intent.close_fraction == 0.5

    def test_default_timestamp(self):
        before = datetime.now(timezone.utc)
        intent = Intent.close(
            symbol="EURUSD", ticket="T5", source="test", reason="test",
        )
        after = datetime.now(timezone.utc)
        assert before <= intent.timestamp <= after

    def test_hashable(self):
        intent = Intent.close(
            symbol="EURUSD", ticket="T6", source="test", reason="test",
        )
        s = {intent}
        assert len(s) == 1
