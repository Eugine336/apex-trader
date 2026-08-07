"""Part XXV guard — no directional reading may reach the Brain.

Verifies the enforced choke point (``MarketState.add`` → ``scrub_directional``):
every Evidence entering the Brain's consolidated state carries ``polarity == 0``
and no directional measurement key, regardless of what any adapter (or a future
one, or injected evidence) tries to set. This is the live-code guarantee behind
"nothing upstream or downstream returns a directional reading."
"""

from cognition.contracts import (
    Evidence,
    EvidenceDomain,
    MarketState,
    scrub_directional,
    _DIRECTIONAL_MEASUREMENT_KEYS,
)


def _directional_evidence():
    return Evidence(
        source_module="rogue_module",
        domain=EvidenceDomain.STRUCTURE,
        symbol="XAUUSD",
        observation="rogue module",
        confidence=0.9,
        uncertainty=0.1,
        polarity=0.8,  # a directional lean that must NOT survive
        measurements={
            "directional_lean": 0.8, "long_probability": 0.7,
            "short_probability": 0.3, "dominant": "LONG", "bias": "up",
            "rsi": 63.2, "sweep_type": "buyside",  # genuine raw measurements kept
        },
    )


def test_market_state_add_strips_direction():
    ms = MarketState(symbol="XAUUSD")
    ms.add(_directional_evidence())
    e = ms.evidence[0]
    assert e.polarity == 0.0
    # directional measurement keys removed, raw ones preserved
    for k in ("directional_lean", "long_probability", "short_probability",
              "dominant", "bias"):
        assert k not in e.measurements
    assert e.measurements["rsi"] == 63.2
    assert e.measurements["sweep_type"] == "buyside"


def test_scrub_directional_is_idempotent_and_fail_safe():
    e = scrub_directional(_directional_evidence())
    assert e.polarity == 0.0
    assert scrub_directional(e).polarity == 0.0  # idempotent
    # never raises on a malformed measurements payload
    e.measurements = None  # type: ignore[assignment]
    assert scrub_directional(e).polarity == 0.0


def test_no_directional_reading_reaches_brain_across_all_sources():
    # Whatever the (possibly rogue) upstream sets, the consolidated state the
    # Brain reads is uniformly non-directional.
    ms = MarketState(symbol="XAUUSD")
    for pol in (0.9, -0.75, 0.5, -1.0):
        ms.add(Evidence(source_module=f"m{pol}", domain=EvidenceDomain.MOMENTUM,
                        symbol="XAUUSD", confidence=0.6, uncertainty=0.4, polarity=pol,
                        measurements={"lean": pol, "value": 0.37}))
    assert all(e.polarity == 0.0 for e in ms.evidence)
    assert all("lean" not in e.measurements for e in ms.evidence)
    # value measurements (raw) survive
    assert all(e.measurements.get("value") == 0.37 for e in ms.evidence)


def test_consolidation_has_no_directional_conflict():
    ms = MarketState(symbol="XAUUSD")
    ms.add(Evidence(source_module="a", domain=EvidenceDomain.STRUCTURE, symbol="XAUUSD",
                    confidence=0.8, uncertainty=0.2, polarity=0.9))
    ms.add(Evidence(source_module="b", domain=EvidenceDomain.MOMENTUM, symbol="XAUUSD",
                    confidence=0.7, uncertainty=0.3, polarity=-0.9))
    cons = ms.consolidation()
    # Opposed leans used to manufacture a directional "conflict"; now there is
    # none — uncertainty is derived non-directionally.
    assert cons["conflict_ratio"] == 0.0
    assert 0.0 <= cons["aggregate_uncertainty"] <= 1.0


def test_directional_key_set_is_declared():
    # Guard against accidental narrowing of the scrub set.
    for k in ("directional_lean", "direction", "bias", "long_probability",
              "short_probability", "long_ev", "short_ev", "dominant", "score"):
        assert k in _DIRECTIONAL_MEASUREMENT_KEYS


# ── Part XXV Art 11 — management-cognition parity ─────────────────────────────

from cognition.brain import CognitiveBrain  # noqa: E402
from cognition.contracts import DecisionType  # noqa: E402
from llm.reasoner import LLMOpinion  # noqa: E402


class _Reasoner:
    def __init__(self, opinion):
        self._op = opinion

    @property
    def available(self):
        return True

    def reason(self, symbol, evidence, now=None):
        return self._op


class _Pos:
    def __init__(self, direction, profit_r=0.0, symbol="XAUUSD"):
        self.direction = direction
        self.profit_r = profit_r
        self.symbol = symbol


def _state():
    ms = MarketState(symbol="XAUUSD")
    ms.add(Evidence(source_module="s", domain=EvidenceDomain.STRUCTURE, symbol="XAUUSD",
                    confidence=0.9, uncertainty=0.1))
    return ms


def _op(direction, confidence, **rich):
    return LLMOpinion(symbol="XAUUSD", direction=direction, confidence=confidence, **rich)


def test_manage_holds_when_thesis_intact_and_records_rich_state():
    brain = CognitiveBrain(reasoner=_Reasoner(_op(
        "LONG", 0.8, primary_hypothesis="pullback absorbed", regime="transitional",
        opportunity="continuation long", opportunity_horizon="minutes",
        expected_value="positive", invalidation="close below swept low")))
    out = brain.manage(_Pos("LONG"), _state())
    assert out.decision.decision_type in (DecisionType.HOLD, DecisionType.SCALE_IN)
    # Article 11 — the rich cognitive state is recorded on the management record.
    q = out.decision.questions_answered
    assert q["what_is_happening"] == "pullback absorbed"
    assert q.get("regime") == "transitional"
    assert "close below swept low" in out.decision.invalidation_conditions


def test_manage_exits_when_opportunity_gone_even_if_direction_aligns():
    # Direction still LONG (aligned) and confident, but the Brain says the
    # opportunity is gone — management must exit on the thesis, not the label.
    brain = CognitiveBrain(reasoner=_Reasoner(_op(
        "LONG", 0.9, opportunity="none", expected_value="negative")))
    out = brain.manage(_Pos("LONG", profit_r=1.0), _state())
    assert out.decision.decision_type == DecisionType.EXIT


def test_manage_minimal_opinion_preserves_prior_behaviour():
    # An empty-field (legacy) opinion must NOT trigger the opportunity/EV exit;
    # an aligned confident read still holds.
    brain = CognitiveBrain(reasoner=_Reasoner(_op("LONG", 0.8)))
    out = brain.manage(_Pos("LONG"), _state())
    assert out.decision.decision_type in (DecisionType.HOLD, DecisionType.SCALE_IN)


# ── Part XXV Art 14 — audit gate ──────────────────────────────────────────────

from cognition.legacy_audit import audit_nondirectional_cognition  # noqa: E402


def test_audit_nondirectional_cognition_is_clean():
    audit = audit_nondirectional_cognition()
    assert audit.scanned_files >= 2
    assert audit.clean, f"directional reading reintroduced: {audit.findings}"
