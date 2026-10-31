"""Shadow-only bridge from Apex's live broker feed to its SINGLE CognitiveBrain.

The pattern detectors are measurement instruments, run for every enabled instrument. They do not compete to
select a trade, assign a final direction, or publish an executable opportunity.
Their factual observations are consolidated into one neutral payload consumed by
CognitionLoop, where Apex's existing CognitiveBrain is the sole decision
authority. This observer has no broker order API.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
import threading
import time
from typing import Any, Callable, Iterable, Mapping, Sequence

from loguru import logger

from gold_desk.indicators import atr
from gold_desk.models import Candle, EventWindow, MarketSnapshot


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
            # A naive provider timestamp has no timezone provenance. Do not
            # silently reinterpret it as UTC or give it source-time authority.
            if value.tzinfo is None or value.utcoffset() is None:
                return None
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
        logger.debug("[market-observer] OHLC conversion failed for {}: {}", tf, exc)
        return ()


class ApexMarketObserver:
    """Continuously update factual market observations for every enabled instrument.

    `venue` is "MT5", "DERIV" or "AUTO". With a fixed venue each symbol must resolve
    through PlatformManager to that venue and ticks from other venues are ignored
    (this prevents mixing broker feeds when a symbol exists on both). With "AUTO"
    each symbol is observed on whichever venue PlatformManager routes it to. The
    observer emits facts only; CognitionLoop and the Brain decide.
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
        event_window_source: Callable[
            [str, datetime, Mapping[str, Sequence[Candle]]], EventWindow | None
        ] | None = None,
        pattern_detectors: Sequence[Any] | None = None,
        max_tick_age_seconds: float = 5.0,
        min_eval_interval_seconds: float = 1.0,
    ) -> None:
        from gold_desk.strategies import default_pattern_detectors

        selected_venue = str(venue or "").strip().upper()
        if selected_venue not in {"MT5", "DERIV", "AUTO"}:
            raise ValueError("APEX market observer venue must be MT5, DERIV or AUTO")
        if max_tick_age_seconds <= 0:
            raise ValueError("max_tick_age_seconds must be positive")

        self.venue = selected_venue
        self.tick_router = tick_router
        self.event_bus = event_bus
        self.candle_fetcher = candle_fetcher
        self.venue_resolver = venue_resolver
        self.market_open = market_open
        self.event_window_source = event_window_source
        # These modules only report facts and measurements. Their interface has
        # no trade direction or executable candidate object.
        self._pattern_detectors = list(pattern_detectors or default_pattern_detectors())
        self.max_tick_age_seconds = float(max_tick_age_seconds)

        from gold_desk.symbols import SymbolPolicy
        self.policy = SymbolPolicy()
        self.symbols = tuple(sorted({
            str(symbol).strip()
            for symbol in symbols
            if symbol and self.policy.is_observable(str(symbol).strip())
        }))

        # Latest tick per symbol (coalesced) so a busy instrument can never starve
        # another; each symbol is evaluated at most every min_eval_interval seconds.
        self._pending_ticks: dict[str, Any] = {}
        self._last_eval: dict[str, float] = {}
        self.min_eval_interval_seconds = float(min_eval_interval_seconds)
        self._wakeup = threading.Event()
        self._venue_by_symbol: dict[str, str] = {}
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
        self._last_brain_wake_state: dict[str, dict[str, float]] = {}
        self._evidence_change_callback: Callable[[str], Any] | None = None
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
                logger.warning("[market-observer] cannot resolve venue for {}: {}", symbol, exc)
                continue
            resolved = (
                "MT5" if "MT5" in connector_name
                else "DERIV" if "DERIV" in connector_name
                else None
            )
            if resolved is None or (self.venue != "AUTO" and resolved != self.venue):
                logger.warning(
                    "[market-observer] not observing {}: requested venue {}, PlatformManager resolved {}",
                    symbol, self.venue, resolved or "unknown",
                )
                continue
            canonical = self.policy.canonical_symbol(symbol) or symbol.upper()
            selected.add(canonical)
            self._venue_by_symbol[canonical] = resolved

        if not selected:
            logger.warning("[market-observer] observer disabled: no symbol routes to venue {}", self.venue)
            return False

        self._selected_symbols = selected
        now = datetime.now(timezone.utc)
        for symbol in self.symbols:
            if (self.policy.canonical_symbol(symbol) or symbol.upper()) not in selected:
                continue
            for timeframe in _TIMEFRAMES:
                self._refresh(symbol, timeframe, now)

        self._stop.clear()
        self._running = True
        self.tick_router.register_callback(self._on_tick)
        self._thread = threading.Thread(
            target=self._run, name="apex-market-observer", daemon=True,
        )
        self._thread.start()
        logger.info(
            "[market-observer] market observer started: symbols={}, venue={}, "
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
        self._wakeup.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2.0)
        self._thread = None
        logger.info(
            "[market-observer] observer stopped: ticks={}, evaluations={}, evidence_events={}",
            self.ticks_received, self.snapshots_evaluated, self.evidence_events_emitted,
        )

    def evidence_for(self, symbol: str) -> dict[str, Any] | None:
        """Return the latest broker-backed evidence payload to CognitionLoop."""
        key = self.policy.canonical_symbol(symbol)
        if key is None:
            return None
        with self._lock:
            payload = self._latest_market_evidence.get(key)
            if payload is None:
                return None
            copied = dict(payload)
            copied["quote"] = dict(payload.get("quote") or {})
            copied["completed_bars"] = {
                tf: [dict(bar) for bar in bars]
                for tf, bars in (payload.get("completed_bars") or {}).items()
            }
            copied["event_context"] = (
                dict(payload["event_context"]) if isinstance(payload.get("event_context"), dict) else None
            )
            copied["pattern_observations"] = [
                {
                    "family": item["family"],
                    "facts": list(item["facts"]),
                    "measurements": dict(item.get("measurements") or {}),
                    "timeframe": item.get("timeframe", ""),
                    "source_bar_time": item["source_bar_time"],
                }
                for item in payload.get("pattern_observations", [])
            ]
        return copied

    def set_evidence_change_callback(self, callback: Callable[[str], Any] | None) -> None:
        """Register the single-Brain wake callback for material market changes.

        The callback must enqueue work only. It runs on the observer worker thread;
        market-data/tick routing is never blocked by Brain reasoning.
        """
        self._evidence_change_callback = callback

    def is_feed_healthy(
        self,
        symbol: str,
        *,
        max_age_seconds: float = 15.0,
        now_epoch: float | None = None,
    ) -> bool:
        """Fail closed unless a recent, open-market quote/bar snapshot exists.

        Used by the shared ActionExecutor immediately before a new OPEN. This is
        operational data-integrity validation, not a pattern score or decision.
        It intentionally does not block CLOSE or other risk-reducing intents.
        """
        key = self.policy.canonical_symbol(symbol)
        if key is None or max_age_seconds <= 0:
            return False
        with self._lock:
            payload = self._latest_market_evidence.get(key)
            if payload is None:
                return False
            # Copy only primitive fields while holding the lock.
            observed_at = str(payload.get("observed_at", "") or "")
            quality_ok = bool(payload.get("data_quality_ok", False))
            market_open = bool(payload.get("market_open", False))
            quote = dict(payload.get("quote") or {})
            bars = dict(payload.get("completed_bars") or {})
        try:
            stamp = datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
            if stamp.tzinfo is None or stamp.utcoffset() is None:
                return False
            current_epoch = time.time() if now_epoch is None else float(now_epoch)
            age = current_epoch - stamp.timestamp()
            bid, ask = float(quote["bid"]), float(quote["ask"])
            m5_bars = bars.get("M5")
            if not isinstance(m5_bars, list) or not m5_bars or not isinstance(m5_bars[-1], dict):
                return False
            latest_m5 = datetime.fromisoformat(
                str(m5_bars[-1].get("close_time", "")).replace("Z", "+00:00")
            )
            if latest_m5.tzinfo is None or latest_m5.utcoffset() is None:
                return False
            bar_age = current_epoch - latest_m5.timestamp()
        except (TypeError, ValueError, KeyError, OverflowError, OSError):
            return False
        return bool(
            quality_ok
            and market_open
            and -1.0 <= age <= float(max_age_seconds)
            and -1.0 <= bar_age <= 2.0 * _TIMEFRAMES["M5"] + 15.0
            and bid > 0.0
            and ask >= bid
        )

    def _on_tick(self, tick: Any) -> None:
        """Non-blocking callback; all analysis runs off the tick-routing thread."""
        if not self._running:
            return
        try:
            symbol = str(getattr(tick, "symbol", "") or "").strip()
            canonical = self.policy.canonical_symbol(symbol)
            if canonical is None or canonical not in self._selected_symbols:
                return
            expected_venue = self._venue_by_symbol.get(canonical) or (
                None if self.venue == "AUTO" else self.venue
            )
            if expected_venue is not None and self._tick_venue(tick) != expected_venue:
                return
            # Latest broker observation per symbol wins; never process a backlog.
            with self._lock:
                self._pending_ticks[canonical] = tick
            self._wakeup.set()
        except Exception:
            self.feed_errors += 1
            logger.exception("[market-observer] tick enqueue failed")

    def _run(self) -> None:
        import time as _time

        while not self._stop.is_set():
            self._wakeup.wait(timeout=0.5)
            self._wakeup.clear()
            with self._lock:
                batch = list(self._pending_ticks.items())
            deferred = False
            for symbol, tick in batch:
                if self._stop.is_set():
                    return
                now = _time.monotonic()
                if now - self._last_eval.get(symbol, 0.0) < self.min_eval_interval_seconds:
                    deferred = True
                    continue
                with self._lock:
                    # Only drop the tick we are about to evaluate (a newer one may have arrived).
                    if self._pending_ticks.get(symbol) is tick:
                        del self._pending_ticks[symbol]
                self._last_eval[symbol] = now
                try:
                    self._evaluate_tick(tick)
                except Exception:
                    self.feed_errors += 1
                    logger.exception("[market-observer] market evidence cycle failed")
            if deferred:
                _time.sleep(0.05)
                self._wakeup.set()

    def _refresh(self, symbol: str, timeframe: str, observed_at: datetime) -> bool:
        key = (self.policy.canonical_symbol(symbol) or symbol.upper(), timeframe)
        try:
            frame = self.candle_fetcher(symbol, timeframe, _HISTORY_BARS)
            bars = _frame_to_closed_candles(frame, timeframe, observed_at)
            if not bars:
                return False
            with self._lock:
                self._bars[key] = bars
            return True
        except Exception as exc:
            logger.debug("[market-observer] history refresh failed {}/{}: {}", symbol, timeframe, exc)
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
        if getattr(tick, "source_timestamp_verified", False) is not True:
            self.stale_ticks += 1
            return
        observed_at = _as_utc_datetime(getattr(tick, "timestamp", None))
        if observed_at is None:
            self.stale_ticks += 1
            return
        quote_age = (received_at - observed_at).total_seconds()
        if quote_age < -1.0 or quote_age > self.max_tick_age_seconds:
            self.stale_ticks += 1
            return

        original_symbol = str(getattr(tick, "symbol", "") or "").strip()
        symbol = self.policy.canonical_symbol(original_symbol)
        if symbol is None or symbol not in self._selected_symbols:
            return
        venue = self._venue_by_symbol.get(symbol) or (None if self.venue == "AUTO" else self.venue)
        if venue is None:
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

        event_window = None
        if self.event_window_source is not None:
            try:
                event_window = self.event_window_source(symbol, observed_at, candle_map)
            except Exception as exc:
                self.feed_errors += 1
                logger.debug("[market-observer] event-window source failed: {}", exc)
        snapshot = MarketSnapshot(
            symbol=symbol,
            venue=venue,
            observed_at=observed_at,
            bid=bid,
            ask=ask,
            candles=candle_map,
            event_window=event_window,
            market_open=market_is_open,
            data_quality_ok=bool(candle_map.get("M5")) and quote_age <= self.max_tick_age_seconds,
        )

        # Each module emits facts only. The interface has no direction, entry,
        # stop, target, strategy score, or order object to compete with the Brain.
        observations: list[dict[str, Any]] = []
        for detector in self._pattern_detectors:
            family = str(getattr(detector, "pattern_id", type(detector).__name__) or type(detector).__name__)
            try:
                for finding in detector.observe(snapshot) or ():
                    facts = tuple(str(item)[:240] for item in finding.facts if str(item).strip())
                    measurements = dict(finding.measurements)
                    observations.append({
                        "family": finding.pattern_id,
                        "facts": list(facts[:12]),
                        "measurements": measurements,
                        "timeframe": finding.timeframe,
                        "source_bar_time": finding.source_bar_time.isoformat(),
                    })
            except Exception as exc:
                self.detector_errors += 1
                logger.warning("[market-observer] detector {} failed: {}", family, exc)

        completed_bars = {
            tf: self._bar_facts(candle_map.get(tf, ()))
            for tf in _TIMEFRAMES
            if candle_map.get(tf)
        }
        payload: dict[str, Any] = {
            "observed_at": observed_at.isoformat(),
            "symbol": symbol,
            "venue": venue,
            "market_open": market_is_open,
            "data_quality_ok": snapshot.data_quality_ok,
            "quote": {"bid": bid, "ask": ask, "spread": ask - bid},
            "completed_bars": completed_bars,
            "event_context": ({
                "name": event_window.name,
                "impact": event_window.impact,
                "starts_at": event_window.starts_at.isoformat(),
                "ends_at": event_window.ends_at.isoformat(),
                "pre_event_high": event_window.pre_event_high,
                "pre_event_low": event_window.pre_event_low,
            } if event_window is not None else None),
            "pattern_observations": observations[:12],
            "data_source": "selected_broker_tick_and_completed_ohlc",
            "decision_authority": "CognitiveBrain",
            "overlap_policy": "evidence_only_no_vote_summing",
            "orders_sent": False,
        }
        signature_payload = {
            "symbol": symbol,
            "venue": venue,
            "completed_bar_times": {
                tf: bars[-1]["close_time"] for tf, bars in completed_bars.items() if bars
            },
            "patterns": observations[:12],
            "market_open": market_is_open,
        }
        signature = hashlib.sha256(
            json.dumps(signature_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        m5_atr = atr(snapshot.bars("M5"), 14) or 0.0
        current_mid = snapshot.mid
        current_spread = snapshot.spread

        with self._lock:
            self._latest_market_evidence[symbol] = payload
            prior_signature = self._last_emitted_signature.get(symbol)
            if signature != prior_signature:
                self._last_emitted_signature[symbol] = signature
                should_publish = True
            else:
                should_publish = False

            prior_wake = self._last_brain_wake_state.get(symbol)
            if prior_wake is None or should_publish:
                should_wake = True
            else:
                # Scale-free (per-instrument) thresholds: ATR/spread relative, with a
                # tiny price-relative floor so a zero-ATR/zero-spread feed still wakes.
                tiny = max(abs(current_mid) * 1e-6, 1e-12)
                price_threshold = max(0.25 * m5_atr, 3.0 * current_spread, tiny)
                moved_materially = abs(current_mid - prior_wake["mid"]) >= price_threshold
                prior_spread = prior_wake["spread"]
                spread_changed = abs(current_spread - prior_spread) >= max(0.50 * prior_spread, tiny)
                should_wake = moved_materially or spread_changed

            if should_wake:
                self._last_brain_wake_state[symbol] = {
                    "mid": current_mid,
                    "spread": current_spread,
                }
            wake_callback = self._evidence_change_callback
        self.ticks_received += 1
        self.snapshots_evaluated += 1
        self.last_result_summary = {
            "observed_at": observed_at.isoformat(),
            "symbol": symbol,
            "venue": venue,
            "pattern_observation_count": len(observations),
            "decision_authority": "CognitiveBrain",
            "orders_sent": False,
        }
        # This event is for monitoring/audit. CognitionLoop reads the same latest
        # payload directly; it never consumes a selected trade or direction vote.
        if should_publish:
            self.event_bus.publish("market_evidence", payload)
            self.evidence_events_emitted += 1
            logger.info(
                "[market-observer][EVIDENCE] {} {}: {} factual pattern observation(s); "
                "authority=CognitiveBrain; no order/decision emitted",
                venue, symbol, len(observations),
            )
        if should_wake and wake_callback is not None:
            try:
                # CognitionLoop.request_reason only queues a symbol; it never
                # runs the Brain or performs broker I/O on this worker thread.
                wake_callback(symbol)
            except Exception as exc:
                logger.debug("[market-observer] Brain wake callback failed: {}", exc)

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


# Backwards-compatible name for older imports.
ApexGoldDeskShadowObserver = ApexMarketObserver
