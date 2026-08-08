"""APEX TRADER — Lightweight in-process event bus (Phase 2).

Thread-safe pub/sub for candle-close events and tick events.
Subscribers register callbacks keyed by event type (a plain string).
Publishing is synchronous — callbacks run on the publisher's thread.
"""

from __future__ import annotations

import threading
from collections import defaultdict
from typing import Any, Callable

from loguru import logger


class EventBus:
    """Simple thread-safe publish/subscribe bus.

    * ``subscribe(event_type, callback)`` — register a listener.
    * ``publish(event_type, event)``      — invoke all listeners for that type.

    Callbacks run synchronously on the calling thread.  A failing callback
    is logged and skipped — it never prevents other subscribers from running.
    """

    def __init__(self) -> None:
        self._subscribers: dict[str, list[Callable[[Any], None]]] = defaultdict(list)
        self._lock = threading.Lock()

    def subscribe(self, event_type: str, callback: Callable[[Any], None]) -> None:
        with self._lock:
            self._subscribers[event_type].append(callback)

    def unsubscribe(self, event_type: str, callback: Callable[[Any], None]) -> None:
        with self._lock:
            try:
                self._subscribers[event_type].remove(callback)
            except ValueError:
                pass

    def publish(self, event_type: str, event: Any) -> None:
        with self._lock:
            callbacks = list(self._subscribers.get(event_type, []))
        for cb in callbacks:
            try:
                cb(event)
            except Exception as exc:
                logger.warning(
                    "[event-bus] callback error on {}: {} — {}",
                    event_type,
                    type(exc).__name__,
                    exc,
                )

    def subscriber_count(self, event_type: str) -> int:
        with self._lock:
            return len(self._subscribers.get(event_type, []))

    def clear(self) -> None:
        with self._lock:
            self._subscribers.clear()
