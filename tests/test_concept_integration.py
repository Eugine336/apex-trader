"""Concept readings flow from candles -> evidence -> hypotheses -> geometry (live-shaped input)."""

import random
import time

import pandas as pd
import pytest

from cognition.brain_reasoning import BrainHypothesis, BrainReasoner
from cognition.contracts import MarketState
from cognition.market_lenses import analyze_market_frames
from cognition.opportunity_geometry import derive_opportunity_geometry

H = 3600


def _frame(rows, symbol="EURUSD"):
    df = pd.DataFrame(rows, columns=["time", "open", "high", "low", "close", "volume"])
    df.attrs.update({
        "source_timestamp_verified": True, "source_symbol": symbol, "source_timestamp_unit": "unix_seconds",
        "source_timestamp_origin": "broker_ohlcv", "source_broker": "mt5",
    })
    return df


def _history(n, forming_open, seed=11):
    """n completed H1 bars ending right before ``forming_open`` (neutral random walk)."""
    from cognition.concepts.crt import _classify

    for s in range(seed, seed + 300):
        rng = random.Random(s)
        px, rows = 1.0800, []
        for i in range(n):
            o = px
            c = px + rng.uniform(-0.0006, 0.0006)
            h = max(o, c) + rng.uniform(0.0001, 0.0004)
            l = min(o, c) - rng.uniform(0.0001, 0.0004)
            rows.append([forming_open - (n - i) * H, o, h, l, c, 100.0])
            px = c
        bars = [dict(open=r[1], high=r[2], low=r[3], close=r[4]) for r in rows[-3:]] + [
            dict(open=1.0820, high=1.0840, low=1.0800, close=1.0810)]
        if all(_classify(a, b) not in ("bullish_crt", "bearish_crt") for a, b in zip(bars, bars[1:])):
            return rows
    raise AssertionError("no neutral seed")


