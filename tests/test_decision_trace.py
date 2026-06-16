"""
Tests for the Decision Trace pipeline-awareness layer.

Covers: mandatory/meaningful justification enforcement, challenge recording,
recorder lifecycle (begin → stamp → finalize), precise rejection attribution,
the loud "stage forgot to stamp" completeness check, a full end-to-end pipeline
trace, and persistence round-trip through the EventStore.
"""

import json
import tempfile
from pathlib import Path

import pytest

from brain.decision_trace import (
    Challenge,
    DecisionTrace,
    DecisionTraceRecorder,
    StageVerdict,
    STAGE_RANKER,
    STAGE_CORRELATION,
    STAGE_DECISION_ENGINE,
    STAGE_PLANNER,
    STAGE_RISK_STACK,
    OUTCOME_TRADE_PLACED,
    OUTCOME_ABORTED,
    PIPELINE_STAGES,
)
from config import DecisionTraceConfig


class _FakeStore:
    """Captures emit() calls instead of writing to disk."""

    def __init__(self):
        self.events = []

    def emit(self, *, event_type, severity, symbol=None, correlation_id=None,
             parent_id=None, source_module=None, payload=None):
        self.events.append({
            "event_type": event_type,
            "symbol": symbol,
            "correlation_id": correlation_id,
            "parent_id": parent_id,
            "payload": payload,
        })
        return "evt-1"


# ── Model validation ──────────────────────────────────────────────────────


class TestStageVerdictValidation:
    def test_empty_justification_raises(self):
        with pytest.raises(ValueError):
            StageVerdict(stage="ranker", owner="x", verdict="PASS", justification="")

    def test_too_short_justification_raises(self):
        with pytest.raises(ValueError):
            StageVerdict(stage="ranker", owner="x", verdict="PASS", justification="ok")

    def test_empty_stage_raises(self):
        with pytest.raises(ValueError):
            StageVerdict(stage="", owner="x", verdict="PASS", justification="a meaningful reason")

    def test_empty_verdict_raises(self):
        with pytest.raises(ValueError):
            StageVerdict(stage="ranker", owner="x", verdict="", justification="a meaningful reason")

    def test_valid_verdict_ok_and_confidence_clamped(self):
        v = StageVerdict(stage="ranker", owner="opportunity_ranker", verdict="LONG_SCALP",
                         justification="best EV cluster on M5", confidence=1.7)
        assert v.confidence == 1.0
        v2 = StageVerdict(stage="ranker", owner="o", verdict="X",
                          justification="another good reason", confidence=-3)
        assert v2.confidence == 0.0

    def test_to_dict_round_trip(self):
        v = StageVerdict(stage="planner", owner="trade_planner", verdict="ENTER",
                         justification="timing and sizing confirmed",
                         evidence={"scan_score": 70}, confidence=0.8)
        d = v.to_dict()
        assert d["stage"] == "planner"
        assert d["evidence"] == {"scan_score": 70}
        assert d["confidence"] == 0.8


class TestChallengeValidation:
    def test_empty_reason_raises(self):
        with pytest.raises(ValueError):
            Challenge(challenger="planner", target_stage="ranker", reason="")

    def test_valid_challenge_ok(self):
        c = Challenge(challenger="governor", target_stage="decision_engine",
                      reason="overrode the engine verdict on thin margin")
        assert c.challenger == "governor"
        assert c.to_dict()["target_stage"] == "decision_engine"


# ── DecisionTrace introspection ─────────────────────────────────────────────


class TestDecisionTrace:
    def _trace(self):
        return DecisionTrace(pair="EURUSD", trace_id="t1")

    def test_add_and_query_stages(self):
        t = self._trace()
        t.add_verdict(StageVerdict(stage=STAGE_RANKER, owner="r", verdict="LONG",
                                   justification="ranker picked long"))
        assert t.has_stage(STAGE_RANKER)
        assert t.stage_names() == [STAGE_RANKER]

    def test_rejected_at_parses_outcome(self):
        t = self._trace()
        t.final_outcome = "REJECTED@correlation"
        assert t.rejected_at() == "correlation"

    def test_rejected_at_none_for_placed(self):
        t = self._trace()
        t.final_outcome = OUTCOME_TRADE_PLACED
        assert t.rejected_at() is None

    def test_full_summary_and_to_dict(self):
        t = self._trace()
        t.add_verdict(StageVerdict(stage=STAGE_RANKER, owner="r", verdict="LONG",
                                   justification="ranker picked long"))
        t.add_challenge(Challenge(challenger="planner", target_stage=STAGE_RANKER,
                                  reason="confidence too low to trust"))
        t.final_outcome = OUTCOME_TRADE_PLACED
        summary = t.full_summary()
        assert "EURUSD" in summary and "ranker" in summary
        d = t.to_dict()
        assert d["pair"] == "EURUSD"
        assert len(d["stages"]) == 1
        assert len(d["challenges"]) == 1
        # serialisable
        json.dumps(d)


# ── Recorder lifecycle ──────────────────────────────────────────────────────


