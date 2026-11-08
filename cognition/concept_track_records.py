"""Producer-owned descriptive concept track records from verified broker quotes.

These records measure whether source-backed concept implications agreed with
subsequent directional quote movement at fixed horizons. They are historical
context only: not forecasts, probabilities, trade decisions or execution gates.
"""
from __future__ import annotations

import json
import logging
import math
import time
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

from cognition.contracts import Evidence, EvidenceDomain

_LOG = logging.getLogger(__name__)

_SNAPSHOT_EVENT = "COGNITIVE_CANDIDATE_SNAPSHOT"
_OUTCOME_EVENT = "COGNITIVE_CANDIDATE_HORIZON_OUTCOME"
_DISPOSITION_EVENT = "COGNITIVE_CANDIDATE_DISPOSITION"

_ALLOWED_DISPOSITIONS = {
    "PASSED_PORTFOLIO_REVIEW_PENDING_EXECUTION_ADMISSION",
    "REJECTED_BY_BRAIN",
    "NO_OPPORTUNITY",
    "DEFERRED_FORMING",
    "BLOCKED_OR_DEFERRED_BY_ADMISSION",
    "OBSERVE",
    "NON_ORIGINATION_DECISION",
}


def _finite(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _payload(row: dict) -> dict:
    try:
        value = json.loads(row.get("payload_json") or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


class ConceptTrackRecordProducer:
    """Build symbol-scoped, selection-complete concept outcome context."""

    def __init__(self, event_store: Any = None, *, max_event_pages: int = 20) -> None:
        self._store = event_store
        self.max_event_pages = max(1, int(max_event_pages))

    def _event_store(self) -> Any:
        if self._store is None:
            from persistence.event_store import get_event_store
            self._store = get_event_store()
        return self._store

    def _query_all(self, event_type: str, symbol: str) -> tuple[list[dict], bool]:
        query = getattr(self._event_store(), "query_events", None)
        if not callable(query):
            return [], False
        rows: list[dict] = []
        offset = 0
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
                return rows, True
            offset += len(page)
        return rows, False

    @staticmethod
    def _active_concepts(
        snapshot: dict, symbol: str, direction: str, decision_epoch: float,
    ) -> dict[str, list[str]]:
        """Return concept -> source evidence IDs for verified active implications."""
        inventory = snapshot.get("evidence_inventory")
        if not isinstance(inventory, list):
            return {}
        concepts: dict[str, list[str]] = defaultdict(list)
        seen: set[tuple[str, str]] = set()
        for row in inventory:
            if not isinstance(row, dict):
                continue
            measurements = row.get("measurements")
            if not isinstance(measurements, dict):
                continue
            evidence_id = str(row.get("evidence_id", "") or "").strip()
            concept = str(measurements.get("concept", "") or "").strip().lower()
            source_epoch = _finite(row.get("source_timestamp_epoch"))
            if (
                not evidence_id or not concept
                or str(row.get("symbol", "") or "").strip().upper() != symbol
                or row.get("source_timestamp_verified") is not True
                or row.get("provisional") is True
                or source_epoch is None or source_epoch <= 0.0
                or source_epoch > decision_epoch
                or measurements.get("source_kind") != "concept_reading"
                or measurements.get("applicable") is not True
            ):
                continue
            implications = measurements.get("implications")
            if not isinstance(implications, list):
                continue
            active = any(
                isinstance(item, dict)
                and str(item.get("direction", "") or "").strip().upper() == direction
                and item.get("activated") is True
                for item in implications
            )
            key = (concept, evidence_id)
            if active and key not in seen:
                concepts[concept].append(evidence_id)
                seen.add(key)
        return dict(concepts)

    def evidence_for(self, symbol: str, *, now: float | None = None) -> list[Evidence]:
        sym = str(symbol or "").strip().upper()
        cutoff = time.time() if now is None else _finite(now)
        if not sym or cutoff is None or cutoff <= 0.0:
            return []
        try:
            snapshots, snapshots_complete = self._query_all(_SNAPSHOT_EVENT, sym)
            outcomes, outcomes_complete = self._query_all(_OUTCOME_EVENT, sym)
            dispositions, dispositions_complete = self._query_all(_DISPOSITION_EVENT, sym)
            if not (snapshots_complete and outcomes_complete and dispositions_complete):
                return []

            snapshot_by_id: dict[str, dict] = {}
            duplicate_snapshots: set[str] = set()
            for row in snapshots:
                payload = _payload(row)
                candidate_id = str(payload.get("candidate_id", "") or "").strip()
                if not candidate_id:
                    continue
                if candidate_id in snapshot_by_id:
                    duplicate_snapshots.add(candidate_id)
                else:
                    snapshot_by_id[candidate_id] = payload
            if duplicate_snapshots:
                _LOG.error("Concept track refresh rejected for %s: duplicate snapshots=%d", sym, len(duplicate_snapshots))
                return []

            disposition_by_id: dict[str, dict] = {}
            for row in dispositions:
                payload = _payload(row)
                candidate_id = str(payload.get("candidate_id", "") or "").strip()
                if not candidate_id:
                    continue
                sequence = int(_finite(row.get("seq")) or 0)
                current = disposition_by_id.get(candidate_id)
                if current is None or sequence > current["_sequence"]:
                    payload["_sequence"] = sequence
                    disposition_by_id[candidate_id] = payload

            outcome_by_key: dict[tuple[str, int], dict] = {}
            duplicates: set[tuple[str, int]] = set()
            for row in outcomes:
                payload = _payload(row)
                candidate_id = str(payload.get("candidate_id", "") or "").strip()
                horizon_raw = _finite(payload.get("horizon_seconds"))
                if not candidate_id or horizon_raw is None or not horizon_raw.is_integer() or horizon_raw <= 0:
                    continue
                key = (candidate_id, int(horizon_raw))
                if key in outcome_by_key:
                    duplicates.add(key)
                else:
                    outcome_by_key[key] = payload
            if duplicates:
                _LOG.error("Concept track refresh rejected for %s: duplicate outcome keys=%d", sym, len(duplicates))
                return []

            records: dict[str, list[dict]] = defaultdict(list)
            latest_source_epoch = 0.0
            for (candidate_id, horizon), outcome in outcome_by_key.items():
                snapshot = snapshot_by_id.get(candidate_id)
                if not isinstance(snapshot, dict):
                    continue
                candidate = snapshot.get("candidate")
                candidate = candidate if isinstance(candidate, dict) else {}
                direction = str(
                    outcome.get("direction")
                    or candidate.get("direction")
                    or candidate.get("direction_implication")
                    or ""
                ).strip().upper()
                decision_epoch = _finite(snapshot.get("decision_epoch"))
                outcome_epoch = _finite(outcome.get("observed_at_epoch"))
                quote = outcome.get("outcome_quote")
                quote = quote if isinstance(quote, dict) else {}
                source_epoch = _finite(quote.get("source_timestamp_epoch"))
                quote_timestamp = _finite(quote.get("timestamp_epoch"))
                quote_bid = _finite(quote.get("bid"))
                quote_ask = _finite(quote.get("ask"))
                quote_mid = _finite(quote.get("mid"))
                quote_kind = str(quote.get("quote_kind", "") or "").strip().lower()
                quote_valid = bool(
                    quote.get("source_timestamp_verified") is True
                    and str(quote.get("symbol", "") or "").strip().upper() == sym
                    and quote_kind in {"bid_ask", "mid_only"}
                    and quote_bid is not None and quote_ask is not None and quote_mid is not None
                    and quote_bid > 0.0 and quote_ask >= quote_bid
                    and (
                        quote_kind != "mid_only" or quote_ask == quote_bid
                    )
                    and abs(quote_mid - ((quote_bid + quote_ask) / 2.0))
                        <= max(1e-10, abs(quote_mid) * 1e-8)
                    and quote_timestamp is not None and source_epoch is not None
                    and abs(quote_timestamp - source_epoch) <= 1e-6
                )
                label = str(outcome.get("outcome_label", "") or "").upper()
                disposition = disposition_by_id.get(candidate_id) or {}
                if (
                    snapshot.get("point_in_time_complete") is not True
                    or snapshot.get("selection_population") != "ALL_RECORDED_BRAIN_CONSIDERED_CANDIDATES"
                    or str(snapshot.get("symbol", "") or "").strip().upper() != sym
                    or direction not in {"LONG", "SHORT"}
                    or decision_epoch is None or outcome_epoch is None or source_epoch is None
                    or not (0.0 < decision_epoch < outcome_epoch <= source_epoch <= cutoff)
                    or outcome.get("outcome_source_timestamp_verified") is not True
                    or not quote_valid
                    or source_epoch is None or outcome_epoch is None
                    or abs(source_epoch - outcome_epoch) > 1e-6
                    or outcome.get("observation_status") != "OBSERVED"
                    or label not in {"FAVOURABLE", "UNFAVOURABLE"}
                    or disposition.get("_sequence", 0) <= 0
                    or str(disposition.get("disposition", "") or "") not in _ALLOWED_DISPOSITIONS
                ):
                    continue
                concepts = self._active_concepts(snapshot, sym, direction, decision_epoch)
                if not concepts:
                    continue
                binary_outcome = 1 if label == "FAVOURABLE" else 0
                directional_return_r = _finite(outcome.get("directional_return_r"))
                if directional_return_r is None:
                    continue
                latest_source_epoch = max(latest_source_epoch, source_epoch)
                for concept, evidence_ids in concepts.items():
                    records[concept].append({
                        "candidate_id": candidate_id,
                        "symbol": sym,
                        "concept": concept,
                        "direction": direction,
                        "outcome": binary_outcome,
                        "outcome_label": label,
                        "decision_epoch": decision_epoch,
                        "outcome_epoch": outcome_epoch,
                        "source_timestamp_epoch": source_epoch,
                        "source_timestamp_verified": True,
                        "horizon_seconds": horizon,
                        "directional_return_r": directional_return_r,
                        "source_evidence_ids": list(evidence_ids),
                        "candidate_state_at_decision": str(
                            snapshot.get("candidate_state_at_decision", "") or ""
                        ),
                        "market_state": (
                            str(candidate.get("market_state", "") or "").strip().lower() or None
                        ),
                        "outcome_semantics": "directional_favourability_at_fixed_quote_horizon_not_trade_win_probability",
                    })

            if not records or latest_source_epoch <= 0.0:
                return []
            summary = {}
            for concept, rows in sorted(records.items()):
                summary[concept] = {
                    "sample_count": len(rows),
                    "favourable_count": sum(row["outcome"] for row in rows),
                    "unfavourable_count": sum(1 - row["outcome"] for row in rows),
                    "mean_directional_return_r": sum(row["directional_return_r"] for row in rows) / len(rows),
                    "horizons_seconds": sorted({row["horizon_seconds"] for row in rows}),
                    "selection_complete": False,
                    "eligible_candidate_sample_only": True,
                    "probability_forecast": False,
                    "trade_win_rate": False,
                    "outcome_semantics": "directional_favourability_at_fixed_quote_horizon",
                }
            sampled_at = time.time()
            return [Evidence(
                source_module="cognition.concept_track_record_producer",
                domain=EvidenceDomain.HISTORICAL_ANALOGUE,
                symbol=sym,
                observation=(
                    f"{sym}: producer-registered quote-history track records are available for "
                    f"{len(summary)} concepts; these are descriptive directional outcomes, not forecasts."
                ),
                confidence=0.0,
                uncertainty=1.0,
                polarity=0.0,
                measurements={
                    "source_kind": "producer_concept_track_records",
                    "concept_track_records": summary,
                    "concept_outcome_history": [
                        row for concept_rows in records.values() for row in concept_rows
                    ],
                    "selection_complete": False,
                    "eligible_candidate_sample_only": True,
                    "sample_scope": (
                        "broker-quote-labelled candidates with complete source evidence, geometry and disposition; "
                        "not the full opportunity population"
                    ),
                    "predictive_probability": False,
                    "trade_win_rate": False,
                    "directional_decision_emitted": False,
                    "entry_authorization_emitted": False,
                    "source_timestamp_epoch": latest_source_epoch,
                    "source_timestamp_verified": True,
                },
                timestamp_epoch=sampled_at,
                timestamp_iso=datetime.fromtimestamp(sampled_at, tz=timezone.utc).isoformat(),
                relevance_horizon_seconds=86400.0,
                source_timestamp_epoch=latest_source_epoch,
                source_timestamp_verified=True,
                data_basis="producer_verified_broker_quote_history",
                provisional=False,
                independence_group=f"concept_track_record:{sym}",
            )]
        except Exception as exc:  # noqa: BLE001 — missing or malformed history remains absent
            _LOG.warning("Concept track record refresh unavailable for %s: %s", sym, exc)
            return []


_DEFAULT_PRODUCER: ConceptTrackRecordProducer | None = None


def get_default_concept_track_record_producer() -> ConceptTrackRecordProducer:
    global _DEFAULT_PRODUCER
    if _DEFAULT_PRODUCER is None:
        _DEFAULT_PRODUCER = ConceptTrackRecordProducer()
    return _DEFAULT_PRODUCER
