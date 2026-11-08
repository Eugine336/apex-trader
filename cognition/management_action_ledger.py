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
            "broker_acknowledgement_present": bool(response is not None),
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

    def status(self, action_id: str, symbol: str) -> str | None:
        history = self._history(str(action_id or "").strip(), str(symbol or "").strip().upper())
        if history is None:
            return None
        if not history:
            return None
        latest = max(history, key=lambda row: int(row.get("seq", 0) or 0))
        payload = _payload(latest)
        return str(payload.get("status") or latest.get("event_type") or "")
