"""Async market watcher that continually re-evaluates fresh Gold snapshots.

The callback is a boundary into Apex's opportunity/portfolio pipeline, not a
broker adapter. Each poll re-evaluates the current market quote and completed
bars, while stable signal keys suppress duplicate candidate publication.
"""
from __future__ import annotations

import asyncio
import inspect
import logging
from datetime import datetime, timezone
from typing import Awaitable, Callable, Protocol

from gold_desk.coordinator import EvaluationResult, StrategyCoordinator
from gold_desk.models import MarketSnapshot

logger = logging.getLogger(__name__)


class SnapshotSource(Protocol):
    async def read_snapshot(self) -> MarketSnapshot | None: ...


Publisher = Callable[[MarketSnapshot, EvaluationResult], object | Awaitable[object]]
Heartbeat = Callable[[MarketSnapshot, EvaluationResult], object | Awaitable[object]]


async def _call(callback, *args) -> None:
    result = callback(*args)
    if inspect.isawaitable(result):
        await result


class GoldMarketWatcher:
    """Continuously inspect Gold, fail closed on feed errors and stale quotes."""

    def __init__(
        self,
        source: SnapshotSource,
        coordinator: StrategyCoordinator,
        publisher: Publisher,
        *,
        heartbeat: Heartbeat | None = None,
        poll_interval_seconds: float = 0.25,
        max_quote_age_seconds: float = 3.0,
    ) -> None:
        if poll_interval_seconds < 0.05:
            raise ValueError("poll interval must be >= 50 ms")
        self.source = source
        self.coordinator = coordinator
        self.publisher = publisher
        self.heartbeat = heartbeat
        self.poll_interval_seconds = poll_interval_seconds
        self.max_quote_age_seconds = max_quote_age_seconds
        self._published_keys: set[str] = set()
        self._published_order: list[str] = []
        self.polls = 0
        self.feed_errors = 0
        self.stale_snapshots = 0

    async def run(self, stop_event: asyncio.Event) -> None:
        while not stop_event.is_set():
            try:
                snapshot = await self.source.read_snapshot()
                self.polls += 1
                if snapshot is not None:
                    now = datetime.now(timezone.utc)
                    age = (now - snapshot.observed_at).total_seconds()
                    if age < -1.0 or age > self.max_quote_age_seconds:
                        self.stale_snapshots += 1
                        result = EvaluationResult(
                            snapshot.observed_at, (),
                            (f"watcher rejected stale/future quote ({age:.2f}s)",),
                        )
                    else:
                        # Strategies use completed bars, but price-sensitive
                        # checks are refreshed from the current bid/ask every poll.
                        result = self.coordinator.evaluate(snapshot)
                        for idea in result.ideas:
                            key = idea.signal_key
                            if key not in self._published_keys:
                                await _call(self.publisher, snapshot, result)
                                self._published_keys.add(key)
                                self._published_order.append(key)
                                if len(self._published_order) > 4096:
                                    expired = self._published_order.pop(0)
                                    self._published_keys.discard(expired)
                    if self.heartbeat is not None:
                        await _call(self.heartbeat, snapshot, result)
            except asyncio.CancelledError:
                raise
            except Exception:
                self.feed_errors += 1
                logger.exception("[gold-desk] snapshot cycle failed; no candidate published")

            try:
                await asyncio.wait_for(stop_event.wait(), timeout=self.poll_interval_seconds)
            except asyncio.TimeoutError:
                pass
