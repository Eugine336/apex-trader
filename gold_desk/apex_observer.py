"""Shadow-only integration with Apex Trader's live tick and broker-candle APIs.

This service observes the existing tick stream and publishes research records
on the existing EventBus. It has no broker order API and cannot submit orders.
A single venue must be selected; candle history is fetched from the same
PlatformManager route as execution, and incoming ticks from other venues are
discarded to avoid mixing broker feeds.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from queue import Empty, Full, Queue
import threading
import time
from typing import Any, Callable, Iterable, Mapping

from loguru import logger

from gold_desk.coordinator import GoldOnlyGuard, StrategyCoordinator
from gold_desk.models import Candle, MarketSnapshot, TradeIdea

_TIMEFRAMES: dict[str, int] = {
    "M5": 5 * 60,
    "M15": 15 * 60,
    "H1": 60 * 60,
    "H4": 4 * 60 * 60,
    "D1": 24 * 60 * 60,
}
_HISTORY_BARS = 200


def _as_utc_datetime(value: Any) -> datetime | None:
    """Convert a pandas/numpy/Python timestamp into a timezone-aware UTC time."""
    try:
        if hasattr(value, "to_pydatetime"):
            value = value.to_pydatetime()
        if isinstance(value, datetime):
            if value.tzinfo is None or value.utcoffset() is None:
                value = value.replace(tzinfo=timezone.utc)
            return value.astimezone(timezone.utc)
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(float(value), tz=timezone.utc)
    except (OverflowError, OSError, TypeError, ValueError):
        return None
    return None


def _frame_to_closed_candles(frame: Any, timeframe: str, observed_at: datetime) -> tuple[Candle, ...]:
    """Convert a broker OHLC dataframe to close-time candles, excluding forming bars.

    Broker adapters conventionally timestamp candles at their opening boundary.
    The trading kernel requires close timestamps, so the timeframe duration is
    added and any bar whose close lies after the observed quote is rejected.
    """
    tf = timeframe.upper()
    seconds = _TIMEFRAMES.get(tf)
    if seconds is None or frame is None:
        return ()
    try:
        if len(frame) == 0:
            return ()
        raw_columns = list(getattr(frame, "columns", ()))
        columns = {str(column).lower(): column for column in raw_columns}
        time_column = columns.get("time")
        required = ("open", "high", "low", "close")
        if any(name not in columns for name in required):
            return ()
        result: list[Candle] = []
        for index, row in frame.iterrows():
            raw_time = row[time_column] if time_column is not None else index
            opened_at = _as_utc_datetime(raw_time)
            if opened_at is None:
                continue
            closed_at = opened_at + timedelta(seconds=seconds)
            if closed_at > observed_at:
                continue
            try:
                volume_column = columns.get("volume")
                volume = float(row[volume_column]) if volume_column is not None else 0.0
                candle = Candle(
                    timestamp=closed_at,
                    open=float(row[columns["open"]]),
                    high=float(row[columns["high"]]),
                    low=float(row[columns["low"]]),
                    close=float(row[columns["close"]]),
                    volume=max(0.0, volume),
                )
            except (KeyError, TypeError, ValueError):
                continue
            result.append(candle)
        result.sort(key=lambda bar: bar.timestamp)
        # Deduplicate a malformed/repeated index rather than passing invalid data
        # into MarketSnapshot, which intentionally requires strictly increasing bars.
        unique: dict[datetime, Candle] = {bar.timestamp: bar for bar in result}
        return tuple(unique[timestamp] for timestamp in sorted(unique))
    except Exception as exc:
        logger.debug("[gold-desk] OHLC conversion failed for {}: {}", tf, exc)
        return ()


class ApexGoldDeskShadowObserver:
    """Continuously analyse XAUUSD on fresh ticks without issuing orders.

    `venue` must be "MT5" or "DERIV". The observer verifies that the
    PlatformManager resolves the selected symbol to the same venue before it
    starts. This is important because a shared XAUUSD alias can otherwise be
    routed to MT5 while a parallel Deriv tick stream is also arriving.
    """

    def __init__(
        self,
        *,
        symbols: Iterable[str],
        venue: str,
        tick_router: Any,
        event_bus: Any,
        candle_fetcher: Callable[[str, str, int], Any],
        venue_resolver: Callable[[str], str],
        market_open: Callable[[str], bool] | None = None,
        coordinator: StrategyCoordinator | None = None,
        max_tick_age_seconds: float = 5.0,
    ) -> None:
        selected_venue = str(venue or "").strip().upper()
        if selected_venue not in {"MT5", "DERIV"}:
            raise ValueError("APEX Gold desk venue must be explicitly MT5 or DERIV")
        if max_tick_age_seconds <= 0:
            raise ValueError("max_tick_age_seconds must be positive")
        self.venue = selected_venue
        self.tick_router = tick_router
        self.event_bus = event_bus
        self.candle_fetcher = candle_fetcher
        self.venue_resolver = venue_resolver
        self.market_open = market_open
        self.coordinator = coordinator or StrategyCoordinator()
        self.gold_guard = GoldOnlyGuard()
        self.symbols = tuple(
            sorted({
                str(symbol).strip()
                for symbol in symbols
                if symbol and self.gold_guard.is_gold(str(symbol).strip())
            })
        )
        self.max_tick_age_seconds = float(max_tick_age_seconds)

        self._queue: Queue[Any] = Queue(maxsize=1)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._running = False
        self._lock = threading.RLock()
        self._bars: dict[tuple[str, str], tuple[Candle, ...]] = {}
        self._last_boundary: dict[tuple[str, str], int] = {}
        self._last_attempt: dict[tuple[str, str], float] = {}
        self._selected_symbols: set[str] = set()
        self._seen_keys: set[str] = set()
        self._seen_order: list[str] = []
        self.ticks_received = 0
        self.snapshots_evaluated = 0
        self.opportunities_emitted = 0
        self.stale_ticks = 0
        self.feed_errors = 0
        self.last_result_summary: dict[str, Any] = {}

    @staticmethod
    def _tick_venue(tick: Any) -> str | None:
        source = str(getattr(tick, "source", "") or "").strip().upper()
        if "MT5" in source:
            return "MT5"
        if "DERIV" in source:
            return "DERIV"
        return None

    def start(self) -> bool:
        """Warm the closed-bar cache and start the observer thread, fail closed."""
        if self._running:
            return True
        selected: set[str] = set()
        for symbol in self.symbols:
            try:
                connector_name = str(self.venue_resolver(symbol) or "").upper()
            except Exception as exc:
                logger.warning("[gold-desk] cannot resolve venue for {}: {}", symbol, exc)
                continue
            resolved_venue = (
                "MT5" if "MT5" in connector_name
                else "DERIV" if "DERIV" in connector_name
                else None
            )
            if resolved_venue != self.venue:
                logger.warning(
                    "[gold-desk] not starting {} observer: requested venue {}, "
                    "PlatformManager resolved {}", symbol, self.venue, resolved_venue or "unknown",
                )
                continue
            selected.add(symbol.upper())
        if not selected:
            logger.warning(
                "[gold-desk] shadow observer disabled: no Gold symbol on selected {} venue",
                self.venue,
            )
            return False

        self._selected_symbols = selected
        now = datetime.now(timezone.utc)
        for symbol in self.symbols:
            if symbol.upper() not in selected:
                continue
            for timeframe in _TIMEFRAMES:
                self._refresh(symbol, timeframe, now)

        self._stop.clear()
        self._running = True
        self.tick_router.register_callback(self._on_tick)
        self._thread = threading.Thread(
            target=self._run,
            name="apex-gold-desk-shadow",
            daemon=True,
        )
        self._thread.start()
        logger.info(
            "[gold-desk] shadow observer started: symbols={}, venue={}, "
            "strategies=8, live_orders=disabled",
            sorted(selected), self.venue,
        )
        return True

    def stop(self) -> None:
        if not self._running:
            return
        self._running = False
        try:
            self.tick_router.unregister_callback(self._on_tick)
        except Exception:
            pass
        self._stop.set()
        try:
            self._queue.put_nowait(None)
        except Full:
            pass
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2.0)
        self._thread = None
        logger.info(
            "[gold-desk] shadow observer stopped: ticks={}, evaluations={}, opportunities={}",
            self.ticks_received, self.snapshots_evaluated, self.opportunities_emitted,
        )

    def _on_tick(self, tick: Any) -> None:
        """Non-blocking tick callback; strategy work stays off the routing thread."""
        if not self._running:
            return
        try:
            symbol = str(getattr(tick, "symbol", "") or "").strip()
            if symbol.upper() not in self._selected_symbols:
                return
            if not self.gold_guard.is_gold(symbol):
                return
            if self._tick_venue(tick) != self.venue:
                return
            try:
                self._queue.put_nowait(tick)
            except Full:
                # Latest market observation wins; do not build a backlog of old ticks.
                try:
                    self._queue.get_nowait()
                except Empty:
                    pass
                try:
                    self._queue.put_nowait(tick)
                except Full:
                    pass
        except Exception:
            self.feed_errors += 1
            logger.exception("[gold-desk] tick enqueue failed")

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                tick = self._queue.get(timeout=0.5)
            except Empty:
                continue
            if tick is None:
                continue
            try:
                self._evaluate_tick(tick)
            except Exception:
                self.feed_errors += 1
                logger.exception("[gold-desk] analysis cycle failed; no order was sent")

    def _refresh(self, symbol: str, timeframe: str, observed_at: datetime) -> bool:
        key = (symbol.upper(), timeframe)
        try:
            frame = self.candle_fetcher(symbol, timeframe, _HISTORY_BARS)
            bars = _frame_to_closed_candles(frame, timeframe, observed_at)
            if not bars:
                return False
            with self._lock:
                self._bars[key] = bars
            return True
        except Exception as exc:
            logger.debug("[gold-desk] history refresh failed {}/{}: {}", symbol, timeframe, exc)
            return False

    def _evaluate_tick(self, tick: Any) -> None:
        received_at = datetime.now(timezone.utc)
        observed_at = _as_utc_datetime(getattr(tick, "timestamp", None))
        if observed_at is None:
            self.stale_ticks += 1
            return
        quote_age = (received_at - observed_at).total_seconds()
        if quote_age < -1.0 or quote_age > self.max_tick_age_seconds:
            self.stale_ticks += 1
            return
        symbol = str(getattr(tick, "symbol", "") or "").strip()
        bid, ask = float(getattr(tick, "bid", 0.0)), float(getattr(tick, "ask", 0.0))
        if bid <= 0 or ask <= 0 or ask < bid:
            self.feed_errors += 1
            return

        epoch = observed_at.timestamp()
        refresh: list[str] = []
        now_mono = time.monotonic()
        with self._lock:
            for timeframe, seconds in _TIMEFRAMES.items():
                key = (symbol.upper(), timeframe)
                boundary = int(epoch // seconds)
                bars = self._bars.get(key, ())
                last_attempt = self._last_attempt.get(key, 0.0)
                should_retry_missing = not bars and now_mono - last_attempt >= 5.0
                if self._last_boundary.get(key) != boundary or should_retry_missing:
                    self._last_boundary[key] = boundary
                    self._last_attempt[key] = now_mono
                    refresh.append(timeframe)
        for timeframe in refresh:
            self._refresh(symbol, timeframe, observed_at)

        with self._lock:
            candle_map = {
                timeframe: self._bars.get((symbol.upper(), timeframe), ())
                for timeframe in _TIMEFRAMES
            }
        is_open = True
        if self.market_open is not None:
            try:
                is_open = bool(self.market_open(symbol))
            except Exception:
                is_open = False
        snapshot = MarketSnapshot(
            symbol=symbol,
            venue=self.venue,
            observed_at=observed_at,
            bid=bid,
            ask=ask,
            candles=candle_map,
            market_open=is_open,
            data_quality_ok=bool(candle_map.get("M5")),
        )
        result = self.coordinator.evaluate(snapshot)
        self.ticks_received += 1
        self.snapshots_evaluated += 1
        self.last_result_summary = {
            "observed_at": observed_at.isoformat(),
            "symbol": symbol,
            "venue": self.venue,
            "idea_count": len(result.ideas),
            "diagnostics": list(result.diagnostics),
        }
        for idea in result.ideas:
            self._publish_idea(snapshot, idea, result.diagnostics)

    def _publish_idea(self, snapshot: MarketSnapshot, idea: TradeIdea, diagnostics: tuple[str, ...]) -> None:
        key = idea.signal_key
        if key in self._seen_keys:
            return
        record = {
            "observed_at": snapshot.observed_at.isoformat(),
            "symbol": idea.symbol,
            "venue": self.venue,
            "direction": idea.direction.value,
            "strategy_id": idea.strategy_id,
            "supporting_strategies": list(idea.supporting_strategies),
            "entry": idea.entry,
            "stop": idea.stop,
            "target": idea.target,
            "reward_to_risk": idea.reward_to_risk,
            "quality_score": idea.quality_score,
            "evidence": list(idea.evidence),
            "rationale": idea.rationale,
            "signal_key": key,
            "shadow_only": True,
            "orders_sent": False,
            "diagnostics": list(diagnostics),
        }
        self.event_bus.publish("gold_desk_opportunity", record)
        self.opportunities_emitted += 1
        self._seen_keys.add(key)
        self._seen_order.append(key)
        if len(self._seen_order) > 4096:
            evicted = self._seen_order.pop(0)
            self._seen_keys.discard(evicted)
        logger.info(
            "[gold-desk][SHADOW] {} {} {} entry={} stop={} target={} rr={:.2f} score={:.1f} "
            "families={} (no order sent)",
            self.venue, idea.symbol, idea.direction.value, idea.entry, idea.stop,
            idea.target, idea.reward_to_risk, idea.quality_score,
            list(idea.supporting_strategies or (idea.strategy_id,)),
        )

    def stats(self) -> dict[str, Any]:
        return {
            "running": self._running,
            "venue": self.venue,
            "symbols": sorted(self._selected_symbols),
            "ticks_received": self.ticks_received,
            "snapshots_evaluated": self.snapshots_evaluated,
            "opportunities_emitted": self.opportunities_emitted,
            "stale_ticks": self.stale_ticks,
            "feed_errors": self.feed_errors,
            "last_result": dict(self.last_result_summary),
        }
