"""
Tests for the Emitter Feedback service (adaptive/emitter_feedback.py).

Dependency-light — builds a real observation ledger on a temp DB, grades
observations, then verifies the read-side feedback: the traded-vs-blocked quality
split, pair and gate breakdowns, the signal_value_when_blocked over-filtering
metric, useful/low-quality context surfacing, gate effectiveness aggregation,
all-emitter summaries, and safe defaults on empty data.

Grading is direction-agnostic (Constitution §XXIX): an observation is
high-quality when a material move followed it — in EITHER direction — never
because a direction it implied turned out right.
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
        # momentum: one traded observation followed by a material move (useful),
        # one blocked observation with NO material move (low quality).
        t = ledger.record_signal(_sig("EURUSD", "momentum", "LONG", 100.0))
        b = ledger.record_signal(_sig("GBPUSD", "momentum", "LONG", 100.0))
        ledger.record_trade_opened(t, "ord1")
        ledger.record_gate_block(b, "planner_skip")
        ledger.run_grading_cycle({"EURUSD": 101.0, "GBPUSD": 100.0})

        resp = service.request_feedback(EmitterFeedbackRequest(emitter="momentum"))
        assert isinstance(resp, EmitterFeedbackResponse)
        assert resp.total_signals == 2
        assert resp.traded_signals == 1
        assert resp.blocked_signals == 1
        assert resp.accuracy_traded == 1.0
        assert resp.accuracy_blocked == 0.0
        assert resp.accuracy_all == 0.5

    def test_quality_is_direction_agnostic(self, ledger, service):
        # A SHORT observation followed by a +1% move still SAW a real event —
        # it is high-quality, not "wrong". This is the core §XXIX behaviour.
        ledger.record_signal(_sig("EURUSD", "structure", "SHORT", 100.0))
        ledger.run_grading_cycle({"EURUSD": 101.0})
        resp = service.request_feedback(EmitterFeedbackRequest(emitter="structure"))
        assert resp.accuracy_all == 1.0

    def test_accuracy_by_pair(self, ledger, service):
        ledger.record_signal(_sig("EURUSD", "structure", "LONG", 100.0))
        ledger.record_signal(_sig("GBPUSD", "structure", "LONG", 100.0))
        # EURUSD sees a material move; GBPUSD stays flat (no material event).
        ledger.run_grading_cycle({"EURUSD": 101.0, "GBPUSD": 100.0})
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
        # Both blocked observations were followed by a material move → the gate
        # is over-filtering real reads.
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
        # Two M5 observations see a material move (useful); one H1 observation
        # stays flat (low quality). Context frequencies split accordingly.
        c1 = ledger.record_signal(_sig("EURUSD", "momentum", "LONG", 100.0, {"timeframe": "M5"}))
        c2 = ledger.record_signal(_sig("GBPUSD", "momentum", "LONG", 100.0, {"timeframe": "M5"}))
        w1 = ledger.record_signal(_sig("USDJPY", "momentum", "LONG", 100.0, {"timeframe": "H1"}))
        assert c1 and c2 and w1
        ledger.run_grading_cycle({"EURUSD": 101.0, "GBPUSD": 101.0, "USDJPY": 100.0})
        resp = service.request_feedback(EmitterFeedbackRequest(emitter="momentum"))
        assert resp.common_useful_context.get("timeframe=M5") == 2
        assert resp.common_low_quality_context.get("timeframe=H1") == 1

    def test_trade_outcomes_collected(self, ledger, service):
        t = ledger.record_signal(_sig("EURUSD", "momentum", "LONG", 100.0))
        ledger.record_trade_opened(t, "ord1")
        ledger.run_grading_cycle({"EURUSD": 101.0})
        ledger.attach_trade_outcome("ord1", {"pnl_r": 2.0})
        resp = service.request_feedback(EmitterFeedbackRequest(emitter="momentum"))
        assert {"pnl_r": 2.0} in resp.trade_outcomes


class TestGateEffectiveness:
    def test_gate_effectiveness(self, ledger, service):
        # gate "strict": blocked 2, both saw a material move → over-filtering.
        # gate "good":   blocked 1, flat (no move) → earning its keep.
        b1 = ledger.record_signal(_sig("EURUSD", "momentum", "LONG", 100.0))
        b2 = ledger.record_signal(_sig("GBPUSD", "structure", "LONG", 100.0))
        b3 = ledger.record_signal(_sig("USDJPY", "vwap", "LONG", 100.0))
        ledger.record_gate_block(b1, "strict")
        ledger.record_gate_block(b2, "strict")
        ledger.record_gate_block(b3, "good")
        ledger.run_grading_cycle({"EURUSD": 101.0, "GBPUSD": 101.0, "USDJPY": 100.0})
        eff = service.get_gate_effectiveness()
        assert eff["strict"]["blocked"] == 2
        assert eff["strict"]["would_have_been_useful"] == 2
        assert eff["strict"]["blocked_accuracy"] == 1.0
        assert eff["good"]["blocked_accuracy"] == 0.0


class TestAllSummaries:
    def test_all_emitter_summaries(self, ledger, service):
        # Both observations are followed by the same +1% material move, so both
        # are high-quality — direction (LONG vs SHORT) is irrelevant to quality.
        ledger.record_signal(_sig("EURUSD", "momentum", "LONG", 100.0))
        ledger.record_signal(_sig("EURUSD", "structure", "SHORT", 100.0))
        ledger.run_grading_cycle({"EURUSD": 101.0})
        summaries = service.get_all_emitter_summaries()
        assert set(summaries.keys()) == {"momentum", "structure"}
        assert summaries["momentum"].accuracy_all == 1.0
        assert summaries["structure"].accuracy_all == 1.0


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
