"""Provider-failure vs genuine-observe distinction.

Constitution Part XVIII Art 5; forensic viva Q78/Q80/Q81/Q106.

A reasoner that is CONFIGURED (``available``) but whose underlying provider FAILS
at runtime (HTTP 0 / 503 / timeout) or returns an unparsable reply must NOT be
read as a FLAT/observe *market view*. The reasoner now exposes that as a liveness
signal (:meth:`LLMReasoner.last_reason_degraded`) and the Brain maps it to
``REASONER_UNAVAILABLE`` (stand down), keeping a real infrastructure outage
distinct from a genuine "nothing to do this cycle".
"""

import json

import pytest

from cognition.brain import CognitiveBrain
from cognition.contracts import DecisionType, Evidence, MarketState
from llm.reasoner import LLMReasoner


class _StubClient:
    """Duck-typed, usable client whose reply is scripted (zero network)."""

    usable = True
    model = "stub"

    def __init__(self, reply):
        self._reply = reply

    def complete(self, system, user):
        return self._reply


def _rich_state(symbol="EURUSD"):
    ms = MarketState(symbol=symbol)
    for i in range(6):
        ms.add(Evidence(source_module=f"m{i}", confidence=0.9, uncertainty=0.1))
    return ms


# ── Reasoner liveness signal ─────────────────────────────────────────────────

def test_reasoner_degraded_on_provider_failure():
    # complete() -> None mimics the client's HTTP 0 / 503 / timeout return.
    r = LLMReasoner(client=_StubClient(None), enabled=True, min_interval_seconds=0)
    assert r.reason("EURUSD", {}) is None
    assert r.last_reason_degraded("EURUSD") is True


def test_reasoner_degraded_on_unparsable_reply():
    r = LLMReasoner(client=_StubClient("no json here"), enabled=True, min_interval_seconds=0)
    assert r.reason("EURUSD", {}) is None
    assert r.last_reason_degraded("EURUSD") is True


def test_reasoner_not_degraded_on_success():
    reply = json.dumps({"direction": "LONG", "confidence": 0.8})
    r = LLMReasoner(client=_StubClient(reply), enabled=True, min_interval_seconds=0)
    assert r.reason("EURUSD", {}) is not None
    assert r.last_reason_degraded("EURUSD") is False


def test_reasoner_recovery_clears_degraded():
    ok = json.dumps({"direction": "LONG", "confidence": 0.8})
    r = LLMReasoner(client=_StubClient(None), enabled=True, min_interval_seconds=0)
    r.reason("EURUSD", {}, now=1000.0)
    assert r.last_reason_degraded("EURUSD") is True
    r._client = _StubClient(ok)  # provider recovers
    # Advance past the consecutive-failure backoff window (Part XX) so the
    # recovery call is actually attempted rather than skipped.
    r.reason("EURUSD", {}, now=2000.0)
    assert r.last_reason_degraded("EURUSD") is False


def test_throttle_does_not_mark_degraded():
    ok = json.dumps({"direction": "FLAT", "confidence": 0.1})
    r = LLMReasoner(client=_StubClient(ok), enabled=True, min_interval_seconds=100.0)
    assert r.reason("EURUSD", {}, now=1000.0) is not None   # healthy call
    assert r.reason("EURUSD", {}, now=1000.5) is None        # throttled (not a failure)
    assert r.last_reason_degraded("EURUSD") is False


def test_per_symbol_degraded_is_isolated():
    ok = json.dumps({"direction": "LONG", "confidence": 0.8})

    class _PerSymbol:
        usable = True
        model = "stub"

        def complete(self, system, user):
            # Fail only for GBPUSD; the symbol is present in the user prompt.
            return None if "GBPUSD" in user else ok

    r = LLMReasoner(client=_PerSymbol(), enabled=True, min_interval_seconds=0)
    r.reason("EURUSD", {"symbol": "EURUSD"})
    r.reason("GBPUSD", {"symbol": "GBPUSD"})
    assert r.last_reason_degraded("EURUSD") is False
    assert r.last_reason_degraded("GBPUSD") is True


# ── Brain maps a degraded reasoner to REASONER_UNAVAILABLE ────────────────────

class _DegradableReasoner:
    """Available reasoner whose reason() returns None; degraded flag scripted."""

    available = True

    def __init__(self, degraded):
        self._degraded = degraded

    def reason(self, symbol, evidence, now=None):
        return None

    def last_reason_degraded(self, symbol):
        return self._degraded


def test_brain_degraded_none_is_reasoner_unavailable():
    out = CognitiveBrain(reasoner=_DegradableReasoner(degraded=True)).reason(_rich_state())
    assert out.decision.decision_type == DecisionType.REASONER_UNAVAILABLE
    assert out.decision.authorises_action is False


def test_brain_healthy_none_is_observe():
    out = CognitiveBrain(reasoner=_DegradableReasoner(degraded=False)).reason(_rich_state())
    assert out.decision.decision_type == DecisionType.CONTINUE_OBSERVING


def test_brain_legacy_reasoner_without_signal_still_observes():
    # A reasoner predating the liveness signal (no last_reason_degraded) must
    # behave exactly as before: None -> CONTINUE_OBSERVING (fail-safe default).
    class _Legacy:
        available = True

        def reason(self, symbol, evidence, now=None):
            return None

    out = CognitiveBrain(reasoner=_Legacy()).reason(_rich_state())
    assert out.decision.decision_type == DecisionType.CONTINUE_OBSERVING


def test_brain_end_to_end_provider_outage_not_a_market_view():
    # Full path: a real LLMReasoner whose provider is down (complete() -> None).
    r = LLMReasoner(client=_StubClient(None), enabled=True, min_interval_seconds=0)
    out = CognitiveBrain(reasoner=r).reason(_rich_state())
    assert out.decision.decision_type == DecisionType.REASONER_UNAVAILABLE
