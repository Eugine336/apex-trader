"""Constitutional conformance suite — cognition layer (Part XXV / §XXIX).

These tests encode the constitution's non-negotiable properties as executable
assertions over the pure-standard-library ``cognition`` layer (importable with
no third-party deps). Two kinds of test live here:

* ``expect="pass"``  — a property the live code ALREADY satisfies and that the
  redesign must NOT regress (e.g. provider failure never becomes a FLAT trade).
* ``expect="xfail"`` — a TARGET property that the current code violates. It is
  registered as a strict-xfail under pytest, so the day the redesign makes it
  true, pytest reports XPASS→failure, forcing the marker's removal. This is the
  green-ratchet the plan promised ("red today, green at the end").

The module is DUAL-MODE: it is collected by pytest, and it also runs headlessly
with plain ``python`` (no pytest needed) via the ``__main__`` block, so the
harness is verifiable in a minimal sandbox. Adding heavier end-to-end coverage
(fake broker + full execution plane) lands with Phase C/D, once the rich
decision path exists.
"""

from __future__ import annotations

import os
import sys
from types import SimpleNamespace

# Make the repo root importable whether this file is collected by pytest (the
# root conftest already handles it) or run headlessly as a plain script (in
# which case the script's own directory would otherwise shadow the repo root).
sys.path.insert(
    0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
)

from cognition.brain import CognitiveBrain
from cognition.contracts import (
    DecisionType,
    Evidence,
    EvidenceDomain,
    MarketState,
)

try:  # pytest is optional so the __main__ runner works in a minimal sandbox
    import pytest
except Exception:  # noqa: BLE001
    pytest = None


# ── dual-mode registry ────────────────────────────────────────────────────
_REGISTRY: "list" = []


def conformance(clause: str, expect: str = "pass", reason: str = ""):
    """Tag a test with its constitution clause + expectation and register it.

    ``expect="xfail"`` additionally applies a strict pytest xfail so a
    now-passing target test fails loudly (prompting marker removal).
    """

    def deco(fn):
        fn.__conformance__ = {"clause": clause, "expect": expect, "reason": reason}
        _REGISTRY.append(fn)
        if pytest is not None and expect == "xfail":
            return pytest.mark.xfail(strict=True, reason=reason)(fn)
        return fn

    return deco


# ── fixtures (plain builders; no pytest fixtures so __main__ can call them) ──
_DOMAINS = (
    EvidenceDomain.STRUCTURE,
    EvidenceDomain.LIQUIDITY,
    EvidenceDomain.MOMENTUM,
    EvidenceDomain.VOLUME,
    EvidenceDomain.ORDER_FLOW,
    EvidenceDomain.VOLATILITY,
)


def _rich_state(symbol: str = "XAUUSD", conf: float = 0.9, unc: float = 0.1) -> MarketState:
    """A well-covered, low-uncertainty MarketState (so the Brain can act)."""
    ms = MarketState(symbol=symbol)
    for i, dom in enumerate(_DOMAINS):
        ms.add(Evidence(
            source_module=f"module_{i}", domain=dom, symbol=symbol,
            observation="instrument reading", confidence=conf, uncertainty=unc,
            polarity=0.0, relevance_horizon_seconds=900.0,
        ))
    return ms


def _opinion(direction: str = "LONG", confidence: float = 0.8, **overrides):
    base = dict(
        direction=direction, confidence=confidence, rationale="scripted",
        competing_hypotheses=[], missing_information=[], primary_hypothesis="",
        alternative_hypotheses=[], what_would_change_my_mind=[], regime="",
        key_uncertainty="", invalidation="", opportunity="", opportunity_horizon="",
        expected_favorable_excursion="", expected_adverse_excursion="",
        expected_value="", execution_quality="", risk="",
        supporting_evidence=[], contradicting_evidence=[],
    )
    base.update(overrides)
    return SimpleNamespace(**base)


class _Reasoner:
    """Duck-typed reasoner matching CognitiveBrain's expectations."""

    def __init__(self, opinion, *, available: bool = True, raises: bool = False):
        self._op = opinion
        self.available = available
        self._raises = raises

    def reason(self, symbol, evidence, now=None):
        if self._raises:
            raise RuntimeError("provider fault")
        return self._op


# ── PASS invariants (must never regress) ────────────────────────────────────

@conformance("§III Art 2 / Part XXV: evidence carries no directional reading (polarity==0)")
def test_market_state_scrubs_directional_evidence():
    ev = Evidence(
        source_module="rogue", observation="leaning LONG", confidence=0.7,
        polarity=0.9,
        measurements={"direction": "LONG", "long_probability": 0.8, "rsi": 55},
    )
    ms = MarketState(symbol="XAUUSD")
    ms.add(ev)
    got = ms.evidence[0]
    assert got.polarity == 0.0, "polarity must be neutralised on entry to MarketState"
    assert "direction" not in got.measurements
    assert "long_probability" not in got.measurements
    assert got.measurements.get("rsi") == 55, "non-directional measurements must survive"


