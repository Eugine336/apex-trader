"""APEX TRADER — PROVIDER_RESILIENCE fixes (V-013, V-016, V-017).

Stdlib-only (``cognition.*`` is pure standard library) so these run without the
optional LLM/runtime dependencies. Executable assertions for:

* V-013 — council degradation is ENFORCED, never a silent quorum pass: unknown
  council coverage is recorded/flagged as degraded (advisor_quorum UNCONFIRMED)
  and, when opted in, attenuates the effective confidence.
* V-016 — provider unavailability is distinguishable from a FLAT market read
  (``direction`` is ``None`` and ``provider_unavailable`` is True).
* V-017 — a non-authoritative CognitionGate never passes silently on absent
  cognition (it fails open but records the reason).
"""

from cognition.brain import CognitiveBrain
from cognition.contracts import (
    DecisionType,
    Evidence,
    MarketState,
)
from cognition.gate import (
    MODE_AUTHORITATIVE,
    MODE_OFF,
    MODE_SHADOW,
    CognitionGate,
)


# ── reasoner / opinion stubs ────────────────────────────────────────────────

class _Opinion:
    def __init__(self, direction="LONG", confidence=0.8):
        self.direction = direction
        self.confidence = confidence
        self.rationale = "scripted"


class _Reasoner:
    available = True

    def __init__(self, opinion):
        self._op = opinion

    def reason(self, symbol, evidence, now=None):
        return self._op


class _DownReasoner:
    """Wired but its provider is unavailable (infrastructure down)."""

    available = False

    def reason(self, symbol, evidence, now=None):
        return None


def _no_council_state(symbol="XAUUSD", conf=0.9):
    """Strong directional evidence but NO reasoning Evidence carrying advisor
    counts — i.e. council coverage is unknown."""
    ms = MarketState(symbol=symbol)
    for dom in ("structure", "momentum"):
        ms.add(Evidence(source_module=f"m_{dom}", domain=dom, symbol=symbol,
                        observation="reading", confidence=conf, uncertainty=0.1,
                        relevance_horizon_seconds=900.0))
    return ms


def _weak_state(symbol="EURUSD"):
    ms = MarketState(symbol=symbol)
    ms.add(Evidence(source_module="m", domain="momentum", symbol=symbol,
                    observation="weak", confidence=0.5, uncertainty=0.5,
                    relevance_horizon_seconds=900.0))
    return ms


# ── V-013 — unknown council is degraded, never a silent quorum pass ──────────

def test_v013_unknown_council_recorded_as_degraded_not_silent():
    brain = CognitiveBrain(reasoner=_Reasoner(_Opinion("LONG", 0.8)))
    out = brain.reason(_no_council_state())
    qa = out.decision.questions_answered
    assert qa.get("cognitive_degradation", "").startswith("OBSERVED")
    assert qa.get("advisor_quorum", "").startswith("UNCONFIRMED")


def test_v013_unknown_council_not_penalised_by_default():
    # Library default (degrade_on_unknown_council=False): the state is recorded as
    # degraded but confidence is preserved, so a Brain deliberately wired without a
    # council is not blanket-penalised.
    brain = CognitiveBrain(reasoner=_Reasoner(_Opinion("LONG", 0.8)))
    out = brain.reason(_no_council_state())
    assert abs(out.decision.confidence - 0.8) < 1e-6
    assert out.decision.decision_type == DecisionType.OPEN_CAMPAIGN


def test_v013_unknown_council_penalised_when_enabled():
    brain = CognitiveBrain(reasoner=_Reasoner(_Opinion("LONG", 0.8)),
                           degrade_on_unknown_council=True,
                           degraded_confidence_multiplier=0.7)
    out = brain.reason(_no_council_state())
    assert abs(out.decision.confidence - 0.56) < 1e-6  # 0.8 x 0.7
    assert out.decision.questions_answered.get(
        "cognitive_degradation", "").startswith("OBSERVED")


# ── V-016 — provider unavailability is not a FLAT market read ────────────────

def test_v016_provider_unavailable_direction_is_none():
    brain = CognitiveBrain(reasoner=_DownReasoner())
    out = brain.reason(_weak_state())
    assert out.decision.decision_type == DecisionType.REASONER_UNAVAILABLE
    assert out.provider_unavailable is True
    assert out.direction is None            # NOT "FLAT"
    assert out.decision_type == "reasoner_unavailable"
    assert out.to_dict()["provider_unavailable"] is True


def test_v016_flat_market_read_is_distinct_from_unavailable():
    # A genuine no-edge market read keeps direction=FLAT and is NOT unavailable.
    brain = CognitiveBrain(reasoner=None)   # no reasoner configured (benign)
    out = brain.reason(_weak_state())
    assert out.provider_unavailable is False
    assert out.direction == "FLAT"


# ── V-017 — the gate never passes silently on absent cognition ───────────────

def test_v017_gate_off_allows():
    assert CognitionGate(None, mode=MODE_OFF).evaluate("EURUSD", "LONG").allow is True


def test_v017_shadow_absent_brain_fails_open_but_records():
    v = CognitionGate(None, mode=MODE_SHADOW).evaluate("EURUSD", "LONG")
    assert v.allow is True
    assert "fail-open" in v.reason


def test_v017_authoritative_absent_brain_fails_closed():
    v = CognitionGate(None, mode=MODE_AUTHORITATIVE).evaluate("EURUSD", "LONG")
    assert v.allow is False
