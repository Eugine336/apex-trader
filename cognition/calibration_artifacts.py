"""Producer-owned, selection-aware calibration artifacts.

Only this module's CalibrationProducer registers artifacts. Evidence payload flags
or IDs supplied by an advisor are insufficient: the Brain resolves the ID against
the in-process producer registry and rechecks symbol, side, horizon and expiry.
The producer uses durable candidate snapshots and fixed-horizon outcome events.
"""

from __future__ import annotations

import hashlib
import json
import math
import threading
import time
from dataclasses import dataclass
from statistics import mean
from typing import Any, Optional

from cognition.contracts import Evidence, EvidenceDomain

_REGISTRY: dict[str, "CalibrationArtifact"] = {}
_REGISTRY_LOCK = threading.RLock()
_DEFAULT_PRODUCER: Optional["CalibrationProducer"] = None
_DEFAULT_LOCK = threading.Lock()

_CANDIDATE_EVENT = "COGNITIVE_CANDIDATE_SNAPSHOT"
_OUTCOME_EVENT = "COGNITIVE_CANDIDATE_HORIZON_OUTCOME"
_DISPOSITION_EVENT = "COGNITIVE_CANDIDATE_DISPOSITION"


@dataclass(frozen=True)
class CalibrationArtifact:
    artifact_id: str
    symbol: str
    direction: str
    horizon_seconds: int
    # Probability of a favourable directional return at the declared horizon;
    # this is not the probability of hitting a target before a stop.
    favourable_probability: float
    expected_return_r: float
    holdout_expected_return_r: float
    expected_return_gap: float
    sample_count: int
    brier_score: float
    reliability_gap: float
    dataset_digest: str
    issued_at_epoch: float
    expires_at_epoch: float
    walk_forward_folds: int
    selection_coverage: float
    producer_version: str = "candidate-calibration-v1"
    protocol: str = "chronological_base_rate_fixed_horizon_v1"
    producer_attested: bool = True

    def to_measurements(self) -> dict[str, Any]:
        # These fields are explanatory metadata. The Brain additionally requires
        # this exact artifact to exist in the producer-owned registry.
        return {
            "calibration_artifact_id": self.artifact_id,
            "calibration_id": self.artifact_id,
            "calibration_side": self.direction,
            "calibration_horizon_seconds": self.horizon_seconds,
            "calibration_method": self.protocol,
            "calibration_scope": "unconditional_symbol_side_horizon_base_rate",
            "expected_return_method": "development_sample_mean_r",
            "favourable_horizon_probability": self.favourable_probability,
            "probability_semantics": "favourable_directional_return_at_fixed_horizon",
            "expected_return_r": self.expected_return_r,
            "holdout_expected_return_r": self.holdout_expected_return_r,
            "expected_return_gap": self.expected_return_gap,
            "sample_count": self.sample_count,
            "brier_score": self.brier_score,
            "reliability_gap": self.reliability_gap,
            "dataset_digest": self.dataset_digest,
            "walk_forward_folds": self.walk_forward_folds,
            "selection_coverage": self.selection_coverage,
            "probability_calibration_verified": True,
            "source_verified": True,
            "out_of_sample": True,
            "walk_forward": self.walk_forward_folds >= 3,
            "untouched_holdout": True,
            "data_leakage_checked": True,
            "selection_bias_checked": self.selection_coverage >= 0.95,
            "overfitting_checked": True,
            "producer_attested": self.producer_attested,
            "producer_version": self.producer_version,
            "validation_protocol": self.protocol,
        }


def _register_artifact(artifact: CalibrationArtifact) -> None:
    """Internal registration primitive; production callers are CalibrationProducer."""
    if not isinstance(artifact, CalibrationArtifact) or not artifact.producer_attested:
        raise TypeError("only producer-issued CalibrationArtifact instances may be registered")
    with _REGISTRY_LOCK:
        # Keep one active artifact per exact calibration scope. Old artifacts
        # must not accumulate or remain eligible after the producer refreshes.
        for key, current in list(_REGISTRY.items()):
            if (
                current.symbol == artifact.symbol
                and current.direction == artifact.direction
                and current.horizon_seconds == artifact.horizon_seconds
            ):
                _REGISTRY.pop(key, None)
        _REGISTRY[artifact.artifact_id] = artifact


