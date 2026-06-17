"""
Tests for the Emitter Feedback service (adaptive/emitter_feedback.py).

Dependency-light — builds a real SignalLedger on a temp DB, grades signals,
then verifies the read-side feedback: traded-vs-blocked accuracy split, pair and
gate breakdowns, the signal_value_when_blocked over-filtering metric, context
frequency surfacing, gate effectiveness aggregation, all-emitter summaries, and
safe defaults on empty data.
"""

import pytest

from adaptive.signal_ledger import SignalLedger, SignalRecord
from adaptive.emitter_feedback import (
    EmitterFeedbackService,
    EmitterFeedbackRequest,
    EmitterFeedbackResponse,
)


@pytest.fixture
def ledger(tmp_path):
    led = SignalLedger(db_path=tmp_path / "fb.db", grading_delay_minutes=0.0)
    yield led
    led.close()


@pytest.fixture
def service(ledger):
    return EmitterFeedbackService(ledger)


def _sig(pair, emitter, direction, price, ctx=None):
    return SignalRecord(
        pair=pair, emitter=emitter, direction=direction,
        strength=0.7, price_at_signal=price, context=ctx or {},
    )


class TestRequestFeedback:
    def test_basic_split(self, ledger, service):
        # momentum: one traded (correct), one blocked (wrong).
        t = ledger.record_signal(_sig("EURUSD", "momentum", "LONG", 100.0))
        b = ledger.record_signal(_sig("GBPUSD", "momentum", "LONG", 100.0))
        ledger.record_trade_opened(t, "ord1")
        ledger.record_gate_block(b, "planner_skip")
        ledger.run_grading_cycle({"EURUSD": 101.0, "GBPUSD": 99.0})

        resp = service.request_feedback(EmitterFeedbackRequest(emitter="momentum"))
        assert isinstance(resp, EmitterFeedbackResponse)
        assert resp.total_signals == 2
        assert resp.traded_signals == 1
        assert resp.blocked_signals == 1
        assert resp.accuracy_traded == 1.0
        assert resp.accuracy_blocked == 0.0
        assert resp.accuracy_all == 0.5

    def test_accuracy_by_pair(self, ledger, service):
        ledger.record_signal(_sig("EURUSD", "structure", "LONG", 100.0))
        ledger.record_signal(_sig("GBPUSD", "structure", "LONG", 100.0))
        ledger.run_grading_cycle({"EURUSD": 101.0, "GBPUSD": 99.0})
        resp = service.request_feedback(EmitterFeedbackRequest(emitter="structure"))
        assert resp.accuracy_by_pair["EURUSD"] == 1.0
        assert resp.accuracy_by_pair["GBPUSD"] == 0.0

    def test_accuracy_by_gate(self, ledger, service):
        b1 = ledger.record_signal(_sig("EURUSD", "vwap", "LONG", 100.0))
        b2 = ledger.record_signal(_sig("GBPUSD", "vwap", "LONG", 100.0))
        ledger.record_gate_block(b1, "entry_score")
        ledger.record_gate_block(b2, "entry_score")
        ledger.run_grading_cycle({"EURUSD": 101.0, "GBPUSD": 101.0})
        resp = service.request_feedback(EmitterFeedbackRequest(emitter="vwap"))
        assert resp.accuracy_by_gate["entry_score"] == 1.0

    def test_signal_value_when_blocked(self, ledger, service):
        # Both blocked signals were actually correct → gate is over-filtering.
        b1 = ledger.record_signal(_sig("EURUSD", "liquidity", "LONG", 100.0))
        b2 = ledger.record_signal(_sig("GBPUSD", "liquidity", "LONG", 100.0))
        ledger.record_gate_block(b1, "corr")
        ledger.record_gate_block(b2, "corr")
        ledger.run_grading_cycle({"EURUSD": 101.0, "GBPUSD": 101.0})
        resp = service.request_feedback(EmitterFeedbackRequest(emitter="liquidity"))
        assert resp.signal_value_when_blocked == 1.0

    def test_include_blocked_false_excludes(self, ledger, service):
        t = ledger.record_signal(_sig("EURUSD", "momentum", "LONG", 100.0))
        b = ledger.record_signal(_sig("GBPUSD", "momentum", "LONG", 100.0))
        ledger.record_trade_opened(t, "ord1")
        ledger.record_gate_block(b, "planner_skip")
        ledger.run_grading_cycle({"EURUSD": 101.0, "GBPUSD": 99.0})
        resp = service.request_feedback(
            EmitterFeedbackRequest(emitter="momentum", include_blocked=False)
        )
        assert resp.total_signals == 1
        assert resp.blocked_signals == 0

    def test_context_frequencies(self, ledger, service):
        c1 = ledger.record_signal(_sig("EURUSD", "momentum", "LONG", 100.0, {"timeframe": "M5"}))
        c2 = ledger.record_signal(_sig("GBPUSD", "momentum", "LONG", 100.0, {"timeframe": "M5"}))
        w1 = ledger.record_signal(_sig("USDJPY", "momentum", "LONG", 100.0, {"timeframe": "H1"}))
        assert c1 and c2 and w1
        ledger.run_grading_cycle({"EURUSD": 101.0, "GBPUSD": 101.0, "USDJPY": 99.0})
        resp = service.request_feedback(EmitterFeedbackRequest(emitter="momentum"))
        assert resp.common_correct_context.get("timeframe=M5") == 2
        assert resp.common_wrong_context.get("timeframe=H1") == 1

    def test_trade_outcomes_collected(self, ledger, service):
        t = ledger.record_signal(_sig("EURUSD", "momentum", "LONG", 100.0))
        ledger.record_trade_opened(t, "ord1")
        ledger.run_grading_cycle({"EURUSD": 101.0})
        ledger.attach_trade_outcome("ord1", {"pnl_r": 2.0})
        resp = service.request_feedback(EmitterFeedbackRequest(emitter="momentum"))
        assert {"pnl_r": 2.0} in resp.trade_outcomes


