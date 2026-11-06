from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

import pytest

from cognition.concepts.base import ConceptReading, Implication
from cognition.contracts import DecisionType, EvidenceDomain, Opportunity, OpportunityState
from cognition.hindcast import (
    HindcastDataError,
    HindcastDataset,
    load_hindcast_dataset,
    run_hindcast,
)


def _bar(epoch, *, open_=100.0, high=101.0, low=99.5, close=100.0):
    return {
        "timestamp_epoch": float(epoch),
        "source_timestamp_epoch": float(epoch),
        "source_timestamp_verified": True,
        "provisional": False,
        "open": open_,
        "high": high,
        "low": low,
        "close": close,
        "volume": 10.0,
    }


def _dataset(bars, *, verified=True):
    return HindcastDataset(
        symbol="EURUSD",
        source_reference="broker-history-export",
        attestation_basis="provider-export-metadata-and-checksum",
        timestamp_semantics="bar_close",
        source_timestamp_verified=verified,
        dataset_digest="a" * 64,
        bars_by_tf={"M1": bars},
    )


class _ReadingModule:
    key = "test.reading"
    family = "test"
    covers_methods = ()
    covers_lenses = ()

    def __init__(self):
        self.seen = []

    def read(self, symbol, bars_by_tf):
        bars = bars_by_tf.get("M1", [])
        self.seen.append([bar["timestamp_epoch"] for bar in bars])
        if not bars:
            return []
        last = bars[-1]
        return [ConceptReading(
            concept="test_structure",
            timeframe="M1",
            domain=EvidenceDomain.STRUCTURE,
            applicable=True,
            applicability_reason="test fixture",
            observation="explicit objective and invalidation",
            implications=(Implication(
                direction="LONG",
                statement="test long hypothesis",
                strength=0.72,
                activated=True,
                activation_reason="fixture trigger",
                invalidation_price=99.0,
                objective_price=102.0,
            ),),
            facts={"regime": "trend"},
            source_bar_epoch=last["timestamp_epoch"],
            source_timestamp_verified=True,
            reference_close=last["close"],
            reference_timeframe="M1",
            relevance_horizon_seconds=600.0,
            independence_group="ohlc:EURUSD:M1",
        )]


class _Brain:
    available = True

    def __init__(self, *, order_type="MARKET"):
        self.seen = []
        self.order_type = order_type

    def reason(self, market_state, *, now=None):
        source_times = [
            item.source_timestamp_epoch for item in market_state.evidence
            if item.source_timestamp_verified
        ]
        self.seen.append((float(now), list(source_times)))
        opportunity = Opportunity(
            opportunity_id="opp-1",
            direction="LONG",
            state=OpportunityState.ACTIVE,
            thesis="test opportunity",
            entry_price=100.0,
            stop_price=99.0,
            target_price=102.0,
            candidate_family="continuation",
            order_type=self.order_type,
        )
        decision = SimpleNamespace(
            decision_type=DecisionType.OPEN_CAMPAIGN,
            preferred_opportunity_id="opp-1",
            opportunities=[opportunity],
            confidence=0.7,
            created_epoch=0.0,
            created_iso="",
        )
        return SimpleNamespace(decision=decision)


def test_hindcast_manifest_checks_hash_and_normalizes_bar_open_timestamps(tmp_path):
    csv_path = tmp_path / "EURUSD_M1.csv"
    csv_path.write_text(
        "time,open,high,low,close,volume\n"
        "2026-01-01T00:00:00Z,1.0,1.1,0.9,1.05,100\n"
        "2026-01-01T00:01:00Z,1.05,1.2,1.0,1.1,120\n",
        encoding="utf-8",
    )
    digest = hashlib.sha256(csv_path.read_bytes()).hexdigest()
    manifest = {
        "schema_version": 1,
        "symbol": "EURUSD",
        "source_reference": "broker export 2026-01-01",
        "attestation_basis": "provider export metadata",
        "source_timestamp_verified": True,
        "timestamp_semantics": "bar_open",
        "timestamp_timezone": "UTC",
        "timeframes": {"M1": {"file": csv_path.name, "sha256": digest}},
    }
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    dataset = load_hindcast_dataset(manifest_path)

    assert dataset.source_timestamp_verified is True
    assert dataset.bars_by_tf["M1"][0]["timestamp_epoch"] == (
        datetime(2026, 1, 1, 0, 1, tzinfo=timezone.utc).timestamp()
    )
    assert dataset.bars_by_tf["M1"][0]["source_timestamp_verified"] is True
    assert len(dataset.dataset_digest) == 64


