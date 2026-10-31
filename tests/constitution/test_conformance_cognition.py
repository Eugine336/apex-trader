"""Constitutional conformance suite — cognition layer (Part XXV / §XXIX).

These tests encode the constitution's non-negotiable properties as executable
assertions over the pure-standard-library ``cognition`` layer (importable with
no third-party deps). Two kinds of test live here:

* ``expect="pass"``  — a property the live code ALREADY satisfies and that the
  redesign must NOT regress (e.g. provider failure never replaces a native market verdict).
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
import time
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


def _rich_state(symbol: str = "XAUUSD", conf: float = 0.9, unc: float = 0.1, target_high: float = 1.110) -> MarketState:
    """A well-covered, low-uncertainty MarketState (so the Brain can act)."""
    ms = MarketState(symbol=symbol)
    stamp = time.time()
    for i, dom in enumerate(_DOMAINS):
        ms.add(Evidence(
            source_module=f"module_{i}", domain=dom, symbol=symbol,
            observation="instrument reading", confidence=conf, uncertainty=unc,
            polarity=0.0, relevance_horizon_seconds=900.0,
        ))
    # Positive-path fixtures must explicitly establish council coverage; an
    # unconfirmed advisor quorum is correctly non-actionable in production.
    ms.add(Evidence(
        source_module="reasoning_engine.test_panel", domain=EvidenceDomain.REASONING,
        symbol=symbol, observation="two independent advisors returned opinions",
        confidence=conf, uncertainty=unc, polarity=0.0,
        measurements={"advisors_responded": 2, "advisors_available": 2, "advisors_total": 2},
        relevance_horizon_seconds=900.0,
    ))
    # Entry-path tests must carry a fresh, explicit spread/slippage estimate.
    # A diagnostic default is not verified execution-cost evidence and must not
    # authorize exposure; negative-EV tests need this evidence too so they reach
    # the EV verdict rather than stopping earlier at the cost-verification gate.
    ms.add(Evidence(
        source_module="execution_cost.test_quote",
        domain=EvidenceDomain.EXECUTION_QUALITY,
        symbol=symbol,
        observation="fresh test spread and slippage quote",
        confidence=conf,
        uncertainty=unc,
        measurements={"estimated_total_cost_r": 0.05, "cost_verified": True, "cost_source": "simulated live quote for test"},
        timestamp_epoch=stamp, source_timestamp_epoch=stamp,
        relevance_horizon_seconds=60.0,
    ))
    # Activated-opportunity tests need fresh, market-derived payoff geometry;
    # generic domain coverage alone must not bypass the structural qualification gate.
    ms.add(Evidence(
        source_module="market_lenses.m5.structure",
        domain=EvidenceDomain.STRUCTURE, symbol=symbol,
        observation="completed-bar structural range", confidence=conf, uncertainty=unc,
        measurements={
            "source_kind": "market_lens", "source_timestamp_known": True,
            "data_basis": "completed_ohlc",
            "source_bar_epoch": time.time(),
            "timeframe": "M5", "current_close": 1.100,
            "prior_range_high": target_high, "prior_range_low": 1.095,
            "range_position": 0.5, "breakout_direction": "up",
            "trend_state": "uptrend", "range_rotation": False, "trend_strength": 0.4,
        },
        relevance_horizon_seconds=900.0,
    ))
    ms.add(Evidence(
        source_module="market_lenses.m5.momentum",
        domain=EvidenceDomain.MOMENTUM, symbol=symbol,
        observation="completed-bar momentum", confidence=conf, uncertainty=unc,
        measurements={
            "source_kind": "market_lens", "source_timestamp_known": True,
            "data_basis": "completed_ohlc",
            "source_bar_epoch": time.time(),
            "timeframe": "M5", "close_change": 0.001,
        },
        relevance_horizon_seconds=900.0,
    ))
    # Test-only calibration record; production evidence must come from durable outcomes.
    from test_support.calibration import register_test_calibration
    artifact = register_test_calibration(symbol, "LONG", horizon_seconds=300, probability=conf)
    ms.add(Evidence(
        source_module="calibration_producer.test_fixture",
        domain=EvidenceDomain.HISTORICAL_ANALOGUE, symbol=symbol,
        observation="producer-registered test calibration artifact",
        confidence=0.0, uncertainty=0.0,
        measurements=artifact.to_measurements(),
        timestamp_epoch=artifact.issued_at_epoch,
        source_timestamp_epoch=artifact.issued_at_epoch,
        relevance_horizon_seconds=3600.0,
    ))    return ms




def _weak_state(symbol: str = "XAUUSD") -> MarketState:
    """Thin evidence: native reasoning should observe rather than invent a trade."""
    ms = MarketState(symbol=symbol)
    for domain in (EvidenceDomain.STRUCTURE, EvidenceDomain.MOMENTUM):
        ms.add(Evidence(
            source_module=f"weak_{domain.value}", domain=domain, symbol=symbol,
            observation="thin, non-directional measurement", confidence=0.5,
            uncertainty=0.5, polarity=0.0, relevance_horizon_seconds=900.0,
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
        # V-002 — a directional advisor opinion carries a first-class, ACTIVATED
        # opportunity so origination flows through a structured object; these
        # conformance tests exercise the EV / act gate, not the legacy-scalar path.
        opportunities=(
            [{"id": "auto", "direction": direction, "state": "ACTIVE",
              "horizon": "M5",
              "quality": confidence, "asymmetry": confidence,
              "evidence_strength": confidence}]
            if str(direction).upper() in ("LONG", "SHORT") else []
        ),
        preferred_opportunity_id="auto",
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


@conformance("Part XXVI: advisor failure must not replace a native observe verdict")
def test_provider_unavailable_preserves_native_observe_verdict():
    out = CognitiveBrain(reasoner=_Reasoner(_opinion(), available=False)).reason(_weak_state())
    assert out.decision.decision_type == DecisionType.CONTINUE_OBSERVING
    assert out.decision.authorises_action is False
    assert out.campaign is None
    assert out.provider_status == "unavailable"
    assert out.provider_unavailable is True
    assert out.native_verdict == DecisionType.CONTINUE_OBSERVING.value


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
    # EV_R = p_win*reward_r - (1-p_win)*risk_r - verified_cost_r
    # = 0.8*2.375 - 0.2*1 - 0.05 = 1.65. The rich fixture deliberately
    # supplies fresh execution-cost evidence, so cognition must account for it.
    assert abs(ev - 1.65) < 1e-9, f"expected 1.65 after verified cost, got {ev}"
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
    "Part XXVI: provider infrastructure status is independent of native market verdict",
)
def test_provider_status_is_independent_of_native_market_verdict():
    out = CognitiveBrain(reasoner=_Reasoner(_opinion(), available=False)).reason(_weak_state())
    assert out.decision.decision_type == DecisionType.CONTINUE_OBSERVING
    assert out.provider_status == "unavailable"
    assert out.native_verdict == DecisionType.CONTINUE_OBSERVING.value
    assert out.provider_unavailable is True
    assert out.decision.authorises_action is False
    assert out.campaign is None


@conformance(
    "§IX Q35/Q36: a confident opportunity with non-positive EV is declined, not opened",
)
def test_negative_ev_opportunity_is_declined():
    # reward:risk of 0.5 ⇒ EV at p=0.6 = 0.6*0.5 - 0.4*1 = -0.1 (< 0), so even
    # though confidence clears the bar, the opportunity is declined on EV.
    brain = CognitiveBrain(
        reasoner=_Reasoner(_opinion("LONG", 0.6)),
        reward_r_default=0.5, min_expected_value=0.0,
    )
    out = brain.reason(_rich_state(conf=0.6, target_high=1.1025))
    assert out.decision.decision_type == DecisionType.REJECT_OPPORTUNITY
    assert out.campaign is None
    assert out.decision.expected_value < 0.0


@conformance("§IX: an opportunity that clears the EV threshold still opens")
def test_positive_ev_opportunity_opens():
    brain = CognitiveBrain(
        reasoner=_Reasoner(_opinion("LONG", 0.8)),
        reward_r_default=2.0, min_expected_value=0.0,
    )
    out = brain.reason(_rich_state(conf=0.8))
    assert out.decision.decision_type == DecisionType.OPEN_CAMPAIGN
    assert out.decision.expected_value >= 0.0


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
