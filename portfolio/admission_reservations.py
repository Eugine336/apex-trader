"""Durable reservations for Brain-approved but not-yet-broker-confirmed exposure.

Compliance and portfolio sizing run before the asynchronous execution flush. Without
a reservation, multiple decisions can each pass against the same stale broker book
and collectively exceed duplicate, position-count or account-heat limits.
"""
from __future__ import annotations

import json
import math
import threading
from typing import Any, Callable

from persistence import domain_events as DE


class PortfolioAdmissionReservations:
    RESERVED_EVENT = DE.PORTFOLIO_OPEN_RESERVATION
    LINKED_EVENT = DE.PORTFOLIO_OPEN_RESERVATION_ORDER_LINKED
    RELEASED_EVENT = DE.PORTFOLIO_OPEN_RESERVATION_RELEASED

    def __init__(self, store_provider: Callable[[], Any] | None = None) -> None:
        self._store_provider = store_provider or self._existing_store
        self._lock = threading.RLock()
        self._loaded = False
        self._active: dict[str, dict[str, Any]] = {}

    @staticmethod
    def _existing_store():
        from persistence.event_store import get_existing_event_store
        return get_existing_event_store()

    @property
    def lock(self):
        """Lock shared with the origination sink across check→reserve→submit."""
        return self._lock

    def _store(self):
        store = self._store_provider()
        if store is None or bool(getattr(store, "is_degraded", False)):
            return None
        return store

    def _load_locked(self) -> bool:
        if self._loaded:
            return True
        store = self._store()
        if store is None:
            return False
        rows: list[dict[str, Any]] = []
        offset = 0
        page_size = 10000
        for _ in range(100):
            page = store.query_events(
                severity_min="DEBUG",
                event_types=[self.RESERVED_EVENT, self.LINKED_EVENT, self.RELEASED_EVENT],
                limit=page_size,
                offset=offset,
                raise_on_error=True,
            )
            rows.extend(page)
            if len(page) < page_size:
                break
            offset += len(page)
        else:
            raise RuntimeError("portfolio reservation event replay exceeded its page bound")
        rows.sort(key=lambda row: int(row.get("seq") or 0))
        active: dict[str, dict[str, Any]] = {}
        for row in rows:
            raw = row.get("payload_json")
            payload = json.loads(raw) if isinstance(raw, str) else raw
            if not isinstance(payload, dict):
                continue
            reservation_id = str(payload.get("reservation_id") or "").strip()
            if not reservation_id:
                continue
            if row.get("event_type") == self.RESERVED_EVENT:
                active[reservation_id] = dict(payload)
            elif row.get("event_type") == self.LINKED_EVENT:
                if reservation_id in active:
                    active[reservation_id]["broker_order_id"] = str(
                        payload.get("broker_order_id") or ""
                    )
            elif row.get("event_type") == self.RELEASED_EVENT:
                active.pop(reservation_id, None)
        self._active = active
        self._loaded = True
        return True

    def snapshot(self) -> list[dict[str, Any]]:
        with self._lock:
            self._load_locked()
            return [dict(item) for item in self._active.values()]

    def reserve(self, payload: dict[str, Any]) -> bool:
        if not isinstance(payload, dict):
            return False
        reservation_id = str(payload.get("reservation_id") or "").strip()
        symbol = str(payload.get("symbol") or "").strip().upper()
        direction = str(payload.get("direction") or "").strip().upper()
        account_key = str(payload.get("account_key") or "").strip()
        try:
            risk_pct = float(payload.get("risk_pct"))
            max_loss = float(payload.get("max_loss"))
        except (TypeError, ValueError, OverflowError):
            return False
        if (
            not reservation_id or not symbol or direction not in {"LONG", "SHORT"}
            or not account_key or not math.isfinite(risk_pct) or risk_pct <= 0.0
            or not math.isfinite(max_loss) or max_loss <= 0.0
        ):
            return False
        with self._lock:
            if not self._load_locked() or reservation_id in self._active:
                return False
            store = self._store()
            if store is None:
                return False
            clean = dict(payload)
            clean.update({
                "reservation_id": reservation_id,
                "symbol": symbol,
                "direction": direction,
                "account_key": account_key,
                "risk_pct": risk_pct,
                "max_loss": max_loss,
            })
            store.emit(
                event_type=self.RESERVED_EVENT,
                severity="INFO",
                symbol=symbol,
                correlation_id=str(clean.get("decision_id") or "") or None,
                parent_id=reservation_id,
                source_module="portfolio.admission_reservations",
                durable=True,
                payload=clean,
            )
            self._active[reservation_id] = clean
            return True

    def link_order(self, reservation_id: str, broker_order_id: str) -> bool:
        key = str(reservation_id or "").strip()
        order_id = str(broker_order_id or "").strip()
        if not key or not order_id:
            return False
        with self._lock:
            if not self._load_locked() or key not in self._active:
                return False
            store = self._store()
            if store is None:
                return False
            item = self._active[key]
            store.emit(
                event_type=self.LINKED_EVENT,
                severity="INFO",
                symbol=str(item.get("symbol") or ""),
                correlation_id=str(item.get("decision_id") or "") or None,
                parent_id=key,
                source_module="portfolio.admission_reservations",
                durable=True,
                payload={"reservation_id": key, "broker_order_id": order_id},
            )
            item["broker_order_id"] = order_id
            return True

    def release(self, reservation_id: str, *, reason: str) -> bool:
        key = str(reservation_id or "").strip()
        if not key:
            return False
        with self._lock:
            if not self._load_locked():
                return False
            item = self._active.get(key)
            if item is None:
                return True
            store = self._store()
            if store is None:
                return False
            store.emit(
                event_type=self.RELEASED_EVENT,
                severity="INFO",
                symbol=str(item.get("symbol") or ""),
                correlation_id=str(item.get("decision_id") or "") or None,
                parent_id=key,
                source_module="portfolio.admission_reservations",
                durable=True,
                payload={
                    "reservation_id": key,
                    "reason": str(reason or "unspecified")[:200],
                },
            )
            self._active.pop(key, None)
            return True

    def release_by_order_id(self, broker_order_id: str, *, reason: str) -> int:
        order_id = str(broker_order_id or "").strip()
        if not order_id:
            return 0
        with self._lock:
            if not self._load_locked():
                return 0
            keys = [
                key for key, item in self._active.items()
                if str(item.get("broker_order_id") or "") == order_id
            ]
            return sum(1 for key in keys if self.release(key, reason=reason))

    def reconcile(self, open_positions: list[Any]) -> int:
        """Release reservations only when a matching broker position is visible."""
        positions = list(open_positions or [])
        released = 0
        with self._lock:
            if not self._load_locked():
                return 0
            matches = []
            for key, item in self._active.items():
                wanted_symbol = str(item.get("symbol") or "").strip().upper()
                wanted_direction = str(item.get("direction") or "").strip().upper()
                for position in positions:
                    if isinstance(position, dict):
                        symbol = str(position.get("symbol") or "").strip().upper()
                        direction = str(position.get("direction") or "").strip().upper()
                    else:
                        symbol = str(getattr(position, "symbol", "") or "").strip().upper()
                        direction = str(getattr(position, "direction", "") or "").strip().upper()
                    direction = "LONG" if direction in {"BUY", "LONG"} else (
                        "SHORT" if direction in {"SELL", "SHORT"} else ""
                    )
                    position_account = (
                        str(position.get("account_key") or "")
                        if isinstance(position, dict)
                        else str(getattr(position, "account_key", "") or "")
                    )
                    if (
                        symbol == wanted_symbol
                        and direction == wanted_direction
                        and position_account == str(item.get("account_key") or "")
                    ):
                        matches.append(key)
                        break
            for key in matches:
                if self.release(key, reason="broker_position_confirmed"):
                    released += 1
        return released
