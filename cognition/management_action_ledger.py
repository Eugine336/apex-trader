"""Durable write-ahead and acknowledgement ledger for Brain management actions.

Action IDs are idempotency keys. Once an action is submitted or its outcome is
ambiguous, the same action ID must not be sent again. The ledger records sink
acknowledgements and broker response metadata without granting decision authority.
"""
from __future__ import annotations

import json
import math
import time
from typing import Any


_SUBMITTED = "MANAGEMENT_ACTION_SUBMITTED"
_ACKNOWLEDGED = "MANAGEMENT_ACTION_ACKNOWLEDGED"
_REJECTED = "MANAGEMENT_ACTION_REJECTED"
_AMBIGUOUS = "MANAGEMENT_ACTION_AMBIGUOUS"

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
        parsed = json.loads(row.get("payload_json") or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _response_summary(response: Any) -> dict:
    if response is None:
        return {}
    fields = (
        "success", "order_id", "ticket", "deal", "retcode", "comment",
        "error", "fill_price", "close_price", "lots_closed", "pnl",
    )
    if isinstance(response, dict):
        allowed = set(fields) | {
            "source", "source_timestamp_epoch", "source_timestamp_verified",
            "position_ticket", "closed_position_ticket", "book_complete",
            "platform_confirmed", "position_present", "current_lots", "current_sl",
            "direction", "opening_deal_verified", "closed_deal_verified",
            "reconciled", "reconciled_epoch", "reconciliation_basis",
        }
        return {
            str(key): value for key, value in response.items()
            if key in allowed
            and (isinstance(value, (str, int, float, bool)) or value is None)
            and not (isinstance(value, float) and not math.isfinite(value))
        }
    result = {}
    for name in fields:
        value = getattr(response, name, None)
        if isinstance(value, (str, int, float, bool)) or value is None:
            if isinstance(value, float) and not math.isfinite(value):
                continue
            if value is not None:
                result[name] = value
    return result


class ManagementActionLedger:
    """Durable event-store ledger keyed by the Brain-authored action ID."""

    def __init__(self, event_store: Any = None) -> None:
        self._store = event_store

    def _event_store(self) -> Any:
        if self._store is None:
            from persistence.event_store import get_event_store
            self._store = get_event_store()
        return self._store

    def _history(self, action_id: str, symbol: str) -> list[dict] | None:
        store = self._event_store()
        query = getattr(store, "query_events", None)
        if not callable(query) or getattr(store, "is_degraded", False) is True:
            return None
        rows = query(
            severity_min="DEBUG",
            event_types=[_SUBMITTED, _ACKNOWLEDGED, _REJECTED, _AMBIGUOUS],
            symbol=symbol,
            correlation_id=action_id,
            limit=1000,
            offset=0,
        )
        if rows is None:
            return None
        return list(rows)

    def begin(self, action: Any, position: Any) -> tuple[bool | None, str]:
        """Record the action before dispatch; None means durable storage unavailable.

        False means the action ID already exists and must not be replayed.
        True means the write-ahead record committed.
        """
        action_id = str(getattr(action, "action_id", "") or "").strip()
        symbol = str(getattr(action, "symbol", "") or "").strip().upper()
        if not action_id:
            return None, "action_id_missing"
        history = self._history(action_id, symbol)
        if history is None:
            return None, "management_action_ledger_unavailable"
        if history:
            latest = max(history, key=lambda row: int(row.get("seq", 0) or 0))
            payload = _payload(latest)
            latest_status = str(payload.get("status", latest.get("event_type", "EXISTS"))).upper()
            if latest_status != "REJECTED":
                return False, latest_status
            # An explicit broker/gate rejection proves this action ID did not
            # take effect. A new submission may retry it; ACKNOWLEDGED and
            # AMBIGUOUS outcomes remain non-replayable until reconciliation.
        size = None
        for name in ("lots", "size", "volume"):
            raw = getattr(position, name, None)
            if raw is not None:
                try:
                    candidate = float(raw)
                except (TypeError, ValueError, OverflowError):
                    continue
                if math.isfinite(candidate) and candidate >= 0.0:
                    size = candidate
                    break
        payload = {
            **(action.to_dict() if callable(getattr(action, "to_dict", None)) else {}),
            "action_id": action_id,
            "status": "SUBMITTED",
            "position_ticket": str(
                getattr(position, "ticket", "")
                or getattr(position, "position_ticket", "")
                or ""
            ),
            "position_size_before": size,
            "submitted_epoch": time.time(),
        }
        try:
            self._event_store().emit(
                _SUBMITTED,
                "INFO",
                symbol=symbol,
                correlation_id=action_id,
                parent_id=str(getattr(action, "decision_id", "") or ""),
                source_module="cognition.management_action_ledger",
                payload=payload,
                durable=True,
            )
            return True, "SUBMITTED"
        except Exception as exc:  # noqa: BLE001 — caller decides risk-aware fallback
            return None, f"management_action_ledger_write_failed:{type(exc).__name__}"

    def finish(
        self, action: Any, status: str, *, response: Any = None, error: str = "",
    ) -> bool:
        normalized = str(status or "").strip().upper()
        event_type = {
            "ACKNOWLEDGED": _ACKNOWLEDGED,
            "REJECTED": _REJECTED,
            "AMBIGUOUS": _AMBIGUOUS,
        }.get(normalized)
        if event_type is None:
            raise ValueError("management action status must be ACKNOWLEDGED, REJECTED or AMBIGUOUS")
        action_id = str(getattr(action, "action_id", "") or "").strip()
        symbol = str(getattr(action, "symbol", "") or "").strip().upper()
        if not action_id or not symbol:
            return False
        payload = {
            "action_id": action_id,
            "decision_id": str(getattr(action, "decision_id", "") or ""),
            "campaign_id": str(getattr(action, "campaign_id", "") or ""),
            "opportunity_id": str(getattr(action, "opportunity_id", "") or ""),
            "kind": str(getattr(action, "kind", "") or ""),
            "status": normalized,
            "response": _response_summary(response),
            "error": str(error or "")[:500],
            "acknowledged_epoch": time.time(),
            "broker_acknowledgement_present": bool(
                set(_response_summary(response)) & {"order_id", "ticket", "deal", "retcode"}
                or (isinstance(response, dict) and response.get("reconciled") is True)
            ),
        }
        try:
            self._event_store().emit(
                event_type,
                "INFO" if normalized != "AMBIGUOUS" else "WARNING",
                symbol=symbol,
                correlation_id=action_id,
                parent_id=str(getattr(action, "decision_id", "") or ""),
                source_module="cognition.management_action_ledger",
                payload=payload,
                durable=True,
            )
            return True
        except Exception:
            return False

    def pending_actions(self, symbol: str | None = None) -> list[dict]:
        """Return latest submitted/ambiguous actions; never infer they succeeded."""
        store = self._event_store()
        query = getattr(store, "query_events", None)
        if not callable(query) or getattr(store, "is_degraded", False) is True:
            return []
        rows = query(
            severity_min="DEBUG",
            event_types=[_SUBMITTED, _ACKNOWLEDGED, _REJECTED, _AMBIGUOUS],
            symbol=str(symbol or "").strip().upper() or None,
            limit=10000,
            offset=0,
        ) or []
        latest: dict[str, dict] = {}
        for row in sorted(rows, key=lambda item: int(item.get("seq", 0) or 0)):
            payload = _payload(row)
            action_id = str(payload.get("action_id", "") or "").strip()
            if not action_id:
                continue
            sequence = int(row.get("seq", 0) or 0)
            merged = {**latest.get(action_id, {}), **payload}
            merged["_seq"] = sequence
            merged["_event_type"] = str(row.get("event_type", "") or "")
            latest[action_id] = merged
        return [
            row for row in latest.values()
            if row.get("status") in {"SUBMITTED", "AMBIGUOUS"}
        ]

    def reconcile(self, action_id: str, symbol: str, broker_evidence: dict) -> bool:
        """Resolve an ambiguous action only from a verified, complete broker snapshot.

        The evidence schema is intentionally strict. If the broker cannot provide
        the required position/deal state, the action remains unresolved.
        """
        action_id = str(action_id or "").strip()
        symbol = str(symbol or "").strip().upper()
        if not action_id or not symbol or not isinstance(broker_evidence, dict):
            return False
        if (
            broker_evidence.get("source_timestamp_verified") is not True
            or not str(broker_evidence.get("source", "") or "").strip()
            or str(broker_evidence.get("symbol", "") or "").strip().upper() != symbol
            or broker_evidence.get("book_complete") is not True
            or broker_evidence.get("platform_confirmed") is not True
        ):
            return False
        observed_epoch = _finite(broker_evidence.get("source_timestamp_epoch"))
        if observed_epoch is None or observed_epoch <= 0.0 or observed_epoch > time.time():
            return False
        pending = next(
            (row for row in self.pending_actions(symbol) if row.get("action_id") == action_id),
            None,
        )
        if pending is None:
            return False
        submitted_epoch = _finite(pending.get("submitted_epoch"))
        if submitted_epoch is None or observed_epoch < submitted_epoch:
            return False
        kind = str(pending.get("kind", "") or "").strip().lower()
        ticket = str(pending.get("position_ticket", "") or "").strip()
        evidence_ticket = str(
            broker_evidence.get("closed_position_ticket")
            if kind in {"close", "reverse"}
            else broker_evidence.get("position_ticket")
            or broker_evidence.get("closed_position_ticket")
            or ""
        ).strip()
        if not ticket or evidence_ticket != ticket:
            return False
        position_present = broker_evidence.get("position_present")
        matched = False
        if kind == "close":
            matched = (
                position_present is False
                and broker_evidence.get("closed_deal_verified") is True
            )
        elif kind == "partial_close":
            before = _finite(pending.get("position_size_before"))
            fraction = _finite(pending.get("fraction"))
            current = _finite(broker_evidence.get("current_lots"))
            if before is not None and fraction is not None and current is not None:
                target = before * (1.0 - fraction)
                tolerance = max(1e-9, before * 1e-6)
                matched = (
                    (
                        position_present is True
                        and 0.0 <= current <= target + tolerance
                        and current < before - tolerance
                    )
                    or (
                        position_present is False
                        and broker_evidence.get("closed_deal_verified") is True
                    )
                )
        elif kind in {"tighten_sl", "protect_sl"}:
            requested = _finite(pending.get("stop_price"))
            current = _finite(broker_evidence.get("current_sl"))
            direction = str(pending.get("direction", "") or "").upper()
            if requested is not None and current is not None:
                matched = (
                    position_present is True
                    and (
                        current >= requested if direction == "LONG"
                        else current <= requested if direction == "SHORT"
                        else False
                    )
                )
        elif kind == "scale_in":
            before = _finite(pending.get("position_size_before"))
            current = _finite(broker_evidence.get("current_lots"))
            matched = (
                position_present is True
                and before is not None and current is not None and current > before
                and broker_evidence.get("opening_deal_verified") is True
            )
        elif kind == "reverse":
            matched = (
                position_present is True
                and str(broker_evidence.get("direction", "") or "").upper()
                    == str(pending.get("target_direction", "") or "").upper()
                and broker_evidence.get("closed_deal_verified") is True
                and broker_evidence.get("opening_deal_verified") is True
            )
        if not matched:
            return False
        action = type("ReconciledAction", (), {
            "action_id": action_id,
            "symbol": symbol,
            "decision_id": str(pending.get("decision_id", "") or ""),
            "campaign_id": str(pending.get("campaign_id", "") or ""),
            "opportunity_id": str(pending.get("opportunity_id", "") or ""),
            "kind": kind,
        })()
        evidence = {
            **broker_evidence,
            "reconciled": True,
            "reconciled_epoch": observed_epoch,
            "reconciliation_basis": "verified_complete_broker_state",
        }
        return self.finish(action, "ACKNOWLEDGED", response=evidence)

    def status(self, action_id: str, symbol: str) -> str | None:
        history = self._history(str(action_id or "").strip(), str(symbol or "").strip().upper())
        if history is None:
            return None
        if not history:
            return None
        latest = max(history, key=lambda row: int(row.get("seq", 0) or 0))
        payload = _payload(latest)
        return str(payload.get("status") or latest.get("event_type") or "")