def _crt_frame(bullish=True):
    now = time.time()
    forming = int(now // H) * H
    rows = _history(60, forming - 2 * H)
    c1_open, c2_open = forming - 2 * H, forming - H
    if bullish:
        rows.append([c1_open, 1.0820, 1.0840, 1.0800, 1.0810, 100.0])
        rows.append([c2_open, 1.0810, 1.0825, 1.0795, 1.0818, 100.0])   # sweep low, close inside
    else:
        rows.append([c1_open, 1.0820, 1.0840, 1.0800, 1.0830, 100.0])
        rows.append([c2_open, 1.0830, 1.0845, 1.0815, 1.0822, 100.0])
    rows.append([forming, 1.0818, 1.0819, 1.0817, 1.0818, 5.0])          # forming bar (excluded)
    return _frame(rows)


def _state(frames):
    state = MarketState(symbol="EURUSD")
    for ev in analyze_market_frames("EURUSD", frames):
        state.add(ev)
    return state


def test_candles_produce_a_concept_reading_in_the_live_evidence_path():
    state = _state({"H1": _crt_frame()})
    readings = [e for e in state.evidence if e.measurements.get("source_kind") == "concept_reading"
                and e.measurements.get("concept") == "crt"]
    assert [e.source_module for e in readings] == ["concept.crt.h1"]
    imp = readings[0].measurements["implications"][0]
    assert imp["direction"] == "LONG" and imp["activated"] is True


def test_registered_concept_hypotheses_survive_faulty_optional_strategy_lens(monkeypatch):
    import cognition.brain_reasoning as reasoning

    state = _state({"H1": _crt_frame()})
    reasoner = BrainReasoner()
    analysis = reasoner.analyze_evidence(state)

    class BrokenLens:
        key = "broken_optional_lens"

    monkeypatch.setattr(reasoning, "STRATEGY_LENSES", (BrokenLens(),))
    analysis.strategy_lenses = [{"key": "broken_optional_lens", "status": "relevant"}]

    hypotheses = reasoner.generate_hypotheses(analysis, "EURUSD")
    assert any(
        hypothesis.hypothesis_kind == "concept_group:h1:long:active"
        and "crt" in hypothesis.supporting_patterns
        for hypothesis in hypotheses
    )


def test_brain_forms_a_hypothesis_carrying_the_concepts_own_invalidation():
    state = _state({"H1": _crt_frame()})
    reasoner = BrainReasoner()
    analysis = reasoner.analyze_evidence(state)
    hyps = reasoner.generate_hypotheses(analysis, "EURUSD")
    concept = [
        h for h in hyps
        if h.hypothesis_kind == "concept_group:h1:long:active"
        and "crt" in h.supporting_patterns
    ]
    assert len(concept) == 1
    h = concept[0]
    assert h.direction_implication == "LONG"
    assert "below the sweep extreme" in h.invalidation
    assert h.confidence_components["calibrated_probability"] is False
    assert h.supporting_evidence_ids and h.supporting_evidence_ids[0]
    assert any("lower-timeframe" in u for u in h.unresolved_unknowns)
    serialized = analysis.to_dict()
    assert serialized["concept_readings"]
    assert serialized["concept_readings"][0]["source_timestamp_verified"] is True



def test_brain_challenges_a_concept_hypothesis_when_its_trigger_is_not_satisfied():
    state = _state({"H1": _crt_frame()})
    reasoner = BrainReasoner()
    analysis = reasoner.analyze_evidence(state)
    reading = next(item for item in analysis.concept_readings if item["concept"] == "crt")
    reading["implications"] = [{
        "direction": "LONG",
        "activated": False,
        "requires": ["lower-timeframe structure shift"],
        "conflicts_with": [],
    }]
    hypothesis = BrainHypothesis(
        statement="CRT suggests a possible long setup",
        confidence=0.8,
        direction_implication="LONG",
        hypothesis_kind=f"concept:crt:{reading['timeframe'].lower()}",
        supporting_evidence_ids=[reading["evidence_id"]],
    )

    (challenged,) = reasoner.self_criticize([hypothesis], analysis)

    assert challenged.confidence < 0.8
    assert "trigger is not currently satisfied" in challenged.criticism


def test_brain_challenges_a_concept_with_an_opposing_same_bar_concept_reading():
    state = _state({"H1": _crt_frame()})
    reasoner = BrainReasoner()
    analysis = reasoner.analyze_evidence(state)
    reading = next(item for item in analysis.concept_readings if item["concept"] == "crt")
    reading["implications"] = [{
        "direction": "LONG",
        "activated": True,
        "requires": [],
        "conflicts_with": [],
    }]
    analysis.concept_readings.append({
        **reading,
        "evidence_id": "opposing-concept-evidence",
        "concept": "market_structure",
        "implications": [{
            "direction": "SHORT",
            "activated": True,
            "requires": [],
            "conflicts_with": [],
        }],
    })
    hypothesis = BrainHypothesis(
        statement="CRT suggests a possible long setup",
        confidence=0.8,
        direction_implication="LONG",
        hypothesis_kind=f"concept:crt:{reading['timeframe'].lower()}",
        supporting_evidence_ids=[reading["evidence_id"]],
    )

    (challenged,) = reasoner.self_criticize([hypothesis], analysis)

    assert challenged.confidence < 0.8
    assert "same-timeframe, same-source-bar concept interpretation opposes" in challenged.criticism

def test_geometry_comes_from_the_concept_not_from_a_default_multiple(monkeypatch):
    import cognition.concepts as concept_registry
    monkeypatch.setattr(
        concept_registry, "CONCEPT_MODULES",
        tuple(module for module in concept_registry.CONCEPT_MODULES if module.key == "crt"),
    )
    state = _state({"H1": _crt_frame()})
    geo = derive_opportunity_geometry(state, "LONG")
    assert geo is not None and geo.concept == "crt" and geo.concept_timeframe == "H1"
    assert geo.stop_price == pytest.approx(1.0795)      # beyond the sweep extreme
    assert geo.target_price == pytest.approx(1.0840)    # opposite extreme of C1
    assert geo.entry_price == pytest.approx(1.0818)     # latest completed close
    assert geo.milestones == (pytest.approx(1.0820),)
    assert geo.reward_r == pytest.approx((1.0840 - 1.0818) / (1.0818 - 1.0795))
    assert "sweep" in geo.invalidation_reason
    # The opposite side may still be derived by the older structure path, but never by this concept.
    opposite = derive_opportunity_geometry(state, "SHORT")
    assert opposite is None or opposite.concept == ""


def test_bearish_crt_gives_short_geometry(monkeypatch):
    import cognition.concepts as concept_registry
    monkeypatch.setattr(
        concept_registry, "CONCEPT_MODULES",
        tuple(module for module in concept_registry.CONCEPT_MODULES if module.key == "crt"),
    )
    state = _state({"H1": _crt_frame(bullish=False)})
    geo = derive_opportunity_geometry(state, "SHORT")
    assert geo is not None
    assert geo.stop_price == pytest.approx(1.0845) and geo.target_price == pytest.approx(1.0800)
    opposite = derive_opportunity_geometry(state, "LONG")
    assert opposite is None or opposite.concept == ""


def test_unverified_concept_readings_do_not_count_as_current_market_domains():
    rows = []
    for i in range(30):
        opening = 100.0 + (i % 2) * 0.05
        closing = opening + 0.02
        rows.append({
            "open": opening, "high": closing + 0.1, "low": opening - 0.1,
            "close": closing, "volume": 100.0,
        })
    frame = pd.DataFrame(rows)
    # Deliberately omit broker timestamp attestation and source timestamps.
    state = _state({"H1": frame})
    concepts = [e for e in state.evidence
                if e.measurements.get("source_kind") == "concept_reading"]
    assert concepts
    assert all(e.source_timestamp_verified is False for e in concepts)
    assert state.consolidation()["market_domain_count"] == 0


def test_concept_pipeline_import_failure_is_preserved_as_non_market_unknown(monkeypatch):
    import builtins

    original_import = builtins.__import__

    def failing_import(name, *args, **kwargs):
        if name == "cognition.concepts":
            raise ImportError("simulated registry initialization failure")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", failing_import)
    evidence = analyze_market_frames("EURUSD", {"H1": _crt_frame()})
    diagnostics = [
        item for item in evidence
        if item.measurements.get("source_kind") == "concept_runtime_diagnostic"
        and item.measurements.get("concept_module") == "pipeline"
    ]

    assert len(diagnostics) == 1
    diagnostic = diagnostics[0]
    assert diagnostic.source_module == "concept_runtime.pipeline"
    assert diagnostic.domain.value == "other"
    assert diagnostic.confidence == 0.0
    assert diagnostic.uncertainty == 1.0
    assert diagnostic.polarity == 0.0
    assert diagnostic.source_timestamp_verified is False
    assert diagnostic.source_timestamp_epoch is None
    assert diagnostic.provisional is True
    assert diagnostic.measurements["status"] == "pipeline_failed"
    assert diagnostic.measurements["exception_type"] == "ImportError"

def test_faulted_concept_reader_is_preserved_as_runtime_health_evidence(monkeypatch):
    import cognition.concepts as concepts

    class BrokenConcept:
        key = "broken_test_module"

        def read(self, symbol, bars_by_tf):
            raise RuntimeError("fixture failure")

    monkeypatch.setattr(concepts, "CONCEPT_MODULES", (BrokenConcept(),))
    evidence = concepts.run_concepts("XAUUSD", {"H1": []})
    assert len(evidence) == 1
    failure = evidence[0]
    assert failure.source_module == "concept_runtime.broken_test_module"
    assert failure.polarity == 0.0
    assert failure.confidence == 0.0
    assert failure.uncertainty == 1.0
    assert failure.measurements["source_kind"] == "concept_runtime_diagnostic"
    assert failure.measurements["non_market_evidence"] is True
