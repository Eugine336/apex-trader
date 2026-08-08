"""Management-specific reasoning cadence (Q40 — reasoned management).

The Brain re-reasons an OPEN position on its OWN tighter throttle bucket, so
management reacts faster than origination WITHOUT raising the global
origination/council rate. Verified at both the reasoner (independent bucket +
per-call interval override) and the Brain (management passes the override,
origination does not; duck-typed stubs fall back safely).
"""

import json

from cognition.brain import CognitiveBrain, PositionView
from cognition.contracts import Evidence, MarketState
from llm.reasoner import LLMReasoner


class _StubClient:
    usable = True
    model = "stub"

    def __init__(self, reply):
        self._reply = reply

    def complete(self, system, user):
        return self._reply


def _ok():
    return json.dumps({"direction": "LONG", "confidence": 0.8})


def _state(symbol="EURUSD"):
    ms = MarketState(symbol=symbol)
    for i in range(6):
        ms.add(Evidence(source_module=f"m{i}", confidence=0.9, uncertainty=0.1))
    return ms


# ── Reasoner: independent throttle bucket + per-call interval override ────────

def test_management_bucket_is_independent_of_origination():
    r = LLMReasoner(client=_StubClient(_ok()), enabled=True, min_interval_seconds=30)
    assert r.reason("EURUSD", {}, now=1000.0) is not None          # origination bucket set
    assert r.reason("EURUSD", {}, now=1010.0) is None              # origination throttled (30s)
    # Management uses a distinct bucket + 8s interval — not throttled by origination.
    assert r.reason("EURUSD", {}, now=1010.0,
                    min_interval=8.0, throttle_key="EURUSD\x00manage") is not None


def test_management_bucket_throttles_at_its_own_interval():
    r = LLMReasoner(client=_StubClient(_ok()), enabled=True, min_interval_seconds=30)
    k = "EURUSD\x00manage"
    assert r.reason("EURUSD", {}, now=1000.0, min_interval=8.0, throttle_key=k) is not None
    assert r.reason("EURUSD", {}, now=1005.0, min_interval=8.0, throttle_key=k) is None      # 5s < 8s
    assert r.reason("EURUSD", {}, now=1009.0, min_interval=8.0, throttle_key=k) is not None  # 9s >= 8s


# ── Brain: management passes the override; origination does not ──────────────

class _Opinion:
    def __init__(self, direction="LONG", confidence=0.8):
        self.direction = direction
        self.confidence = confidence
        self.rationale = "x"


class _SpyReasoner:
    available = True

    def __init__(self):
        self.calls = []

    def reason(self, symbol, evidence, *, now=None, min_interval=None, throttle_key=None):
        self.calls.append({"min_interval": min_interval, "throttle_key": throttle_key})
        return _Opinion()

    def last_reason_degraded(self, symbol):
        return False


class _LegacyReasoner:
    available = True

    def reason(self, symbol, evidence, now=None):
        return _Opinion()


def test_brain_detects_override_support():
    assert CognitiveBrain(reasoner=_SpyReasoner())._reason_supports_override is True
    assert CognitiveBrain(reasoner=_LegacyReasoner())._reason_supports_override is False


def test_brain_management_uses_tighter_bucket():
    spy = _SpyReasoner()
    brain = CognitiveBrain(reasoner=spy, manage_min_interval_seconds=8.0)
    brain.manage(PositionView(symbol="EURUSD", direction="LONG"), _state("EURUSD"))
    assert spy.calls[-1]["min_interval"] == 8.0
    assert spy.calls[-1]["throttle_key"] == "EURUSD\x00manage"


def test_brain_origination_keeps_global_rate():
    spy = _SpyReasoner()
    brain = CognitiveBrain(reasoner=spy, manage_min_interval_seconds=8.0)
    brain.reason(_state("EURUSD"))
    assert spy.calls[-1]["min_interval"] is None
    assert spy.calls[-1]["throttle_key"] is None


def test_brain_legacy_reasoner_manage_does_not_raise():
    brain = CognitiveBrain(reasoner=_LegacyReasoner(), manage_min_interval_seconds=8.0)
    out = brain.manage(PositionView(symbol="EURUSD", direction="LONG"), _state("EURUSD"))
    assert out is not None