def test_hindcast_manifest_rejects_tampered_file_and_unattested_timestamps(tmp_path):
    csv_path = tmp_path / "EURUSD_M1.csv"
    csv_path.write_text(
        "time,open,high,low,close\n2026-01-01T00:00:00Z,1,1.1,0.9,1\n",
        encoding="utf-8",
    )
    manifest = {
        "schema_version": 1,
        "symbol": "EURUSD",
        "source_reference": "broker export",
        "attestation_basis": "provider metadata",
        "source_timestamp_verified": True,
        "timestamp_semantics": "bar_close",
        "timestamp_timezone": "UTC",
        "timeframes": {"M1": {"file": csv_path.name, "sha256": "0" * 64}},
    }
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(HindcastDataError, match="SHA-256 mismatch"):
        load_hindcast_dataset(path)

    manifest["source_timestamp_verified"] = False
    manifest["timeframes"]["M1"]["sha256"] = hashlib.sha256(csv_path.read_bytes()).hexdigest()
    path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(HindcastDataError, match="source_timestamp_verified"):
        load_hindcast_dataset(path)


def test_hindcast_never_exposes_future_bars_to_concepts_or_brain():
    bars = [
        _bar(60, close=100.0),
        _bar(120, close=100.2, high=103.0),
        _bar(180, close=100.3),
    ]
    brain = _Brain()
    module = _ReadingModule()
    report = run_hindcast(
        _dataset(bars),
        max_horizon_seconds=120,
        base_timeframe="M1",
        brain=brain,
        modules=(module,),
    )
    assert report.decisions == 3
    assert len(brain.seen) == 3
    for cutoff, visible in brain.seen:
        assert all(stamp <= cutoff for stamp in visible)
    assert brain.seen[0] == (60.0, [60.0])
    assert module.seen[0] == [60.0]
    assert module.seen[1] == [60.0, 120.0]


def test_concept_and_brain_labels_use_objective_before_invalidation():
    bars = [
        _bar(60),
        _bar(120, high=103.0, low=100.0, close=102.5),
        _bar(180),
    ]
    report = run_hindcast(
        _dataset(bars),
        max_horizon_seconds=120,
        base_timeframe="M1",
        brain=_Brain(),
        modules=(_ReadingModule(),),
    )
    assert report.concept_outcomes[0]["outcome"] == "OBJECTIVE_FIRST"
    assert report.concept_outcomes[0]["market_state"] == "trend"
    assert report.concept_outcomes[0]["strength_semantics"] == (
        "uncalibrated_reasoning_strength_not_probability"
    )
    assert report.brain_calibration_pairs == [(0.7, True)]


def test_same_bar_objective_and_invalidation_is_ambiguous_not_a_win():
    bars = [
        _bar(60),
        _bar(120, high=103.0, low=98.0, close=100.0),
        _bar(180),
    ]
    report = run_hindcast(
        _dataset(bars),
        max_horizon_seconds=120,
        base_timeframe="M1",
        brain=_Brain(),
        modules=(_ReadingModule(),),
    )
    assert report.concept_outcomes[0]["outcome"] == "AMBIGUOUS_SAME_BAR"
    assert report.brain_calibration_pairs == []
    assert report.ambiguous_brain_candidates == 1


def test_hindcast_excludes_pending_order_opportunities_from_market_fill_labels():
    bars = [_bar(60), _bar(120, high=103.0), _bar(180)]
    report = run_hindcast(
        _dataset(bars),
        max_horizon_seconds=120,
        base_timeframe="M1",
        brain=_Brain(order_type="LIMIT"),
        modules=(_ReadingModule(),),
    )
    assert report.brain_calibration_pairs == []
    assert report.skipped_brain_candidates >= 1


def test_hindcast_seeds_only_resolved_brain_pairs_with_dataset_digest(tmp_path):
    from cognition.hindcast import seed_calibration_tracker
    from cognition.influence import CalibrationTracker

    report = run_hindcast(
        _dataset([_bar(60), _bar(120, high=103.0), _bar(180)]),
        max_horizon_seconds=120,
        base_timeframe="M1",
        brain=_Brain(),
        modules=(_ReadingModule(),),
    )
    tracker = CalibrationTracker(path=tmp_path / "calibration.json")
    assert seed_calibration_tracker(tracker, report) == 1
    status = tracker.get_status()
    assert status["samples"] == 1
    assert status["sources"] == {"hindcast:aaaaaaaaaaaaaaaa": 1}


def test_hindcast_report_does_not_claim_system4_quote_artifacts_from_ohlc():
    report = run_hindcast(
        _dataset([_bar(60), _bar(120, high=103.0)]),
        max_horizon_seconds=120,
        base_timeframe="M1",
        brain=_Brain(),
        modules=(_ReadingModule(),),
    )
    assert "OHLC candles are not broker bid/ask quote evidence" in report.system4_artifact_status


def test_unverified_dataset_cannot_be_used_to_train():
    with pytest.raises(HindcastDataError, match="unverified source timestamps"):
        run_hindcast(
            _dataset([_bar(60)], verified=False),
            max_horizon_seconds=60,
            base_timeframe="M1",
            brain=_Brain(),
            modules=(_ReadingModule(),),
        )