@conformance("§XVIII Q80/Q106: provider failure must NOT become a FLAT market conclusion/trade")
def test_provider_unavailable_is_not_a_flat_trade():
    brain = CognitiveBrain(reasoner=_Reasoner(_opinion(), available=False))
    out = brain.reason(_rich_state())
    assert out.decision.decision_type == DecisionType.CONTINUE_OBSERVING
    assert out.campaign is None, "an unavailable reasoner must not open a campaign"
    assert out.direction == "FLAT"


@conformance("§XVIII Q81/Q104: a None/errored reasoner degrades to observe, never a trade")
def test_reasoner_none_or_exception_degrades_to_observe():
    for reasoner in (_Reasoner(None), _Reasoner(_opinion(), raises=True)):
        out = CognitiveBrain(reasoner=reasoner).reason(_rich_state())
        assert out.decision.decision_type == DecisionType.CONTINUE_OBSERVING
        assert out.campaign is None


@conformance("sanity: a confident opinion over rich evidence DOES open a campaign")
def test_confident_opinion_opens_campaign():
    out = CognitiveBrain(reasoner=_Reasoner(_opinion("LONG", 0.8))).reason(_rich_state())
    assert out.decision.decision_type == DecisionType.OPEN_CAMPAIGN
    assert out.campaign is not None and out.campaign.direction == "LONG"


# ── XFAIL targets (established by the redesign; strict ratchet) ──────────────

@conformance(
    "§IX Q13/Q17: expected value is a computed EV in R, not the confidence scalar",
)
def test_expected_value_is_not_a_confidence_proxy():
    brain = CognitiveBrain(reasoner=_Reasoner(_opinion("LONG", 0.8)), reward_r_default=2.0)
    out = brain.reason(_rich_state())
    assert out.decision.decision_type == DecisionType.OPEN_CAMPAIGN
    ev = out.decision.expected_value
    # EV_R = p_win*reward_r - (1-p_win)*risk_r = 0.8*2 - 0.2*1 = 1.4
    assert abs(ev - 1.4) < 1e-9, f"expected 1.4, got {ev}"
    assert ev != out.decision.confidence


@conformance(
    "§V Q14/§VI: the decision preserves multiple competing hypotheses (no single-direction collapse)",
)
def test_decision_preserves_competing_hypotheses():
    op = _opinion(
        "LONG", 0.8,
        competing_hypotheses=["bullish breakout", "bull trap into sweep"],
        alternative_hypotheses=["bullish breakout", "bull trap into sweep"],
    )
    out = CognitiveBrain(reasoner=_Reasoner(op)).reason(_rich_state())
    hyps = out.decision.hypotheses
    assert hyps is not None and len(hyps) >= 2
    primary = hyps[0]
    assert primary.direction == "LONG"
    assert abs(primary.probability - 0.8) < 1e-9


@conformance(
    "§XVIII Q78: 'reasoner unavailable' must be a state distinct from a market 'observe'",
    expect="xfail", reason="Phase F-1: add a distinct REASONER_UNAVAILABLE brain state",
)
def test_reasoner_unavailable_is_a_distinct_state():
    out = CognitiveBrain(reasoner=_Reasoner(_opinion(), available=False)).reason(_rich_state())
    distinct = (
        out.decision.decision_type.value == "reasoner_unavailable"
        or getattr(out, "reasoner_unavailable", False) is True
    )
    assert distinct, "provider-unavailable must be distinguishable from a genuine observe"


# ── headless runner (no pytest required) ────────────────────────────────────

def _main() -> int:
    rows = []
    all_as_expected = True
    for fn in _REGISTRY:
        meta = fn.__conformance__
        expect = meta["expect"]
        err = None
        try:
            fn()
        except AssertionError as exc:  # noqa: PERF203 - test harness
            err = exc
        except Exception as exc:  # noqa: BLE001
            err = exc
        if expect == "pass":
            ok = err is None
            status = "PASS" if ok else "FAIL"
        else:  # xfail target
            ok = err is not None
            status = "xfail (expected-red)" if ok else "XPASS (flip to expect='pass'!)"
        all_as_expected = all_as_expected and ok
        rows.append((status, fn.__name__, meta["clause"], err))

    print("=" * 78)
    print("APEX TRADER — cognition conformance (headless)")
    print("=" * 78)
    for status, name, clause, err in rows:
        print(f"[{status:>22}] {name}")
        if status.startswith("FAIL") or status.startswith("XPASS"):
            print(f"      clause: {clause}")
            if err is not None:
                print(f"      detail: {err}")
    npass = sum(1 for s, *_ in rows if s == "PASS")
    nxfail = sum(1 for s, *_ in rows if s.startswith("xfail"))
    print("-" * 78)
    print(
        f"{npass} protected invariant(s) PASS, {nxfail} target(s) correctly red; "
        f"harness {'OK' if all_as_expected else 'BROKEN'}."
    )
    return 0 if all_as_expected else 1


if __name__ == "__main__":
    raise SystemExit(_main())
