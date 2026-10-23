"""Shadow-only bridge from Apex's live broker feed to its SINGLE CognitiveBrain.

The Gold pattern detectors are measurement instruments. They do not compete to
select a trade, assign a final direction, or publish an executable opportunity.
Their factual observations are consolidated into one neutral payload consumed by
CognitionLoop, where Apex's existing CognitiveBrain is the sole decision
authority. This observer has no broker order API.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from queue import Empty, Full, Queue
import hashlib
import json
import threading
import time
from typing import Any, Callable, Iterable, Sequence

from loguru import logger

from gold_desk.models import Candle, MarketSnapshot


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


def _frame_to_closed_candles(
    frame: Any, timeframe: str, observed_at: datetime,
) -> tuple[Candle, ...]:
    """Convert broker OHLC history to closed candles, excluding a forming bar.

    Broker adapters conventionally timestamp bars at their opening boundary.
    The trading kernel uses close timestamps, so the timeframe duration is
    added; any candle whose close lies after the current broker tick is rejected.
    """
    tf = timeframe.upper()
    seconds = _TIMEFRAMES.get(tf)
    if seconds is None or frame is None:
        return ()
    try:
        if len(frame) == 0:
            return ()
        columns = {str(column).lower(): column for column in list(getattr(frame, "columns", ()))}
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
                result.append(Candle(
                    timestamp=closed_at,
                    open=float(row[columns["open"]]),
                    high=float(row[columns["high"]]),
                    low=float(row[columns["low"]]),
                    close=float(row[columns["close"]]),
                    volume=max(0.0, volume),
                ))
            except (KeyError, TypeError, ValueError):
                continue
        result.sort(key=lambda bar: bar.timestamp)
        # De-duplicate malformed broker history rather than passing a broken
        # candle sequence to MarketSnapshot.
        unique: dict[datetime, Candle] = {bar.timestamp: bar for bar in result}
        return tuple(unique[timestamp] for timestamp in sorted(unique))
    except Exception as exc:
        logger.debug("[gold-desk] OHLC conversion failed for {}: {}", tf, exc)
        return ()


class ApexGoldDeskShadowObserver:
    """Continuously update Gold observations, then let CognitionLoop decide.

    `venue` must be "MT5" or "DERIV". The selected symbol must resolve through
    PlatformManager to that same venue, and ticks from other venues are ignored.
    This prevents mixing broker feeds when XAUUSD is available on both.
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
        pattern_detectors: Sequence[Any] | None = None,
        max_tick_age_seconds: float = 5.0,
    ) -> None:
        from gold_desk.strategies import default_pattern_detectors

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
        # These modules are read-only pattern detectors. Their TradeIdea-shaped
        # internal result is deliberately stripped down to factual observations
        # before the one evidence record reaches CognitionLoop.
        self._pattern_detectors = list(pattern_detectors or default_pattern_detectors())
        self.max_tick_age_seconds = float(max_tick_age_seconds)

        from gold_desk.symbols import GoldOnlyGuard
        self.gold_guard = GoldOnlyGuard()
        self.symbols = tuple(sorted({
            str(symbol).strip()
            for symbol in symbols
            if symbol and self.gold_guard.is_gold(str(symbol).strip())
        }))

        self._queue: Queue[Any] = Queue(maxsize=1)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._running = False
        self._lock = threading.RLock()
        self._bars: dict[tuple[str, str], tuple[Candle, ...]] = {}
        self._last_boundary: dict[tuple[str, str], int] = {}
        self._last_attempt: dict[tuple[str, str], float] = {}
        self._selected_symbols: set[str] = set()
        self._latest_market_evidence: dict[str, dict[str, Any]] = {}
        self._last_emitted_signature: dict[str, str] = {}
        self.ticks_received = 0
        self.snapshots_evaluated = 0
        self.evidence_events_emitted = 0
        self.stale_ticks = 0
        self.feed_errors = 0
        self.detector_errors = 0
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
        """Warm broker candles and start observing; fail closed on route mismatch."""
        if self._running:
            return True
        selected: set[str] = set()
        for symbol in self.symbols:
            try:
                connector_name = str(self.venue_resolver(symbol) or "").upper()
            except Exception as exc:
                logger.warning("[gold-desk] cannot resolve venue for {}: {}", symbol, exc)
                continue
            resolved = (
                "MT5" if "MT5" in connector_name
                else "DERIV" if "DERIV" in connector_name
                else None
            )
            if resolved != self.venue:
                logger.warning(
                    "[gold-desk] not observing {}: requested venue {}, PlatformManager resolved {}",
                    symbol, self.venue, resolved or "unknown",
                )
                continue
            selected.add(self.gold_guard.canonical_symbol(symbol) or symbol.upper())

        if not selected:
            logger.warning("[gold-desk] observer disabled: no Gold symbol on selected {} venue", self.venue)
            return False

        self._selected_symbols = selected
        now = datetime.now(timezone.utc)
        for symbol in self.symbols:
            if (self.gold_guard.canonical_symbol(symbol) or symbol.upper()) not in selected:
                continue
            for timeframe in _TIMEFRAMES:
                self._refresh(symbol, timeframe, now)

        self._stop.clear()
        self._running = True
        self.tick_router.register_callback(self._on_tick)
        self._thread = threading.Thread(
            target=self._run, name="apex-gold-market-observer", daemon=True,
        )
        self._thread.start()
        logger.info(
            "[gold-desk] market observer started: symbols={}, venue={}, "
            "detectors={}, decision_authority=CognitiveBrain, live_orders=disabled",
            sorted(selected), self.venue, len(self._pattern_detectors),
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
            "[gold-desk] observer stopped: ticks={}, evaluations={}, evidence_events={}",
            self.ticks_received, self.snapshots_evaluated, self.evidence_events_emitted,
        )

    def evidence_for(self, symbol: str) -> dict[str, Any] | None:
        """Return the latest broker-backed evidence payload to CognitionLoop."""
        if not self.gold_guard.is_gold(symbol):
            return None
        with self._lock:
            payload = self._latest_market_evidence.get(str(symbol).strip().upper())
            if payload is None:
                # Gold aliases are canonicalised at the broker boundary.
                payload = self._latest_market_evidence.get("XAUUSD")
            if payload is None:
                return None
            copied = dict(payload)
            copied["quote"] = dict(payload.get("quote") or {})
            copied["completed_bars"] = {
                tf: [dict(bar) for bar in bars]
                for tf, bars in (payload.get("completed_bars") or {}).items()
            }
            copied["pattern_observations"] = [
                {"family": item["family"], "facts": list(item["facts"]),
                 "source_bar_time": item["source_bar_time"]}
                for item in payload.get("pattern_observations", [])
            ]
        return copied

    def _on_tick(self, tick: Any) -> None:
        """Non-blocking callback; all analysis runs off the tick-routing thread."""
        if not self._running:
            return
        try:
            symbol = str(getattr(tick, "symbol", "") or "").strip()
            canonical = self.gold_guard.canonical_symbol(symbol)
            if canonical is None or canonical not in self._selected_symbols:
                return
            if self._tick_venue(tick) != self.venue:
                return
            try:
                self._queue.put_nowait(tick)
            except Full:
                # Latest broker observation wins; never process a backlog of old ticks.
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
                logger.exception("[gold-desk] market evidence cycle failed")

    def _refresh(self, symbol: str, timeframe: str, observed_at: datetime) -> bool:
        key = (self.gold_guard.canonical_symbol(symbol) or symbol.upper(), timeframe)
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

    @staticmethod
    def _bar_facts(bars: Sequence[Candle], limit: int = 3) -> list[dict[str, Any]]:
        return [
            {
                "close_time": bar.timestamp.isoformat(),
                "open": bar.open,
                "high": bar.high,
                "low": bar.low,
                "close": bar.close,
                "volume": bar.volume,
            }
            for bar in bars[-limit:]
        ]

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

        original_symbol = str(getattr(tick, "symbol", "") or "").strip()
        symbol = self.gold_guard.canonical_symbol(original_symbol)
        if symbol != "XAUUSD" or symbol not in self._selected_symbols:
            return
        bid, ask = float(getattr(tick, "bid", 0.0)), float(getattr(tick, "ask", 0.0))
        if bid <= 0 or ask <= 0 or ask < bid:
            self.feed_errors += 1
            return

        epoch = observed_at.timestamp()
        refresh: list[str] = []
        now_mono = time.monotonic()
        with self._lock:
            for timeframe, seconds in _TIMEFRAMES.items():
                key = (symbol, timeframe)
                boundary = int(epoch // seconds)
                bars = self._bars.get(key, ())
                last_attempt = self._last_attempt.get(key, 0.0)
                missing_retry = not bars and now_mono - last_attempt >= 5.0
                if self._last_boundary.get(key) != boundary or missing_retry:
                    self._last_boundary[key] = boundary
                    self._last_attempt[key] = now_mono
                    refresh.append(timeframe)
        for timeframe in refresh:
            self._refresh(symbol, timeframe, observed_at)

        with self._lock:
            candle_map = {tf: self._bars.get((symbol, tf), ()) for tf in _TIMEFRAMES}

        market_is_open = False
        if self.market_open is not None:
            try:
                market_is_open = bool(self.market_open(original_symbol))
            except Exception:
                market_is_open = False
        else:
            market_is_open = True

        snapshot = MarketSnapshot(
            symbol=symbol,
            venue=self.venue,
            observed_at=observed_at,
            bid=bid,
            ask=ask,
            candles=candle_map,
            market_open=market_is_open,
            data_quality_ok=bool(candle_map.get("M5")) and quote_age <= self.max_tick_age_seconds,
        )

        # Run each measurement instrument, but do not elect a strategy winner.
        # Candidate direction, score, entry, stop and target are intentionally
        # stripped; only raw pattern facts and their source bar remain.
        observations: list[dict[str, Any]] = []
        for detector in self._pattern_detectors:
            family = str(getattr(detector, "pattern_id", type(detector).__name__) or type(detector).__name__)
            try:
                for finding in detector.evaluate(snapshot) or ():
                    facts = tuple(str(item)[:240] for item in getattr(finding, "evidence", ()) if str(item).strip())
                    if not facts:
                        facts = ("pattern preconditions detected on completed bars",)
                    source_time = getattr(finding, "source_bar_time", observed_at)
                    observations.append({
                        "family": family,
                        "facts": list(facts[:8]),
                        "source_bar_time": source_time.isoformat() if hasattr(source_time, "isoformat") else str(source_time),
                    })
            except Exception as exc:
                self.detector_errors += 1
                logger.warning("[gold-desk] detector {} failed: {}", family, exc)

        completed_bars = {
            tf: self._bar_facts(candle_map.get(tf, ()))
            for tf in _TIMEFRAMES
            if candle_map.get(tf)
        }
        payload: dict[str, Any] = {
            "observed_at": observed_at.isoformat(),
            "symbol": symbol,
            "venue": self.venue,
            "market_open": market_is_open,
            "data_quality_ok": snapshot.data_quality_ok,
            "quote": {"bid": bid, "ask": ask, "spread": ask - bid},
            "completed_bars": completed_bars,
            "pattern_observations": observations[:12],
            "data_source": "selected_broker_tick_and_completed_ohlc",
            "decision_authority": "CognitiveBrain",
            "overlap_policy": "evidence_only_no_vote_summing",
            "orders_sent": False,
        }
        signature_payload = {
            "symbol": symbol,
            "venue": self.venue,
            "completed_bar_times": {
                tf: bars[-1]["close_time"] for tf, bars in completed_bars.items() if bars
            },
            "patterns": observations[:12],
            "market_open": market_is_open,
        }
        signature = hashlib.sha256(
            json.dumps(signature_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()

        with self._lock:
            self._latest_market_evidence[symbol] = payload
            prior_signature = self._last_emitted_signature.get(symbol)
            if signature != prior_signature:
                self._last_emitted_signature[symbol] = signature
                should_publish = True
            else:
                should_publish = False
        self.ticks_received += 1
        self.snapshots_evaluated += 1
        self.last_result_summary = {
            "observed_at": observed_at.isoformat(),
            "symbol": symbol,
            "venue": self.venue,
            "pattern_observation_count": len(observations),
            "decision_authority": "CognitiveBrain",
            "orders_sent": False,
        }
        # This event is for monitoring/audit. CognitionLoop reads the same latest
        # payload directly; it never consumes a selected trade or direction vote.
        if should_publish:
            self.event_bus.publish("gold_market_evidence", payload)
            self.evidence_events_emitted += 1
            logger.info(
                "[gold-desk][EVIDENCE] {} {}: {} factual pattern observation(s); "
                "authority=CognitiveBrain; no order/decision emitted",
                self.venue, symbol, len(observations),
            )

    def stats(self) -> dict[str, Any]:
        return {
            "running": self._running,
            "venue": self.venue,
            "symbols": sorted(self._selected_symbols),
            "ticks_received": self.ticks_received,
            "snapshots_evaluated": self.snapshots_evaluated,
            "evidence_events_emitted": self.evidence_events_emitted,
            "stale_ticks": self.stale_ticks,
            "feed_errors": self.feed_errors,
            "detector_errors": self.detector_errors,
            "last_result": dict(self.last_result_summary),
            "decision_authority": "CognitiveBrain",
            "live_orders_enabled": False,
        }