class TestGateEffectiveness:
    def test_gate_effectiveness(self, ledger, service):
        # gate "strict": blocked 2, both correct → over-filtering.
        # gate "good":   blocked 1, wrong → earning its keep.
        b1 = ledger.record_signal(_sig("EURUSD", "momentum", "LONG", 100.0))
        b2 = ledger.record_signal(_sig("GBPUSD", "structure", "LONG", 100.0))
        b3 = ledger.record_signal(_sig("USDJPY", "vwap", "LONG", 100.0))
        ledger.record_gate_block(b1, "strict")
        ledger.record_gate_block(b2, "strict")
        ledger.record_gate_block(b3, "good")
        ledger.run_grading_cycle({"EURUSD": 101.0, "GBPUSD": 101.0, "USDJPY": 99.0})
        eff = service.get_gate_effectiveness()
        assert eff["strict"]["blocked"] == 2
        assert eff["strict"]["blocked_accuracy"] == 1.0
        assert eff["good"]["blocked_accuracy"] == 0.0


class TestAllSummaries:
    def test_all_emitter_summaries(self, ledger, service):
        ledger.record_signal(_sig("EURUSD", "momentum", "LONG", 100.0))
        ledger.record_signal(_sig("EURUSD", "structure", "SHORT", 100.0))
        ledger.run_grading_cycle({"EURUSD": 101.0})
        summaries = service.get_all_emitter_summaries()
        assert set(summaries.keys()) == {"momentum", "structure"}
        assert summaries["momentum"].accuracy_all == 1.0   # LONG, price up
        assert summaries["structure"].accuracy_all == 0.0  # SHORT, price up


class TestSafeDefaults:
    def test_empty_emitter_returns_defaults(self, service):
        resp = service.request_feedback(EmitterFeedbackRequest(emitter="ghost"))
        assert resp.emitter == "ghost"
        assert resp.total_signals == 0
        assert resp.accuracy_all == 0.0
        assert resp.accuracy_by_pair == {}
        assert resp.trade_outcomes == []

    def test_gate_effectiveness_empty(self, service):
        assert service.get_gate_effectiveness() == {}

    def test_all_summaries_empty(self, service):
        assert service.get_all_emitter_summaries() == {}
