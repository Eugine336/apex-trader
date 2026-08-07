"""Offline tests for the Advisory-Council pipeline (Constitution Part XXI).

Covers the Consultation Ledger (records + per-advisor scorecards, Art 9/10/11)
and the consolidator's adaptive consultation depth + ledger recording (Art 8).
"""

from types import SimpleNamespace

from cognition.consultation_ledger import ConsultationLedger, build_consultation_ledger
from cognition.contracts import Evidence, EvidenceDomain
from cognition.loop import EvidenceConsolidator


# ── Fakes ─────────────────────────────────────────────────────────────────────

def _op(engine, direction, confidence=0.7, latency_ms=10.0):
    return SimpleNamespace(engine=engine, direction=direction,
                           confidence=confidence, latency_ms=latency_ms)


def _consultation(symbol, opinions, consulted=None, capability="strategic_reasoning"):
    return SimpleNamespace(
        symbol=symbol, opinions=opinions,
        consulted=consulted if consulted is not None else [o.engine for o in opinions],
        capability=capability,
    )


class _FakeOrch:
    available = True

    def __init__(self, opinions):
        self._opinions = opinions
        self.calls = []

    def consult(self, symbol, evidence, *, now=None, capability="", max_engines=None):
        self.calls.append({"symbol": symbol, "capability": capability,
                           "max_engines": max_engines})
        return _consultation(symbol, self._opinions, capability=capability)


class _FakeLedger:
    def __init__(self):
        self.records = []

    def record(self, consultation, *, uncertainty=None, capability=None, now=None):
        self.records.append({"consultation": consultation, "uncertainty": uncertainty,
                             "capability": capability})


def _evi(polarity, confidence=0.9, uncertainty=0.05):
    return Evidence(
        source_module="test.src", domain=EvidenceDomain.MULTI_TIMEFRAME,
        symbol="EURUSD", observation="x", confidence=confidence,
        uncertainty=uncertainty, polarity=polarity,
    )  # relevance_horizon_seconds defaults to None ⇒ always fresh


# ── ConsultationLedger: records + scorecards ──────────────────────────────────

def test_record_builds_scorecards_and_majority():
    led = ConsultationLedger()
    rec = led.record(_consultation(
        "EURUSD",
        [_op("gpt", "LONG", 0.8, 12.0), _op("claude", "LONG", 0.6, 30.0),
         _op("gemini", "SHORT", 0.5, 20.0)],
    ), uncertainty=0.4)
    assert rec["majority"] == "LONG"          # 2 LONG vs 1 SHORT
    assert 0.0 < rec["dispersion"] < 1.0       # not unanimous
    assert rec["replies"] == 3
    sc = led.scorecard("gpt")
    assert sc["consulted"] == 1 and sc["replies"] == 1
    assert sc["agreement_rate"] == 1.0         # gpt agreed with LONG majority
    assert led.scorecard("gemini")["agreement_rate"] == 0.0  # disagreed


def test_non_reply_lowers_reply_rate():
    led = ConsultationLedger()
    # 'slow' was asked but did not reply.
    led.record(_consultation("EURUSD", [_op("gpt", "LONG")],
                             consulted=["gpt", "slow"]))
    assert led.scorecard("gpt")["reply_rate"] == 1.0
    slow = led.scorecard("slow")
    assert slow["consulted"] == 1 and slow["replies"] == 0
    assert slow["reply_rate"] == 0.0
    assert slow["avg_confidence"] is None


def test_per_capability_breakdown():
    led = ConsultationLedger()
    led.record(_consultation("EURUSD", [_op("gpt", "LONG")], capability="risk_analysis"))
    led.record(_consultation("EURUSD", [_op("gpt", "SHORT")], capability="risk_analysis"))
    sc = led.scorecard("gpt")
    assert sc["consulted"] == 2
    assert sc["by_capability"]["risk_analysis"]["consulted"] == 2


def test_get_status_and_running_count():
    led = ConsultationLedger(max_records=2)
    for _ in range(3):
        led.record(_consultation("EURUSD", [_op("gpt", "LONG")]))
    st = led.get_status()
    assert st["consultations"] == 3          # total ever seen
    assert st["records_kept"] == 2           # deque capped
    assert st["advisor_count"] == 1
    assert st["last"]["symbol"] == "EURUSD"


def test_record_fail_safe_on_garbage():
    led = ConsultationLedger()
    assert led.record(object()) is None      # no consulted/opinions → None, no raise
    assert led.record(None) is None


def test_persistence_writes_jsonl(tmp_path=None):
    import os, tempfile, json
    path = os.path.join(tempfile.gettempdir(), "apex_test_consultations.jsonl")
    try:
        if os.path.exists(path):
            os.remove(path)
        led = ConsultationLedger(persist_path=path)
        led.record(_consultation("EURUSD", [_op("gpt", "LONG")]))
        with open(path, encoding="utf-8") as fh:
            line = fh.readline()
        rec = json.loads(line)
        assert rec["symbol"] == "EURUSD" and rec["majority"] == "LONG"
    finally:
        if os.path.exists(path):
            os.remove(path)


def test_build_helper_disabled_returns_none():
    assert build_consultation_ledger(enabled=False) is None
    assert isinstance(build_consultation_ledger(enabled=True), ConsultationLedger)


# ── Consolidator: adaptive depth (Art 8) + recording ──────────────────────────

def test_consult_depth_shallow_when_confident_and_records():
    orch = _FakeOrch([_op("gpt", "LONG"), _op("claude", "LONG")])
    led = _FakeLedger()
    con = EvidenceConsolidator(ctx=None, reasoning=orch)
    con.set_consultation_ledger(led)
    agree = [_evi(0.5), _evi(0.5), _evi(0.5), _evi(0.5)]  # unanimous, confident
    ms = con.build("EURUSD", injected=agree, now=None)
    assert orch.calls, "orchestrator should have been consulted"
    assert orch.calls[0]["capability"] == "strategic_reasoning"
    assert orch.calls[0]["max_engines"] == 1          # clear read ⇒ 1 advisor
    assert len(led.records) == 1                       # consultation recorded
    assert led.records[0]["capability"] == "strategic_reasoning"
    # Each advisor opinion became competing Evidence for the Brain.
    srcs = {e.source_module for e in ms.evidence}
    assert "reasoning_engine.gpt" in srcs and "reasoning_engine.claude" in srcs


def test_consult_depth_deep_when_conflicted():
    orch = _FakeOrch([_op("gpt", "LONG")])
    con = EvidenceConsolidator(ctx=None, reasoning=orch)
    conflict = [_evi(0.6, 0.7, 0.9), _evi(0.6, 0.7, 0.9),
                _evi(-0.6, 0.7, 0.9), _evi(-0.6, 0.7, 0.9)]  # split + uncertain
    con.build("EURUSD", injected=conflict, now=None)
    assert orch.calls[0]["max_engines"] is None        # critical ⇒ consult all


def test_consult_falls_back_when_orch_lacks_kwargs():
    class _BasicReasoner:
        available = True

        def __init__(self):
            self.calls = 0

        def consult(self, symbol, evidence, *, now=None):
            self.calls += 1
            return _consultation(symbol, [_op("solo", "LONG")])

    r = _BasicReasoner()
    con = EvidenceConsolidator(ctx=None, reasoning=r)
    ms = con.build("EURUSD", injected=[_evi(0.5)], now=None)
    assert r.calls == 1                                 # basic signature used
    assert any(e.source_module == "reasoning_engine.solo" for e in ms.evidence)
