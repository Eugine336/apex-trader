"""
APEX TRADER — Dashboard Events Mixin (Phase 5)
Reads the append-only event store (apex_events.db) for the dashboard.
Replaces the volatile in-memory system_warnings[] with persistent,
severity-filtered, paginated event queries.
"""

from __future__ import annotations

import json
import time as _time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from loguru import logger


class EventsMixin:
    """Reads from the EventStore singleton for activity feed + reconciliation."""

    _events_cache: list
    _events_cache_ts: float
    _events_cache_ttl: float

    def _get_event_store(self):
        try:
            from persistence.event_store import get_event_store
            return get_event_store()
        except Exception as e:
            logger.debug(f"Event store unavailable: {e}")
            return None

    def get_events(
        self,
        severity_min: str = "INFO",
        event_types: Optional[List[str]] = None,
        symbol: Optional[str] = None,
        correlation_id: Optional[str] = None,
        limit: int = 200,
        offset: int = 0,
    ) -> dict:
        """Full activity feed from the persistent event store."""
        store = self._get_event_store()
        if store is None:
            return {"events": [], "total": 0, "source": "unavailable"}

        try:
            rows = store.query_events(
                severity_min=severity_min,
                event_types=event_types,
                symbol=symbol,
                correlation_id=correlation_id,
                limit=limit,
                offset=offset,
            )
            events = []
            for row in rows:
                payload = {}
                pj = row.get("payload_json")
                if pj:
                    try:
                        payload = json.loads(pj) if isinstance(pj, str) else pj
                    except Exception:
                        payload = {}

                ts_ms = row.get("ts_utc_ms", 0)
                ts_iso = ""
                if ts_ms:
                    try:
                        ts_iso = datetime.fromtimestamp(
                            ts_ms / 1000, tz=timezone.utc
                        ).isoformat()
                    except Exception:
                        ts_iso = str(ts_ms)

                sev = (row.get("severity") or "INFO").upper()
                level = _severity_to_level(sev)

                message = payload.get("message", "")
                if not message:
                    et = row.get("event_type", "")
                    message = payload.get("reason", payload.get("exit_reason", et))

                events.append({
                    "event_id": row.get("event_id"),
                    "correlation_id": row.get("correlation_id"),
                    "timestamp": ts_iso,
                    "level": level,
                    "severity": sev,
                    "event_type": row.get("event_type", "LOG"),
                    "symbol": row.get("symbol") or "",
                    "source_module": row.get("source_module") or "",
                    "message": str(message),
                    "payload": payload,
                })
            return {"events": events, "total": len(events), "source": "event_store"}
        except Exception as exc:
            logger.debug("[state_events] get_events failed: {}", exc)
            return {"events": [], "total": 0, "source": "error"}

    def get_reconciliation(self) -> dict:
        """Broker-vs-derived exit reason discrepancies."""
        store = self._get_event_store()
        if store is None:
            return {"anomalies": [], "total": 0}

        try:
            raw = store.get_reconciliation(limit=200)
            anomalies = []
            for row in raw:
                payload = row.get("_payload") or {}
                if not payload:
                    pj = row.get("payload_json")
                    if pj:
                        try:
                            payload = json.loads(pj) if isinstance(pj, str) else pj
                        except Exception:
                            payload = {}

                ts_ms = row.get("ts_utc_ms", 0)
                ts_iso = ""
                if ts_ms:
                    try:
                        ts_iso = datetime.fromtimestamp(
                            ts_ms / 1000, tz=timezone.utc
                        ).isoformat()
                    except Exception:
                        ts_iso = str(ts_ms)

                anomalies.append({
                    "event_id": row.get("event_id"),
                    "correlation_id": row.get("correlation_id"),
                    "timestamp": ts_iso,
                    "symbol": row.get("symbol") or "",
                    "exit_reason": payload.get("exit_reason", ""),
                    "exit_reason_source": payload.get("exit_reason_source", ""),
                    "raw_broker_reason": payload.get("raw_broker_reason", ""),
                    "raw_broker_comment": payload.get("raw_broker_comment", ""),
                    "order_id": payload.get("order_id"),
                    "pnl_dollars": payload.get("pnl_dollars"),
                })
            return {"anomalies": anomalies, "total": len(anomalies)}
        except Exception as exc:
            logger.debug("[state_events] get_reconciliation failed: {}", exc)
            return {"anomalies": [], "total": 0}


def _severity_to_level(severity: str) -> str:
    """Map EventStore severity to the dashboard's existing level vocabulary."""
    s = severity.upper()
    if s == "ERROR":
        return "error"
    if s == "WARNING":
        return "warning"
    if s == "DEBUG":
        return "debug"
    return "info"