class TestRecorder:
    def _recorder(self, store=None, enabled=True):
        return DecisionTraceRecorder(DecisionTraceConfig(enabled=enabled), event_store=store)

    def test_disabled_is_noop(self):
        rec = self._recorder(enabled=False)
        assert rec.begin("EURUSD") is None
        rec.stamp(STAGE_RANKER, "r", "LONG", "should not record")
        rec.finalize_success()
        assert rec.current is None

    def test_full_pipeline_success_persists_all_stages(self):
        store = _FakeStore()
        rec = self._recorder(store)
        rec.begin("EURUSD", cycle_id="cycle-1", setup_id="setup-1")
        for stage in PIPELINE_STAGES:
            rec.stamp(stage, f"{stage}_owner", "PASS", f"{stage} approved this setup",
                      evidence={"k": 1}, confidence=0.7)
        rec.finalize_success()
        assert len(store.events) == 1
        payload = store.events[0]["payload"]
        assert payload["final_outcome"] == OUTCOME_TRADE_PLACED
        assert {s["stage"] for s in payload["stages"]} == set(PIPELINE_STAGES)
        assert store.events[0]["correlation_id"] == "cycle-1"
        assert rec.current is None  # cleared after finalize

    def test_rejection_attributed_to_blocking_stage(self):
        store = _FakeStore()
        rec = self._recorder(store)
        rec.begin("GBPUSD")
        rec.stamp(STAGE_RANKER, "r", "LONG", "ranker chose long")
        rec.stamp(STAGE_CORRELATION, "correlation_engine", "BLOCK",
                  "hedge conflict with open EURUSD", blocking=True)
        rec.finalize_rejection("hedge conflict with open EURUSD")
        payload = store.events[0]["payload"]
        assert payload["final_outcome"] == "REJECTED@correlation"
        assert payload["rejected_at"] == "correlation"

    def test_rejection_without_blocking_stage_falls_back_to_risk_stack(self):
        store = _FakeStore()
        rec = self._recorder(store)
        rec.begin("USDJPY")
        rec.stamp(STAGE_RANKER, "r", "LONG", "ranker chose long")
        rec.stamp(STAGE_DECISION_ENGINE, "decision_engine", "ENTER", "engine approved")
        # A secondary gate (spread/EV/ML) rejected without stamping a core stage.
        rec.finalize_rejection("spread too wide")
        payload = store.events[0]["payload"]
        assert payload["final_outcome"] == f"REJECTED@{STAGE_RISK_STACK}"

    def test_challenge_recorded(self):
        store = _FakeStore()
        rec = self._recorder(store)
        rec.begin("EURUSD")
        rec.stamp(STAGE_RANKER, "r", "LONG_SCALP", "ranker picked scalp long")
        rec.challenge("planner", STAGE_RANKER, "scanner score weak for this pick")
        rec.stamp(STAGE_PLANNER, "trade_planner", "ENTER", "timing confirmed")
        rec.finalize_success()
        payload = store.events[0]["payload"]
        assert len(payload["challenges"]) == 1
        assert payload["challenges"][0]["challenger"] == "planner"

    def test_abandoned_outcome(self):
        store = _FakeStore()
        rec = self._recorder(store)
        rec.begin("EURUSD")
        rec.stamp(STAGE_RANKER, "r", "LONG", "ranker chose long")
        rec.finalize_abandoned("circuit breaker open")
        assert store.events[0]["payload"]["final_outcome"] == OUTCOME_ABORTED

    def test_unfinalized_previous_trace_is_abandoned_on_begin(self):
        store = _FakeStore()
        rec = self._recorder(store)
        rec.begin("EURUSD")
        rec.stamp(STAGE_RANKER, "r", "LONG", "ranker chose long")
        # Begin again without finalising — the prior trace must be flushed.
        rec.begin("GBPUSD")
        assert len(store.events) == 1
        assert store.events[0]["payload"]["final_outcome"] == OUTCOME_ABORTED
        assert store.events[0]["payload"]["pair"] == "EURUSD"

    def test_completeness_check_logs_when_ranker_missing(self):
        # A trace that never stamped the ranker should still persist (loud, not
        # fatal). We assert it does not raise and is persisted.
        store = _FakeStore()
        rec = self._recorder(store)
        rec.begin("EURUSD")
        rec.stamp(STAGE_CORRELATION, "correlation_engine", "PASS", "no conflict here")
        rec.finalize_success()
        assert len(store.events) == 1
        assert not any(s["stage"] == STAGE_RANKER for s in store.events[0]["payload"]["stages"])

    def test_malformed_stamp_does_not_break_loop(self):
        store = _FakeStore()
        rec = self._recorder(store)
        rec.begin("EURUSD")
        # Empty justification would raise in StageVerdict — recorder swallows it.
        rec.stamp(STAGE_RANKER, "r", "LONG", "")
        rec.finalize_success()
        # No ranker stamp recorded, but the trace still finalised cleanly.
        assert store.events[0]["payload"]["stages"] == []


# ── Persistence round-trip through the real EventStore ──────────────────────


class TestPersistenceRoundTrip:
    def test_write_and_read_back(self):
        from persistence.event_store import EventStore
        from persistence.domain_events import DECISION_TRACE

        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "trace_events.db")
            store = EventStore(db_path=db_path)
            try:
                rec = DecisionTraceRecorder(DecisionTraceConfig(enabled=True), event_store=store)
                rec.begin("EURUSD", cycle_id="cycle-xyz", setup_id="setup-xyz")
                rec.stamp(STAGE_RANKER, "opportunity_ranker", "LONG_SWING",
                          "best EV cluster on swing horizon", evidence={"ev_r": 1.48},
                          confidence=0.78)
                rec.stamp(STAGE_PLANNER, "trade_planner", "ENTER", "timing confirmed")
                rec.finalize_success()
                store.flush()

                rows = store.query_events(
                    severity_min="DEBUG", event_types=[DECISION_TRACE], limit=10,
                )
                assert len(rows) == 1
                payload = json.loads(rows[0]["payload_json"])
                assert payload["pair"] == "EURUSD"
                assert payload["final_outcome"] == OUTCOME_TRADE_PLACED
                stages = {s["stage"] for s in payload["stages"]}
                assert stages == {STAGE_RANKER, STAGE_PLANNER}
                assert rows[0]["correlation_id"] == "cycle-xyz"
            finally:
                store.close()
