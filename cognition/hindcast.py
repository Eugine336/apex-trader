"""Point-in-time historical replay for the native concept system and Brain.

The replay consumes caller-attested historical OHLC files. It never fetches data,
creates quotes from candle closes, sends orders, or treats concept strength as a
probability. Concept outcomes are labelled only when their own objective or
invalidation is touched after the decision cutoff. Same-bar double touches are
ambiguous and excluded from calibration.
"""
from __future__ import annotations

import hashlib
import json
import math
import string
from bisect import bisect_right
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from backtest.data import HistoricalDataLoader
from cognition.concepts import read_concept_readings
from cognition.concepts.base import ConceptModule, ConceptReading, reading_to_evidence
from cognition.contracts import MarketState
from cognition.influence import CalibrationTracker

_TIMEFRAME_SECONDS = {
    "M1": 60, "M5": 300, "M15": 900, "M30": 1800,
    "H1": 3600, "H4": 14400, "D1": 86400,
}


class HindcastDataError(ValueError):
    """Historical dataset or provenance manifest is invalid."""


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass(frozen=True)
class HindcastDataset:
    symbol: str
    source_reference: str
    attestation_basis: str
    timestamp_semantics: str
    source_timestamp_verified: bool
    dataset_digest: str
    bars_by_tf: dict[str, list[dict[str, Any]]]


@dataclass
class HindcastReport:
    symbol: str
    source_reference: str
    attestation_basis: str
    dataset_digest: str
    timestamp_semantics: str
    source_timestamp_verified: bool
    base_timeframe: str
    max_horizon_seconds: int
    decision_stride: int
    decisions: int = 0
    decision_faults: int = 0
    concept_readings: int = 0
    concept_reader_faults: int = 0
    concept_implications_seen: int = 0
    concept_implications_unlabelable: int = 0
    concept_implications_not_activated: int = 0
    concept_outcomes: list[dict[str, Any]] = field(default_factory=list)
    brain_calibration_pairs: list[tuple[float, bool]] = field(default_factory=list)
    skipped_brain_candidates: int = 0
    unresolved_brain_candidates: int = 0
    ambiguous_brain_candidates: int = 0
    concept_summary: dict[str, dict[str, Any]] = field(default_factory=dict)
    brain_calibration: dict[str, Any] = field(default_factory=dict)
    system4_artifact_status: str = (
        "NOT_GENERATED: OHLC candles are not broker bid/ask quote evidence; "
        "the producer-owned System 4 artifact gate requires verified quote events."
    )
    faults: list[dict[str, str]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "symbol": self.symbol,
            "source_reference": self.source_reference,
            "attestation_basis": self.attestation_basis,
            "dataset_digest": self.dataset_digest,
            "timestamp_semantics": self.timestamp_semantics,
            "source_timestamp_verified": self.source_timestamp_verified,
            "base_timeframe": self.base_timeframe,
            "max_horizon_seconds": self.max_horizon_seconds,
            "decision_stride": self.decision_stride,
            "decisions": self.decisions,
            "decision_faults": self.decision_faults,
            "concept_readings": self.concept_readings,
            "concept_reader_faults": self.concept_reader_faults,
            "concept_implications_seen": self.concept_implications_seen,
            "concept_implications_unlabelable": self.concept_implications_unlabelable,
            "concept_implications_not_activated": self.concept_implications_not_activated,
            "concept_outcomes": list(self.concept_outcomes),
            "concept_summary": dict(self.concept_summary),
            "brain_calibration_pairs": [
                {"confidence": confidence, "won": won}
                for confidence, won in self.brain_calibration_pairs
            ],
            "brain_calibration": dict(self.brain_calibration),
            "skipped_brain_candidates": self.skipped_brain_candidates,
            "unresolved_brain_candidates": self.unresolved_brain_candidates,
            "ambiguous_brain_candidates": self.ambiguous_brain_candidates,
            "system4_artifact_status": self.system4_artifact_status,
            "faults": list(self.faults),
        }