def resolve_calibration_artifact(
    artifact_id: str,
    symbol: str,
    direction: str,
    horizon_seconds: int,
    *,
    now: Optional[float] = None,
) -> Optional[CalibrationArtifact]:
    """Resolve only an artifact issued by this producer in this process."""
    key = str(artifact_id or "").strip()
    if not key:
        return None
    with _REGISTRY_LOCK:
        artifact = _REGISTRY.get(key)
    if artifact is None:
        return None
    current = time.time() if now is None else float(now)
    if (
        artifact.symbol != str(symbol or "").strip().upper()
        or artifact.direction != str(direction or "").strip().upper()
        or artifact.horizon_seconds != int(horizon_seconds)
        or current < artifact.issued_at_epoch
        or current > artifact.expires_at_epoch
        or not artifact.producer_attested
    ):
        return None
    return artifact


class CalibrationProducer:
    """Build simple, auditable base-rate calibrations from the full candidate ledger.

    This is deliberately not a feature-search or model-fitting system. It emits
    symbol/side/horizon favourable-return base rates and development-sample mean
    R after chronological walk-forward checks and a final untouched holdout.
    It cannot establish a predictive edge or current-regime conditionality.
    """

    def __init__(
        self,
        event_store: Any = None,
        *,
        min_samples_per_split: int = 30,
        refresh_interval_seconds: float = 300.0,
        artifact_ttl_seconds: float = 7 * 86400.0,
        max_event_pages: int = 20,
    ) -> None:
        self._store = event_store
        self.min_samples = max(30, int(min_samples_per_split))
        self.refresh_interval = max(30.0, float(refresh_interval_seconds))
        self.artifact_ttl = max(3600.0, float(artifact_ttl_seconds))
        self.max_event_pages = max(1, int(max_event_pages))
        self._last_refresh: dict[str, float] = {}
        self._lock = threading.RLock()

    def _event_store(self) -> Any:
        if self._store is not None:
            return self._store
        from persistence.event_store import get_event_store
        self._store = get_event_store()
        return self._store

    def _query_all(self, event_type: str, symbol: str) -> tuple[list[dict], bool]:
        store = self._event_store()
        query = getattr(store, "query_events", None)
        if not callable(query):
            return [], False
        rows: list[dict] = []
        offset = 0
        complete = False
        for _ in range(self.max_event_pages):
            page = query(
                severity_min="DEBUG",
                event_types=[event_type],
                symbol=symbol,
                limit=10000,
                offset=offset,
            ) or []
            rows.extend(page)
            if len(page) < 10000:
                complete = True
                break
            offset += len(page)
        return rows, complete

    @staticmethod
    def _payload(row: dict) -> dict:
        try:
            value = json.loads(row.get("payload_json") or "{}")
            return value if isinstance(value, dict) else {}
        except (TypeError, ValueError, json.JSONDecodeError):
            return {}

    @staticmethod
    def _valid_point_in_time(payload: dict) -> bool:
        evidence = payload.get("point_in_time_evidence")
        if not isinstance(evidence, list) or not evidence:
            return False
        decision_epoch = float(payload.get("decision_epoch") or 0.0)
        for item in evidence:
            if not isinstance(item, dict):
                return False
            if (
                item.get("source_timestamp_verified") is not True
                or item.get("provisional") is True
            ):
                return False
            stamp = item.get("source_timestamp_epoch")
            try:
                stamp = float(stamp)
            except (TypeError, ValueError, OverflowError):
                return False
            if not math.isfinite(stamp) or stamp <= 0.0 or stamp > decision_epoch:
                return False
            raw_horizon = item.get("relevance_horizon_seconds")
            try:
                horizon = 900.0 if raw_horizon is None else float(raw_horizon)
            except (TypeError, ValueError, OverflowError):
                return False
            if not math.isfinite(horizon):
                return False
            if horizon > 0.0 and decision_epoch - stamp > horizon:
                return False
        return True

    def refresh(self, symbol: str, *, now: Optional[float] = None) -> list[CalibrationArtifact]:
        current = time.time() if now is None else float(now)
        sym = str(symbol or "").strip().upper()
        if not sym:
            return []
        with self._lock:
            last = self._last_refresh.get(sym, 0.0)
            if current - last < self.refresh_interval:
                return []
            self._last_refresh[sym] = current
        try:
            snapshots, snapshots_complete = self._query_all(_CANDIDATE_EVENT, sym)
            outcomes, outcomes_complete = self._query_all(_OUTCOME_EVENT, sym)
            dispositions, dispositions_complete = self._query_all(_DISPOSITION_EVENT, sym)
            if not (snapshots_complete and outcomes_complete and dispositions_complete):
                return []
            candidate_map = {
                str(payload.get("candidate_id", "")): payload
                for payload in (self._payload(row) for row in snapshots)
                if payload.get("candidate_id")
            }
            outcome_map = {}
            for row in outcomes:
                payload = self._payload(row)
                candidate_id = str(payload.get("candidate_id", "") or "")
                horizon = int(payload.get("horizon_seconds", 0) or 0)
                if candidate_id and horizon:
                    outcome_map[(candidate_id, horizon)] = payload
            disposition_map = {}
            for row in dispositions:
                payload = self._payload(row)
                candidate_id = str(payload.get("candidate_id", "") or "")
                if candidate_id:
                    disposition_map[candidate_id] = payload

            groups: dict[tuple[str, str], list[dict]] = {}
            invalid_directional_candidates = 0
            for candidate_id, payload in candidate_map.items():
                candidate = payload.get("candidate") or {}
                quote = payload.get("reference_quote") or {}
                direction = str(
                    candidate.get("direction")
                    or candidate.get("direction_implication")
                    or ""
                ).strip().upper()
                if direction not in {"LONG", "SHORT"}:
                    continue
                candidate_kind = str(payload.get("candidate_kind", "") or "")
                if candidate_kind != "opportunity" and not (
                    candidate_kind == "hypothesis"
                    and candidate.get("calibration_candidate") is True
                ):
                    continue
                try:
                    decision_epoch = float(payload.get("decision_epoch"))
                except (TypeError, ValueError, OverflowError):
                    invalid_directional_candidates += 1
                    continue
                if not math.isfinite(decision_epoch) or decision_epoch <= 0.0:
                    invalid_directional_candidates += 1
                    continue
                try:
                    reference_epoch = float(quote.get("source_timestamp_epoch"))
                    reference_price = float(quote.get("mid"))
                    entry_price = float(candidate.get("entry_price"))
                    stop_price = float(candidate.get("stop_price"))
                    risk_distance = (
                        reference_price - stop_price if direction == "LONG"
                        else stop_price - reference_price
                    )
                    entry_deviation_r = (
                        abs(entry_price - reference_price) / risk_distance
                        if risk_distance > 0.0 else float("inf")
                    )
                    quote_valid = (
                        math.isfinite(reference_epoch)
                        and math.isfinite(reference_price)
                        and math.isfinite(entry_price)
                        and math.isfinite(stop_price)
                        and math.isfinite(risk_distance)
                        and math.isfinite(entry_deviation_r)
                        and 0.0 < reference_epoch <= decision_epoch
                        and reference_price > 0.0
                        and risk_distance > 0.0
                        and entry_deviation_r <= 0.25
                    )
                except (TypeError, ValueError, OverflowError):
                    quote_valid = False
                decision_id = str(payload.get("decision_id", "") or "")
                if not decision_id:
                    invalid_directional_candidates += 1
                    continue
                row = {
                    "candidate_id": candidate_id,
                    "decision_id": decision_id,
                    "decision_epoch": decision_epoch,
                    "direction": direction,
                    "candidate": candidate,
                    "payload": payload,
                    "quote_valid": quote_valid,
                    "risk_distance": risk_distance if quote_valid else None,
                    "provenance_valid": self._valid_point_in_time(payload),
                    "disposition_valid": candidate_id in disposition_map,
                }
                groups.setdefault((decision_id, direction), []).append(row)
            artifacts: list[CalibrationArtifact] = []
            for direction in ("LONG", "SHORT"):
                for horizon in (300, 900, 3600, 14400, 86400):
                    eligible_groups = []
                    total_matured = 0
                    observed_groups = 0
                    missed_groups = 0
                    for (decision_id, side), members in groups.items():
                        if side != direction:
                            continue
                        decision_epoch = min(item["decision_epoch"] for item in members)
                        if decision_epoch + horizon > current:
                            continue
                        total_matured += 1
                        # A candidate only enters the calibration sample when
                        # its quote, feature timestamps and final disposition are
                        # all attributable. Missing members remain in the
                        # denominator and therefore reduce selection coverage.
                        if not all(
                            item["quote_valid"] and item["provenance_valid"]
                            and item["disposition_valid"]
                            for item in members
                        ):
                            continue
                        member_outcomes = [
                            outcome_map.get((item["candidate_id"], horizon))
                            for item in members
                        ]
                        if any(item is None for item in member_outcomes):
                            continue
                        if any(item.get("observation_status") == "MISSED_WINDOW" for item in member_outcomes):
                            missed_groups += 1
                            continue
                        if not all(item.get("observation_status") == "OBSERVED" for item in member_outcomes):
                            continue
                        returns = []
                        for item in member_outcomes:
                            try:
                                value = float(item.get("directional_return_r"))
                            except (TypeError, ValueError, OverflowError):
                                value = float("nan")
                            if math.isfinite(value):
                                returns.append(value)
                        if len(returns) != len(member_outcomes):
                            continue
                        # Multiple opportunities can coexist for the same
                        # decision and side. Their labels may legitimately
                        # differ; aggregate their outcomes at the decision level
                        # instead of discarding the entire decision.
                        ordered_returns = sorted(returns)
                        median_return = (
                            ordered_returns[len(ordered_returns) // 2]
                            if len(ordered_returns) % 2
                            else (
                                ordered_returns[len(ordered_returns) // 2 - 1]
                                + ordered_returns[len(ordered_returns) // 2]
                            ) / 2.0
                        )
                        observed_groups += 1
                        eligible_groups.append({
                            "decision_id": decision_id,
                            "decision_epoch": decision_epoch,
                            "label": 1 if median_return > 0.0 else 0,
                            "return_r": median_return,
                            "candidate_ids": sorted(item["candidate_id"] for item in members),
                        })
                    coverage = (
                        (observed_groups + missed_groups) / total_matured
                        if total_matured else 0.0
                    )
                    missed_rate = missed_groups / max(1, total_matured)
                    if (
                        invalid_directional_candidates > 0
                        or total_matured == 0
                        or coverage < 0.95
                        or missed_rate > 0.05
                        or len(eligible_groups) < self.min_samples * 5
                    ):
                        continue
                    eligible_groups.sort(key=lambda item: item["decision_epoch"])
                    n = len(eligible_groups)
                    holdout_start = int(n * 0.80)
                    holdout = eligible_groups[holdout_start:]
                    if len(holdout) < self.min_samples:
                        continue
                    holdout_start_epoch = holdout[0]["decision_epoch"]
                    # Purge development rows whose fixed-horizon outcome could
                    # overlap the untouched holdout decision window.
                    development = [
                        item for item in eligible_groups[:holdout_start]
                        if item["decision_epoch"] + horizon < holdout_start_epoch
                    ]
                    if len(development) < self.min_samples * 3:
                        continue

                    # Three expanding chronological folds. Each training sample
                    # must have its full fixed horizon before the validation window.
                    folds = []
                    development_size = len(development)
                    for train_fraction in (0.40, 0.55, 0.70):
                        train_end = int(development_size * train_fraction)
                        validation_end = min(
                            development_size,
                            train_end + max(self.min_samples, int(development_size * 0.12)),
                        )
                        if train_end <= 0 or validation_end <= train_end:
                            continue
                        validation = development[train_end:validation_end]
                        if not validation:
                            continue
                        validation_start = validation[0]["decision_epoch"]
                        training = [
                            item for item in development[:train_end]
                            if item["decision_epoch"] + horizon < validation_start
                        ]
                        if len(training) < self.min_samples or len(validation) < self.min_samples:
                            continue
                        probability = mean(item["label"] for item in training)
                        brier = mean(
                            (probability - item["label"]) ** 2 for item in validation
                        )
                        folds.append((probability, brier, len(training), len(validation)))
                    if len(folds) < 3:
                        continue

                    base_probability = mean(item["label"] for item in development)
                    holdout_rate = mean(item["label"] for item in holdout)
                    expected_return_r = mean(item["return_r"] for item in development)
                    holdout_expected_return_r = mean(item["return_r"] for item in holdout)
                    expected_return_gap = abs(expected_return_r - holdout_expected_return_r)
                    holdout_brier = mean(
                        (base_probability - item["label"]) ** 2 for item in holdout
                    )
                    reliability_gap = abs(base_probability - holdout_rate)
                    if (
                        not math.isfinite(base_probability)
                        or not math.isfinite(holdout_brier)
                        or not math.isfinite(reliability_gap)
                        or not math.isfinite(expected_return_r)
                        or not math.isfinite(holdout_expected_return_r)
                        or not math.isfinite(expected_return_gap)
                        or holdout_brier > 0.25
                        or reliability_gap > 0.15
                        or expected_return_gap > 0.50
                    ):
                        continue
                    digest_input = [
                        (item["decision_id"], item["decision_epoch"], item["label"])
                        for item in eligible_groups
                    ]
                    digest = hashlib.sha256(
                        json.dumps(digest_input, sort_keys=True, separators=(",", ":")).encode("utf-8")
                    ).hexdigest()
                    artifact_id = hashlib.sha256(
                        f"{sym}|{direction}|{horizon}|{digest}|chronological_base_rate_fixed_horizon_v1".encode("utf-8")
                    ).hexdigest()
                    artifact = CalibrationArtifact(
                        artifact_id=artifact_id,
                        symbol=sym,
                        direction=direction,
                        horizon_seconds=horizon,
                        favourable_probability=base_probability,
                        expected_return_r=expected_return_r,
                        holdout_expected_return_r=holdout_expected_return_r,
                        expected_return_gap=expected_return_gap,
                        sample_count=len(holdout),
                        brier_score=holdout_brier,
                        reliability_gap=reliability_gap,
                        dataset_digest=digest,
                        issued_at_epoch=current,
                        expires_at_epoch=current + self.artifact_ttl,
                        walk_forward_folds=len(folds),
                        selection_coverage=coverage,
                    )
                    _register_artifact(artifact)
                    artifacts.append(artifact)
            return artifacts
        except Exception:
            return []

    def evidence_for(self, symbol: str, *, now: Optional[float] = None) -> list[Evidence]:
        current = time.time() if now is None else float(now)
        sym = str(symbol or "").strip().upper()
        if not sym:
            return []
        self.refresh(sym, now=current)
        with _REGISTRY_LOCK:
            artifacts = [
                item for item in _REGISTRY.values()
                if item.symbol == sym and item.issued_at_epoch <= current <= item.expires_at_epoch
            ]
        return [
            Evidence(
                source_module="calibration_producer.fixed_horizon",
                domain=EvidenceDomain.HISTORICAL_ANALOGUE,
                symbol=sym,
                observation=(
                    f"Producer-attested chronological fixed-horizon calibration "
                    f"for {item.direction}, horizon={item.horizon_seconds}s."
                ),
                confidence=0.0,
                uncertainty=0.0,
                measurements=item.to_measurements(),
                timestamp_epoch=item.issued_at_epoch,
                source_timestamp_epoch=item.issued_at_epoch,
                relevance_horizon_seconds=self.artifact_ttl,
                data_basis="durable_selection_aware_candidate_outcomes",
                independence_group=f"calibration:{sym}:{item.direction}:{item.horizon_seconds}",
                measurement_version=item.producer_version,
            )
            for item in artifacts
        ]


def get_default_calibration_producer() -> CalibrationProducer:
    global _DEFAULT_PRODUCER
    with _DEFAULT_LOCK:
        if _DEFAULT_PRODUCER is None:
            _DEFAULT_PRODUCER = CalibrationProducer()
        return _DEFAULT_PRODUCER


__all__ = [
    "CalibrationArtifact", "CalibrationProducer",
    "resolve_calibration_artifact", "get_default_calibration_producer",
]
