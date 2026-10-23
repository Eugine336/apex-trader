"""Apex identity and contextual M1 observations."""

import pandas as pd

from cognition.brain import CognitiveBrain
from cognition.brain_reasoning import BrainReasoner
from cognition.contracts import Evidence, EvidenceDomain, MarketState
from cognition.identity import (
    IDENTITY_STATEMENT,
    IDENTITY_VERSION,
    NON_NEGOTIABLE_PRINCIPLES,
    identity_contract,
)
from cognition.pattern_rules import detect_patterns, pattern_name
from cognition.price_action_patterns import (
    detect_m1_price_action,
    evidence_from_m1_candles,
)
from cognition.strategy_knowledge import STRATEGY_LENSES, assess_strategy_lenses


def _m1_frame(*, rejection: bool = True) -> pd.DataFrame:
    rows = []
    for index in range(24):
        # Quiet, overlapping candles establish a small local range.
        rows.append({
            "open": 100.0,
            "high": 100.5,
            "low": 99.5,
            "close": 100.1 if index % 2 == 0 else 100.0,
            "volume": 100.0,
        })
    if rejection:
        rows.append({
            "open": 100.2,
            "high": 100.4,
            "low": 98.7,
            "close": 100.15,
            "volume": 180.0,
        })
    else:
        rows.append({
            "open": 100.0,
            "high": 100.5,
            "low": 99.5,
            "close": 100.1,
            "volume": 100.0,
        })
    return pd.DataFrame(rows)


def test_identity_is_one_exact_machine_readable_contract():
    contract = identity_contract()
    assert IDENTITY_VERSION == "1.0.0"
    assert contract["identity"] == IDENTITY_STATEMENT
    assert len(NON_NEGOTIABLE_PRINCIPLES) >= 10
    assert "does not belong to one strategy, timeframe or trading style" in IDENTITY_STATEMENT
    brain = CognitiveBrain(reasoner=None)
    assert brain.identity_statement == IDENTITY_STATEMENT
    assert brain.identity_version == IDENTITY_VERSION
    assert brain.identity_contract()["identity"] == IDENTITY_STATEMENT


def test_m1_rejection_patterns_become_neutral_provisional_evidence():
    frame = _m1_frame()
    measurements = detect_m1_price_action(frame)
    assert measurements
    assert "pin_bar" in measurements["pattern_tags"]
    assert "range_rejection" in measurements["pattern_tags"]
    assert measurements["near_range_boundary"] is True

    evidence = evidence_from_m1_candles("XAUUSD", frame, provisional=True)
    assert evidence is not None
    assert evidence.source_module == "price_action.m1"
    assert evidence.domain == EvidenceDomain.STRUCTURE
    assert evidence.polarity == 0.0
    assert evidence.measurements["provisional"] is True
    assert evidence.measurements["source_kind"] == "m1_price_action"
    assert "entry" not in evidence.measurements
    assert "stop" not in evidence.measurements
    assert "target" not in evidence.measurements


def test_m1_patterns_require_boundary_context_to_create_native_pattern():
    evidence = evidence_from_m1_candles("XAUUSD", _m1_frame(), provisional=True)
    assert evidence is not None
    found = [pattern_name(item) for item in detect_patterns([evidence])]
    assert "m1_price_action_at_boundary" in found

    # A bland overlapping candle is not promoted to a pattern.
    bland = evidence_from_m1_candles("EURUSD", _m1_frame(rejection=False), provisional=True)
    assert bland is None


def test_native_brain_keeps_m1_reaction_as_a_hypothesis_not_an_order():
    evidence = evidence_from_m1_candles("XAUUSD", _m1_frame(), provisional=True)
    assert evidence is not None
    state = MarketState(symbol="XAUUSD")
    state.add(evidence)
    analysis = BrainReasoner().analyze_evidence(state, state.consolidation())
    hypotheses = BrainReasoner().generate_hypotheses(analysis, "XAUUSD")
    assert any("m1_price_action_at_boundary" in h.supporting_patterns for h in hypotheses)
    assert all(not hasattr(h, "order") for h in hypotheses)


def test_strategy_knowledge_assesses_lenses_without_authorizing_trades():
    evidence = evidence_from_m1_candles("XAUUSD", _m1_frame(), provisional=True)
    assert evidence is not None
    state = MarketState(symbol="XAUUSD")
    state.add(evidence)
    analysis = BrainReasoner().analyze_evidence(state, state.consolidation())
    assert len(analysis.strategy_lenses) == len(STRATEGY_LENSES)
    candle_lens = next(item for item in analysis.strategy_lenses if item["key"] == "candle_price_action")
    assert candle_lens["status"] in {"relevant", "watch"}
    assert "limitation" in candle_lens
    assert "direction" not in candle_lens
    assert "trade" not in candle_lens
    assert any("lenses:" in analysis.summary() for _ in [0])


def test_strategy_lenses_can_mark_missing_data_as_not_evaluable():
    assessments = assess_strategy_lenses([], [], {})
    assert assessments
    assert all(item["status"] == "not_evaluable" for item in assessments)
    assert all(item["limitation"] for item in assessments)
