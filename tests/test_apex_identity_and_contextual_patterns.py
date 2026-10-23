"""Apex identity and contextual M1 observations."""

from datetime import datetime, timedelta, timezone

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
from cognition.market_lenses import analyze_market_frames
from config import CognitionConfig


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


def test_identity_safe_configuration_defaults_require_authoritative_net_ev_and_broad_evidence(monkeypatch):
    for name in (
        "COGNITION_GATE_MODE",
        "COGNITION_EV_PRIMARY_GATE",
        "COGNITION_MIN_EVIDENCE_DOMAINS",
        "COGNITION_EVENT_MIN_INTERVAL_SECONDS",
    ):
        monkeypatch.delenv(name, raising=False)
    cfg = CognitionConfig()
    assert cfg.gate_mode == "authoritative"
    assert cfg.ev_primary_gate is True
    assert cfg.min_evidence_domains >= 3
    assert cfg.event_min_interval_seconds == 1.0
    assert CognitiveBrain(reasoner=None).min_evidence_domains == 3


def test_m1_price_action_uses_source_bar_time_and_does_not_refresh_stale_data():
    frame = _m1_frame()
    now = datetime.now(timezone.utc)
    frame["time"] = pd.date_range(
        end=now - timedelta(minutes=5),
        periods=len(frame),
        freq="min",
    )
    evidence = evidence_from_m1_candles("XAUUSD", frame, provisional=True)
    assert evidence is not None
    assert evidence.measurements["source_timestamp_known"] is True
    assert evidence.timestamp_epoch < now.timestamp() - 4 * 60
    assert evidence.is_fresh(now.timestamp()) is False


def test_m1_price_action_with_unknown_source_time_is_explicitly_uncertain():
    evidence = evidence_from_m1_candles("XAUUSD", _m1_frame(), provisional=True)
    assert evidence is not None
    assert evidence.measurements["source_timestamp_known"] is False
    assert evidence.confidence == 0.25
    assert evidence.uncertainty == 0.75


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


def _frames_with_breakout() -> pd.DataFrame:
    rows = []
    for index in range(31):
        rows.append({
            "open": 100.0,
            "high": 100.5,
            "low": 99.5,
            "close": 100.1 if index % 2 == 0 else 100.0,
            "tick_volume": 100.0,
        })
    # This is the latest completed candle; a final forming row follows it.
    rows[-1] = {
        "open": 100.1,
        "high": 101.8,
        "low": 100.0,
        "close": 101.6,
        "tick_volume": 220.0,
    }
    rows.append({
        "open": 101.6, "high": 102.0, "low": 101.3, "close": 101.8,
        "tick_volume": 140.0,
    })
    return pd.DataFrame(rows)


def _frames_with_liquidity_reclaim() -> pd.DataFrame:
    rows = []
    for index in range(31):
        rows.append({
            "open": 100.0,
            "high": 100.5,
            "low": 99.5,
            "close": 100.1 if index % 2 == 0 else 100.0,
            "volume": 100.0,
        })
    rows[-1] = {
        "open": 99.9,
        "high": 100.2,
        "low": 98.6,
        "close": 100.0,
        "volume": 160.0,
    }
    rows.append({
        "open": 100.0, "high": 100.2, "low": 99.8, "close": 100.05,
        "volume": 120.0,
    })
    return pd.DataFrame(rows)


def test_universal_market_lenses_emit_structured_facts_for_all_supported_timeframes():
    frame = _frames_with_breakout()
    evidence = analyze_market_frames(
        "EURUSD",
        {"M5": frame, "H1": frame},
        source_includes_forming_bar=True,
    )
    assert evidence
    assert {item.domain for item in evidence} >= {
        EvidenceDomain.STRUCTURE,
        EvidenceDomain.MOMENTUM,
        EvidenceDomain.VOLATILITY,
        EvidenceDomain.VOLUME,
    }
    structure = next(
        item for item in evidence
        if item.domain == EvidenceDomain.STRUCTURE
        and item.measurements.get("timeframe") == "M5"
    )
    assert structure.measurements["bos"] is True
    assert structure.measurements["provisional"] is False
    assert structure.measurements["source_timestamp_known"] is False
    assert structure.confidence < 0.7
    assert structure.polarity == 0.0
    assert "direction" not in structure.measurements
    assert "entry" not in structure.measurements
    assert "stop" not in structure.measurements
    assert "target" not in structure.measurements
    assert any(
        item.domain == EvidenceDomain.VOLATILITY
        and item.measurements.get("expansion") is True
        for item in evidence
    )


def test_universal_market_lenses_report_sweep_reclaim_without_claiming_absorption():
    frame = _frames_with_liquidity_reclaim()
    evidence = analyze_market_frames(
        "XAUUSD", {"M5": frame}, source_includes_forming_bar=True,
    )
    sweep = next(item for item in evidence if item.domain == EvidenceDomain.LIQUIDITY)
    assert sweep.measurements["sweep"] is True
    assert sweep.measurements["sweep_side"] == "sell"
    assert sweep.measurements["reclaimed_boundary"] == "prior_range_low"
    assert sweep.measurements["absorption_confirmed"] is False
    assert sweep.polarity == 0.0


def test_native_rules_create_multiple_market_hypotheses_from_completed_bar_evidence():
    frames = _frames_with_breakout()
    evidence = analyze_market_frames(
        "XAUUSD",
        {"M5": frames, "H1": frames},
        source_includes_forming_bar=True,
    )
    state = MarketState(symbol="XAUUSD")
    for item in evidence:
        state.add(item)
    reasoner = BrainReasoner()
    analysis = reasoner.analyze_evidence(state, state.consolidation())
    pattern_names = [pattern_name(item) for item in analysis.detected_patterns]
    assert "structural_break" in pattern_names
    assert "volatility_expansion_breakout" in pattern_names
    hypotheses = reasoner.generate_hypotheses(analysis, "XAUUSD")
    assert len(hypotheses) >= 2
    assert any(h.direction_implication in {"LONG", "SHORT"} for h in hypotheses)
    assert any("counter" in h.opportunity_implication.lower() for h in hypotheses)


def test_market_lens_evidence_expires_from_original_broker_candle_time():
    frame = _frames_with_breakout()
    now = datetime.now(timezone.utc)
    frame["time"] = pd.date_range(
        end=now - timedelta(hours=2),
        periods=len(frame),
        freq="5min",
    )
    evidence = analyze_market_frames(
        "EURUSD", {"M5": frame}, source_includes_forming_bar=True,
    )
    structure = next(
        item for item in evidence
        if item.domain == EvidenceDomain.STRUCTURE
        and item.measurements.get("timeframe") == "M5"
    )
    assert structure.measurements["source_timestamp_known"] is True
    assert structure.timestamp_epoch < now.timestamp() - 2 * 3600 + 1
    assert structure.is_fresh(now.timestamp()) is False