def load_hindcast_dataset(manifest_path: str | Path) -> HindcastDataset:
    """Load SHA-256 checked OHLC files using an explicit source-time manifest.

    Manifest schema:
      symbol, source_reference, attestation_basis,
      source_timestamp_verified=true, timestamp_semantics=(bar_open|bar_close),
      timestamp_timezone=UTC, timeframes={TF: {file, sha256}}.

    The manifest's source attestation is a caller declaration, not an independent
    authentication of the data vendor. File hashes make the declared dataset
    reproducible and detect post-manifest modification.
    """
    manifest_file = Path(manifest_path).resolve()
    if not manifest_file.is_file():
        raise FileNotFoundError(str(manifest_file))
    try:
        manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise HindcastDataError(f"cannot read manifest: {exc}") from exc
    if not isinstance(manifest, dict) or int(manifest.get("schema_version", 0)) != 1:
        raise HindcastDataError("manifest schema_version must be 1")
    symbol = str(manifest.get("symbol", "") or "").strip().upper()
    source_reference = str(manifest.get("source_reference", "") or "").strip()
    attestation_basis = str(manifest.get("attestation_basis", "") or "").strip()
    semantics = str(manifest.get("timestamp_semantics", "") or "").strip().lower()
    timestamp_timezone = str(manifest.get("timestamp_timezone", "") or "").strip().upper()
    if not symbol or not source_reference or not attestation_basis:
        raise HindcastDataError("symbol, source_reference and attestation_basis are required")
    if semantics not in {"bar_open", "bar_close"}:
        raise HindcastDataError("timestamp_semantics must be bar_open or bar_close")
    if timestamp_timezone != "UTC":
        raise HindcastDataError("timestamp_timezone must be explicitly declared as UTC")
    if manifest.get("source_timestamp_verified") is not True:
        raise HindcastDataError(
            "hindcast training requires explicit source_timestamp_verified=true"
        )
    timeframes = manifest.get("timeframes")
    if not isinstance(timeframes, dict) or not timeframes:
        raise HindcastDataError("manifest timeframes must be a non-empty object")

    root = manifest_file.parent
    loader = HistoricalDataLoader()
    bars_by_tf: dict[str, list[dict[str, Any]]] = {}
    digest_rows = []
    for raw_tf, spec in sorted(timeframes.items()):
        timeframe = str(raw_tf).strip().upper()
        if timeframe not in _TIMEFRAME_SECONDS or not isinstance(spec, dict):
            raise HindcastDataError(f"unsupported timeframe or malformed spec: {raw_tf}")
        filename = str(spec.get("file", "") or "").strip()
        expected_hash = str(spec.get("sha256", "") or "").strip().lower()
        if not filename or len(expected_hash) != 64:
            raise HindcastDataError(f"{timeframe}: file and SHA-256 are required")
        source_file = (root / filename).resolve()
        if not source_file.is_relative_to(root):
            raise HindcastDataError(f"{timeframe}: file escapes manifest directory")
        if not source_file.is_file():
            raise FileNotFoundError(str(source_file))
        actual_hash = _sha256_file(source_file)
        if actual_hash != expected_hash:
            raise HindcastDataError(f"{timeframe}: SHA-256 mismatch for {filename}")
        candles = loader.load(source_file, pair=symbol, validate=True)
        if semantics == "bar_open" and timeframe == "D1":
            raise HindcastDataError(
                "D1 bar-open timestamps require explicit bar-close timestamps because session/DST lengths vary"
            )
        seconds = _TIMEFRAME_SECONDS[timeframe]
        rows = []
        for candle in candles:
            stamp = candle.time.timestamp()
            completed_at = stamp + seconds if semantics == "bar_open" else stamp
            if not math.isfinite(completed_at) or completed_at <= 0.0:
                raise HindcastDataError(f"{timeframe}: invalid candle timestamp")
            rows.append({
                "timestamp_epoch": completed_at,
                "source_timestamp_epoch": completed_at,
                "source_timestamp_verified": True,
                "provisional": False,
                "open": candle.open,
                "high": candle.high,
                "low": candle.low,
                "close": candle.close,
                "volume": candle.volume,
            })
        if any(rows[index]["timestamp_epoch"] <= rows[index - 1]["timestamp_epoch"]
               for index in range(1, len(rows))):
            raise HindcastDataError(f"{timeframe}: normalized close times are not strictly increasing")
        bars_by_tf[timeframe] = rows
        digest_rows.append((timeframe, filename, actual_hash, len(rows)))

    digest_payload = json.dumps({
        "symbol": symbol,
        "source_reference": source_reference,
        "attestation_basis": attestation_basis,
        "timestamp_semantics": semantics,
        "timestamp_timezone": timestamp_timezone,
        "files": digest_rows,
    }, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return HindcastDataset(
        symbol=symbol,
        source_reference=source_reference,
        attestation_basis=attestation_basis,
        timestamp_semantics=semantics,
        source_timestamp_verified=True,
        dataset_digest=hashlib.sha256(digest_payload).hexdigest(),
        bars_by_tf=bars_by_tf,
    )


def _finite(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _market_state_tag(readings: list[ConceptReading]) -> str:
    """Use explicit concept regime facts; never infer a state from price prose."""
    keys = ("market_regime", "regime_label", "regime_name", "regime_state", "regime")
    explicit_labels = set()
    transition_flags = set()
    for reading in readings:
        facts = reading.facts if isinstance(reading.facts, dict) else {}
        for key in keys:
            value = facts.get(key)
            if isinstance(value, str) and value.strip():
                explicit_labels.add(value.strip().lower()[:60])
        transition = facts.get("possible_transition")
        if isinstance(transition, bool):
            transition_flags.add(transition)
    if explicit_labels:
        return "|".join(sorted(explicit_labels))[:120]
    if transition_flags == {True}:
        return "distribution_transition_warning"
    if transition_flags == {False}:
        return "no_distribution_transition_warning"
    if transition_flags == {True, False}:
        return "mixed_distribution_transition_state"
    return "unknown"


def _evaluate_levels(
    future_bars: list[dict[str, Any]],
    *,
    after_epoch: float,
    horizon_seconds: int,
    direction: str,
    objective_price: float,
    invalidation_price: float,
    reference_price: Optional[float] = None,
) -> tuple[str, Optional[float]]:
    """Resolve objective/invalidation chronology from future OHLC only."""
    side = str(direction or "").upper()
    if (
        not math.isfinite(objective_price) or not math.isfinite(invalidation_price)
        or objective_price <= 0.0 or invalidation_price <= 0.0
    ):
        return "INVALID_GEOMETRY", None
    reference = _finite(reference_price)
    if side == "LONG":
        if not invalidation_price < objective_price:
            return "INVALID_GEOMETRY", None
        if reference is not None and not invalidation_price < reference < objective_price:
            return "INVALID_GEOMETRY", None
    elif side == "SHORT":
        if not objective_price < invalidation_price:
            return "INVALID_GEOMETRY", None
        if reference is not None and not objective_price < reference < invalidation_price:
            return "INVALID_GEOMETRY", None
    else:
        return "INVALID_GEOMETRY", None

    deadline = after_epoch + horizon_seconds
    for bar in future_bars:
        stamp = _finite(bar.get("timestamp_epoch"))
        if stamp is None or stamp <= after_epoch:
            continue
        if stamp > deadline:
            break
        high, low = _finite(bar.get("high")), _finite(bar.get("low"))
        if high is None or low is None:
            continue
        if side == "LONG":
            invalidated = low <= invalidation_price
            objective_hit = high >= objective_price
        else:
            invalidated = high >= invalidation_price
            objective_hit = low <= objective_price
        if invalidated and objective_hit:
            return "AMBIGUOUS_SAME_BAR", stamp
        if invalidated:
            return "INVALIDATION_FIRST", stamp
        if objective_hit:
            return "OBJECTIVE_FIRST", stamp
    return "UNRESOLVED", None


def _summarize_concept_outcomes(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        key = "|".join((
            row["concept"], row["timeframe"], row["direction"], row["market_state"],
        ))
        groups[key].append(row)
    summary = {}
    for key, members in sorted(groups.items()):
        objective = sum(item["outcome"] == "OBJECTIVE_FIRST" for item in members)
        invalidation = sum(item["outcome"] == "INVALIDATION_FIRST" for item in members)
        ambiguous = sum(item["outcome"] == "AMBIGUOUS_SAME_BAR" for item in members)
        unresolved = sum(item["outcome"] == "UNRESOLVED" for item in members)
        invalid_geometry = sum(item["outcome"] == "INVALID_GEOMETRY" for item in members)
        resolved = objective + invalidation
        summary[key] = {
            "observations": len(members),
            "resolved": resolved,
            "objective_first": objective,
            "invalidation_first": invalidation,
            "ambiguous_same_bar": ambiguous,
            "unresolved": unresolved,
            "invalid_geometry": invalid_geometry,
            "objective_first_rate": round(objective / resolved, 6) if resolved else None,
            "semantics": "historical descriptive rate; not a calibrated probability or current-regime edge",
        }
    return summary


def _validate_normalized_dataset(dataset: HindcastDataset) -> None:
    if (
        not str(dataset.symbol or "").strip()
        or not str(dataset.source_reference or "").strip()
        or not str(dataset.attestation_basis or "").strip()
        or len(str(dataset.dataset_digest or "")) != 64
        or any(char not in string.hexdigits for char in str(dataset.dataset_digest or ""))
    ):
        raise HindcastDataError("dataset identity, attestation, and SHA-256 digest are required")
    if dataset.timestamp_semantics not in {"bar_open", "bar_close"}:
        raise HindcastDataError("unsupported timestamp semantics")
    if not isinstance(dataset.bars_by_tf, dict) or not dataset.bars_by_tf:
        raise HindcastDataError("dataset has no timeframe bars")
    for timeframe, rows in dataset.bars_by_tf.items():
        if timeframe not in _TIMEFRAME_SECONDS or not isinstance(rows, list):
            raise HindcastDataError(f"invalid timeframe rows: {timeframe}")
        previous = None
        for index, row in enumerate(rows):
            if not isinstance(row, dict):
                raise HindcastDataError(f"{timeframe} row {index} is not an object")
            stamp = _finite(row.get("timestamp_epoch"))
            source_stamp = _finite(row.get("source_timestamp_epoch"))
            if (
                stamp is None or stamp <= 0.0
                or source_stamp is None or abs(source_stamp - stamp) > 1e-6
                or row.get("source_timestamp_verified") is not True
                or row.get("provisional") is not False
                or (previous is not None and stamp <= previous)
            ):
                raise HindcastDataError(f"{timeframe} row {index} has invalid source-time ordering/provenance")
            values = {
                name: _finite(row.get(name))
                for name in ("open", "high", "low", "close")
            }
            if any(value is None or value <= 0.0 for value in values.values()):
                raise HindcastDataError(f"{timeframe} row {index} has invalid OHLC values")
            if (
                values["high"] < max(values["open"], values["close"], values["low"])
                or values["low"] > min(values["open"], values["close"], values["high"])
            ):
                raise HindcastDataError(f"{timeframe} row {index} has inconsistent OHLC values")
            previous = stamp


def run_hindcast(
    dataset: HindcastDataset,
    *,
    max_horizon_seconds: int,
    base_timeframe: str = "M5",
    decision_stride: int = 1,
    brain: Any = None,
    modules: tuple[ConceptModule, ...] | None = None,
) -> HindcastReport:
    """Replay all registered concepts and the native Brain over point-in-time bars.

    Only bars whose completed-at timestamp is <= the decision cutoff are passed
    into readers. Labels inspect bars strictly AFTER that cutoff. A caller may
    inject a Brain/modules for deterministic testing; production defaults use
    the real CognitiveBrain and every registered concept module.
    """
    _validate_normalized_dataset(dataset)
    horizon = int(max_horizon_seconds)
    stride = int(decision_stride)
    base_tf = str(base_timeframe or "").strip().upper()
    if horizon <= 0:
        raise ValueError("max_horizon_seconds must be positive and explicit")
    if stride <= 0:
        raise ValueError("decision_stride must be positive")
    if base_tf not in dataset.bars_by_tf:
        raise HindcastDataError(f"base timeframe {base_tf} is not in the dataset")
    if not dataset.source_timestamp_verified:
        raise HindcastDataError("unverified source timestamps cannot seed a hindcast")
    base_rows = dataset.bars_by_tf[base_tf]
    if not base_rows:
        raise HindcastDataError("base timeframe has no candles")

    if brain is None:
        from cognition.brain import CognitiveBrain
        brain = CognitiveBrain(reasoner=None)

    report = HindcastReport(
        symbol=dataset.symbol,
        source_reference=dataset.source_reference,
        attestation_basis=dataset.attestation_basis,
        dataset_digest=dataset.dataset_digest,
        timestamp_semantics=dataset.timestamp_semantics,
        source_timestamp_verified=dataset.source_timestamp_verified,
        base_timeframe=base_tf,
        max_horizon_seconds=horizon,
        decision_stride=stride,
    )
    timeframe_epochs = {
        tf: [float(row["timestamp_epoch"]) for row in rows]
        for tf, rows in dataset.bars_by_tf.items()
    }
    labelled_readings: set[tuple] = set()
    last_brain_window_end: dict[tuple[str, str, int], float] = {}
    calibration_tracker = CalibrationTracker(min_samples=30, history_limit=100_000)

    for base_index, base_bar in enumerate(base_rows):
        if base_index % stride:
            continue
        cutoff = float(base_bar["timestamp_epoch"])
        visible_bars: dict[str, list[dict[str, Any]]] = {}
        for timeframe, rows in dataset.bars_by_tf.items():
            end = bisect_right(timeframe_epochs[timeframe], cutoff)
            if end:
                visible_bars[timeframe] = rows[:end]
        if not visible_bars:
            continue

        readings, reader_faults = read_concept_readings(
            dataset.symbol,
            visible_bars,
            context={
                "hindcast": True,
                "decision_epoch": cutoff,
                "source_reference": dataset.source_reference,
                "dataset_digest": dataset.dataset_digest,
            },
            modules=modules,
        )
        report.concept_readings += len(readings)
        report.concept_reader_faults += len(reader_faults)
        for fault in reader_faults:
            report.faults.append({
                "stage": "concept_reader",
                "module": fault["module"],
                "exception_type": fault["exception_type"],
                "message": fault["message"],
            })

        market = MarketState(symbol=dataset.symbol, created_epoch=cutoff)
        market.created_iso = datetime.fromtimestamp(
            cutoff, tz=timezone.utc,
        ).isoformat()
        for reading in readings:
            market.add(reading_to_evidence(dataset.symbol, reading))
        try:
            output = brain.reason(market, now=cutoff)
            decision = getattr(output, "decision", None)
            if decision is None:
                raise ValueError("Brain output has no decision package")
            # DecisionPackage defaults to wall-clock creation; overwrite only the
            # replay metadata after reasoning so it records this historical cut.
            decision.created_epoch = cutoff
            decision.created_iso = datetime.fromtimestamp(
                cutoff, tz=timezone.utc,
            ).isoformat()
            report.decisions += 1
        except Exception as exc:  # noqa: BLE001 — retain replay fault and continue
            report.decision_faults += 1
            report.faults.append({
                "stage": "brain_reason",
                "module": "cognitive_brain",
                "exception_type": type(exc).__name__,
                "message": str(exc)[:500],
            })
            continue

        state_tag = _market_state_tag(readings)
        for reading in readings:
            if not reading.applicable:
                continue
            source_epoch = _finite(reading.source_bar_epoch)
            if (
                reading.source_timestamp_verified is not True
                or source_epoch is None or source_epoch > cutoff
            ):
                report.concept_implications_unlabelable += len(reading.implications)
                continue
            relevance = _finite(reading.relevance_horizon_seconds)
            if relevance is not None and relevance > 0.0 and cutoff - source_epoch > relevance:
                continue
            future = dataset.bars_by_tf.get(reading.timeframe, [])
            visible_for_reading = visible_bars.get(reading.timeframe, [])
            if not future or not visible_for_reading:
                continue
            reference_price = _finite(visible_for_reading[-1].get("close"))
            if reference_price is None:
                reference_price = _finite(reading.reference_close)
            if reference_price is None:
                continue
            for implication in reading.implications:
                report.concept_implications_seen += 1
                direction = str(implication.direction or "").upper()
                if direction not in {"LONG", "SHORT"}:
                    report.concept_implications_unlabelable += 1
                    continue
                if not implication.activated:
                    report.concept_implications_not_activated += 1
                    continue
                objective = _finite(implication.objective_price)
                invalidation = _finite(implication.invalidation_price)
                if objective is None or invalidation is None:
                    report.concept_implications_unlabelable += 1
                    continue
                key = (
                    reading.concept, reading.timeframe, source_epoch,
                    direction, objective, invalidation, state_tag,
                )
                if key in labelled_readings:
                    continue
                labelled_readings.add(key)
                outcome, resolved_at = _evaluate_levels(
                    future,
                    after_epoch=cutoff,
                    horizon_seconds=horizon,
                    direction=direction,
                    objective_price=objective,
                    invalidation_price=invalidation,
                    reference_price=reference_price,
                )
                report.concept_outcomes.append({
                    "concept": reading.concept,
                    "timeframe": reading.timeframe,
                    "direction": direction,
                    "market_state": state_tag,
                    "decision_epoch": cutoff,
                    "source_bar_epoch": source_epoch,
                    "independence_group": reading.independence_group or (
                        f"ohlc:{dataset.symbol}:{reading.timeframe}"
                        if reading.data_basis in {"", "completed_ohlc"} else ""
                    ),
                    "objective_price": objective,
                    "invalidation_price": invalidation,
                    "reasoning_strength": _finite(implication.strength),
                    "strength_semantics": "uncalibrated_reasoning_strength_not_probability",
                    "outcome": outcome,
                    "resolved_at_epoch": resolved_at,
                })

        decision_type = getattr(
            getattr(decision, "decision_type", None), "value",
            str(getattr(decision, "decision_type", "")),
        )
        if decision_type != "open_campaign":
            continue
        preferred_id = str(getattr(decision, "preferred_opportunity_id", "") or "")
        opportunities = list(getattr(decision, "opportunities", []) or [])
        selected = next((
            item for item in opportunities
            if preferred_id and str(getattr(item, "opportunity_id", "") or "") == preferred_id
        ), None)
        if selected is None:
            report.skipped_brain_candidates += 1
            continue
        direction = str(getattr(selected, "direction", "") or "").upper()
        order_type = str(getattr(selected, "order_type", "MARKET") or "MARKET").upper()
        stop = _finite(getattr(selected, "stop_price", None))
        target = _finite(getattr(selected, "target_price", None))
        confidence = _finite(getattr(decision, "confidence", None))
        family = str(getattr(selected, "candidate_family", "") or "").strip().lower()
        if (
            direction not in {"LONG", "SHORT"} or order_type != "MARKET"
            or stop is None or target is None
            or confidence is None or family in {"", "unclassified", "unknown"}
        ):
            report.skipped_brain_candidates += 1
            continue
        family_key = (family, direction, horizon)
        if cutoff <= last_brain_window_end.get(family_key, 0.0):
            continue
        # Selection is based on decision time only, never on the eventual label.
        last_brain_window_end[family_key] = cutoff + horizon
        entry = _finite(getattr(selected, "entry_price", None))
        if entry is None:
            entry = _finite(base_bar.get("close"))
        outcome, _ = _evaluate_levels(
            base_rows,
            after_epoch=cutoff,
            horizon_seconds=horizon,
            direction=direction,
            objective_price=target,
            invalidation_price=stop,
            reference_price=entry,
        )
        if outcome == "OBJECTIVE_FIRST":
            report.brain_calibration_pairs.append((confidence, True))
        elif outcome == "INVALIDATION_FIRST":
            report.brain_calibration_pairs.append((confidence, False))
        elif outcome == "AMBIGUOUS_SAME_BAR":
            report.ambiguous_brain_candidates += 1
        elif outcome == "INVALID_GEOMETRY":
            report.skipped_brain_candidates += 1
        else:
            report.unresolved_brain_candidates += 1

    report.concept_summary = _summarize_concept_outcomes(report.concept_outcomes)
    calibration_tracker.observe_many(report.brain_calibration_pairs, source="hindcast")
    report.brain_calibration = calibration_tracker.get_status()
    return report


def seed_calibration_tracker(
    tracker: CalibrationTracker,
    report: HindcastReport,
) -> int:
    """Persist only outcome pairs from a source-time-attested hindcast report."""
    if not report.source_timestamp_verified or not report.dataset_digest:
        return 0
    source = f"hindcast:{report.dataset_digest[:16]}"
    return tracker.observe_many(report.brain_calibration_pairs, source=source)


__all__ = [
    "HindcastDataError", "HindcastDataset", "HindcastReport",
    "load_hindcast_dataset", "run_hindcast", "seed_calibration_tracker",
]
