"""APEX TRADER — Tick-source threads (extracted from the bootstrap).

Phase K (Constitution Part XI — modular design): the MT5 poller and Deriv
adapter tick-source threads were defined inline in the 11k-line
``event_driven_bootstrap.py``. They are self-contained daemons that read prices
from the platform layer and inject :class:`~tick.Tick` objects into the
``TickRouter``; lifting them into a named, unit-tested module is part of the
behaviour-preserving decomposition. The bootstrap re-imports these names, so
every construction site (and the existing
``from event_driven_bootstrap import MT5TickPoller`` / ``DerivTickAdapter`` test
import paths) resolves identically — behaviour unchanged.
"""

from __future__ import annotations

import json
import math
import threading
import time as _time
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, Callable, Optional

from loguru import logger

from tick import Tick

if TYPE_CHECKING:  # type hints only — not needed (and not imported) at runtime
    from platforms.platform_manager import PlatformManager
    from tick import TickRouter


class MT5TickPoller:
    """Polls MT5 prices and injects Tick objects into the TickRouter."""

    def __init__(
        self,
        platform_manager: PlatformManager,
        tick_router: TickRouter,
        symbols: list[str],
        poll_interval: float = 0.05,
        should_poll: Optional[Callable[[str], bool]] = None,
        checkpoint_store: Optional[Any] = None,
    ) -> None:
        self._pm = platform_manager
        self._router = tick_router
        self._symbols = list(symbols)
        self._interval = poll_interval
        # Gate that returns False when a symbol's market is currently closed
        # (e.g. session-gated FX/commodity/index instruments on the weekend).
        # Closed symbols are skipped before any broker call, so they neither
        # emit stale-tick warnings nor accrue toward permanent removal.
        self._should_poll = should_poll or (lambda _sym: True)
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._error_counts: dict[str, int] = {}
        self._last_error_log: dict[str, float] = {}
        # Per-symbol UTC cursor and bounded event fingerprints prevent overlap
        # duplicates when replaying the broker's historical quote tape.
        self._history_cursor: dict[str, datetime] = {}
        self._history_seen: dict[str, set[tuple]] = {}
        self._history_seen_order: dict[str, deque] = {}
        self._history_seen_limit = 20_000
        self._history_gap_count: dict[str, int] = {}
        self._history_broker_identity: dict[str, str] = {}
        self._checkpoint_store = checkpoint_store
        self._restored_history_checkpoints: dict[str, dict[str, Any]] = {}
        self._checkpoint_restore_attempted: set[str] = set()
        self._checkpoint_error_logged: set[str] = set()
        self._last_checkpoint_cursor: dict[str, datetime] = {}
        self._history_gap_reported: set[tuple[str, str, str, str]] = set()
        self._restore_history_checkpoints()

    def _restore_history_checkpoints(self) -> None:
        """Load the latest durable per-symbol replay checkpoint from EventStore."""
        store = self._checkpoint_store
        if store is None:
            return
        try:
            from persistence import domain_events as DE
            rows = store.query_events(
                severity_min="INFO",
                event_types=[DE.MT5_TICK_RECOVERY_CHECKPOINT],
                limit=10000,
                raise_on_error=True,
            )
            for row in rows:
                symbol = str(row.get("symbol") or "").strip().upper()
                if not symbol or symbol in self._restored_history_checkpoints:
                    continue
                payload = row.get("payload_json")
                payload = json.loads(payload) if isinstance(payload, str) else payload
                if isinstance(payload, dict) and payload.get("schema") == "apex.mt5_tick_checkpoint.v1":
                    self._restored_history_checkpoints[symbol] = payload
        except Exception as exc:
            logger.error("[mt5-poller] durable history checkpoint restore failed: {}", exc)

    def _apply_history_checkpoint(self, symbol: str, broker_server: str) -> None:
        if symbol in self._checkpoint_restore_attempted:
            return
        self._checkpoint_restore_attempted.add(symbol)
        payload = self._restored_history_checkpoints.pop(symbol, None)
        if not payload:
            return
        if str(payload.get("broker_server") or "") != broker_server:
            self._history_gap_count[symbol] = self._history_gap_count.get(symbol, 0) + 1
            logger.error(
                "[mt5-poller] {} persisted history cursor belongs to a different broker server; "
                "checkpoint rejected and history will re-anchor without replaying foreign data",
                symbol,
            )
            return
        try:
            cursor = datetime.fromisoformat(str(payload["cursor_utc"]))
            if cursor.tzinfo is None:
                raise ValueError("checkpoint cursor is timezone-naive")
            self._history_cursor[symbol] = cursor.astimezone(timezone.utc)
            self._last_checkpoint_cursor[symbol] = cursor.astimezone(timezone.utc)
            fingerprints = payload.get("recent_fingerprints", [])
            if isinstance(fingerprints, list):
                for item in fingerprints[-self._history_seen_limit:]:
                    if isinstance(item, list) and len(item) == 4:
                        self._remember_history_tick(symbol, tuple(item))
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            self._history_gap_count[symbol] = self._history_gap_count.get(symbol, 0) + 1
            logger.error("[mt5-poller] {} persisted history checkpoint invalid: {}", symbol, exc)

    def _persist_history_checkpoint(
        self, symbol: str, cursor: datetime, broker_server: str,
    ) -> bool:
        store = self._checkpoint_store
        if store is None or not broker_server:
            return False
        previous = self._last_checkpoint_cursor.get(symbol)
        if previous is not None and cursor <= previous:
            return True
        if previous is not None and (cursor - previous).total_seconds() < 5.0:
            return True
        order = self._history_seen_order.get(symbol, deque())
        fingerprints = [list(key) for key in list(order)[-256:]]
        try:
            from persistence import domain_events as DE
            store.emit(
                event_type=DE.MT5_TICK_RECOVERY_CHECKPOINT,
                severity="INFO",
                symbol=symbol,
                source_module="tick.mt5_poller",
                payload={
                    "schema": "apex.mt5_tick_checkpoint.v1",
                    "broker_server": broker_server,
                    "cursor_utc": cursor.astimezone(timezone.utc).isoformat(),
                    "recent_fingerprints": fingerprints,
                },
                durable=True,
            )
            self._last_checkpoint_cursor[symbol] = cursor.astimezone(timezone.utc)
            return True
        except Exception as exc:
            if symbol not in self._checkpoint_error_logged:
                self._checkpoint_error_logged.add(symbol)
                logger.critical(
                    "[mt5-poller] {} history checkpoint could not be committed; "
                    "cursor will not advance durably: {}",
                    symbol, exc,
                )
            return False

    def _persist_history_gap(
        self, symbol: str, start: datetime, end: datetime,
        broker_server: str, reason: str,
    ) -> None:
        """Durably record a range that could not be verified or replayed."""
        store = self._checkpoint_store
        if store is None or not broker_server:
            return
        identity = (
            symbol, broker_server, start.astimezone(timezone.utc).isoformat(),
            end.astimezone(timezone.utc).isoformat(),
        )
        if identity in self._history_gap_reported:
            return
        try:
            from persistence import domain_events as DE
            store.emit(
                event_type=DE.MT5_TICK_RECOVERY_GAP,
                severity="ERROR",
                symbol=symbol,
                source_module="tick.mt5_poller",
                payload={
                    "schema": "apex.mt5_tick_gap.v1",
                    "broker_server": broker_server,
                    "from_utc": identity[2],
                    "to_utc": identity[3],
                    "reason": str(reason),
                },
                durable=True,
            )
            # Only suppress duplicate writes after the durable store confirms
            # success. A transient EventStore failure must remain retryable.
            self._history_gap_reported.add(identity)
        except Exception as exc:
            logger.critical(
                "[mt5-poller] {} unrecoverable history gap could not be durably recorded: {}",
                symbol, exc,
            )

    def _remember_history_tick(self, symbol: str, key: tuple) -> bool:
        seen = self._history_seen.setdefault(symbol, set())
        order = self._history_seen_order.setdefault(symbol, deque())
        if key in seen:
            return False
        if len(order) >= self._history_seen_limit:
            seen.discard(order.popleft())
        order.append(key)
        seen.add(key)
        return True

    @staticmethod
    def _tick_key(time_msc: Any, bid: Any, ask: Any, flags: Any = 0) -> tuple:
        try:
            stamp = int(time_msc)
            bid_value = float(bid)
            ask_value = float(ask)
            flag_value = int(flags or 0)
            if not (math.isfinite(bid_value) and math.isfinite(ask_value)):
                raise ValueError("non-finite MT5 quote identity")
            bid_value = round(bid_value, 12)
            ask_value = round(ask_value, 12)
        except (TypeError, ValueError, OverflowError):
            raise ValueError("invalid MT5 quote tick identity")
        return stamp, bid_value, ask_value, flag_value

    def _route_latest_quote(self, symbol: str, quote: Any) -> bool:
        """Route the verified latest quote as a continuity fallback."""
        timestamp = getattr(quote, "time", None)
        raw = getattr(quote, "raw_fields", {})
        raw = dict(raw) if isinstance(raw, dict) else {}
        if not isinstance(timestamp, datetime):
            return False
        try:
            bid = float(getattr(quote, "bid", None))
            ask = float(getattr(quote, "ask", None))
        except (TypeError, ValueError, OverflowError):
            return False
        if not (
            math.isfinite(bid) and math.isfinite(ask)
            and bid > 0.0 and ask > 0.0
        ):
            return False

        # Deduplication uses the normalized UTC event instant, never the
        # provider's raw epoch. MT5 latest-quote time_msc can be broker-server
        # time while copy_ticks_range history is UTC; comparing those raw
        # values makes the same quote appear to be two different events.
        time_msc = raw.get("timestamp_utc_epoch")
        if time_msc is not None:
            try:
                time_msc = int(round(float(time_msc) * 1000.0))
            except (TypeError, ValueError, OverflowError):
                time_msc = None
        if time_msc is None and timestamp.tzinfo is not None:
            # TickData.timestamp is already normalized and timezone-aware.
            time_msc = int(timestamp.astimezone(timezone.utc).timestamp() * 1000)
        if time_msc is not None:
            key = self._tick_key(time_msc, bid, ask, raw.get("flags", 0))
            if not self._remember_history_tick(symbol, key):
                return False
        tick = Tick(
            symbol=symbol,
            bid=bid,
            ask=ask,
            timestamp=timestamp,
            source="mt5",
            source_timestamp_verified=(
                getattr(quote, "source_timestamp_verified", False) is True
            ),
            quote_kind=getattr(quote, "quote_kind", "unknown"),
            spread_unit=getattr(quote, "spread_unit", "unknown"),
            raw_fields=raw,
        )
        self._router.on_tick(tick)
        return True

    def _route_history_quotes(
        self, symbol: str, latest_quote: Any, *,
        _range_start: Optional[datetime] = None,
        _range_end: Optional[datetime] = None,
    ) -> bool:
        """Replay all verified bid/ask ticks since the last UTC cursor.

        MT5's latest-quote polling can skip intermediate updates. The history
        API returns the broker's available quote tape, so the poller requests an
        overlapping range, validates the adapter's UTC attestation, deduplicates
        boundary repeats and routes every usable bid/ask event. Trade-only rows
        are not converted into synthetic bid/ask quotes.
        """
        fetch = getattr(self._pm, "get_tick_history", None)
        if not callable(fetch):
            return False
        if _range_start is None or _range_end is None:
            end = getattr(latest_quote, "time", None)
            if not isinstance(end, datetime) or end.tzinfo is None:
                return False
            end = end.astimezone(timezone.utc)
            latest_raw = getattr(latest_quote, "raw_fields", {})
            latest_raw = latest_raw if isinstance(latest_raw, dict) else {}
            broker_server = str(latest_raw.get("broker_server_identity") or "").strip()
            if broker_server:
                previous_server = self._history_broker_identity.get(symbol)
                if previous_server and previous_server != broker_server:
                    self._history_cursor.pop(symbol, None)
                    self._history_seen.pop(symbol, None)
                    self._history_seen_order.pop(symbol, None)
                    self._checkpoint_restore_attempted.discard(symbol)
                    self._history_gap_count[symbol] = self._history_gap_count.get(symbol, 0) + 1
                    logger.error(
                        "[mt5-poller] {} broker server identity changed from {} to {}; "
                        "discarded the prior server's replay cursor and dedup state",
                        symbol, previous_server, broker_server,
                    )
                self._history_broker_identity[symbol] = broker_server
                self._apply_history_checkpoint(symbol, broker_server)
            cursor = self._history_cursor.get(symbol)
            start = (
                cursor - timedelta(milliseconds=1)
                if cursor is not None
                else end - timedelta(seconds=300)
            )
        else:
            start = _range_start.astimezone(timezone.utc)
            end = _range_end.astimezone(timezone.utc)
            latest_raw = getattr(latest_quote, "raw_fields", {})
            latest_raw = latest_raw if isinstance(latest_raw, dict) else {}
            broker_server = str(latest_raw.get("broker_server_identity") or "").strip()
        window_seconds = (end - start).total_seconds()
        if window_seconds < 0:
            self._persist_history_gap(
                symbol, end, start, broker_server, "replay_cursor_ahead_of_latest_quote",
            )
            self._history_cursor.pop(symbol, None)
            self._history_seen.pop(symbol, None)
            self._history_seen_order.pop(symbol, None)
            self._history_gap_count[symbol] = self._history_gap_count.get(symbol, 0) + 1
            logger.error(
                "[mt5-poller] {} replay cursor was ahead of the latest verified quote; "
                "cursor and dedup state cleared after recording the gap",
                symbol,
            )
            return False
        if window_seconds > 300.0:
            # Recover long outages as bounded, independently attested ranges.
            # Each completed batch advances only to observed history, never to
            # the requested end. A failed/empty/unverified batch leaves the
            # remaining interval outstanding for the next poll/restart.
            batch_start = start
            recovered_any = False
            while batch_start < end:
                batch_end = min(batch_start + timedelta(seconds=300), end)
                recovered = self._route_history_quotes(
                    symbol, SimpleNamespace(time=batch_end, raw_fields=latest_raw),
                    _range_start=batch_start, _range_end=batch_end,
                )
                if not recovered:
                    self._history_gap_count[symbol] = self._history_gap_count.get(symbol, 0) + 1
                    self._persist_history_gap(
                        symbol, batch_start, batch_end, broker_server,
                        "batch_fetch_empty_or_timestamp_attestation_failed",
                    )
                    return False
                recovered_any = True
                if batch_end >= end:
                    break
                batch_start = batch_end - timedelta(milliseconds=1)
            return recovered_any
        try:
            frame = fetch(symbol, start, end)
        except Exception as exc:
            self._persist_history_gap(
                symbol, start, end, broker_server, f"history_fetch_failed:{type(exc).__name__}",
            )
            now = _time.monotonic()
            if now - self._last_error_log.get(f"history:{symbol}", 0.0) >= 60.0:
                self._last_error_log[f"history:{symbol}"] = now
                logger.warning("[mt5-poller] {} tick-history fetch failed: {}", symbol, exc)
            return False
        attrs = getattr(frame, "attrs", {})
        if (
            not isinstance(attrs, dict)
            or attrs.get("source_timestamp_verified") is not True
            or attrs.get("source_timezone") != "UTC"
            or attrs.get("utc_normalization") != "utc_request_bounds_verified"
        ):
            self._persist_history_gap(
                symbol, start, end, broker_server,
                "history_timestamp_attestation_missing_or_invalid",
            )
            now = _time.monotonic()
            if now - self._last_error_log.get(f"history-clock:{symbol}", 0.0) >= 60.0:
                self._last_error_log[f"history-clock:{symbol}"] = now
                logger.warning(
                    "[mt5-poller] {} tick-history timestamps are not verified UTC; "
                    "history is not routed and cursor is not advanced",
                    symbol,
                )
            return False
        if bool(getattr(frame, "empty", True)):
            self._persist_history_gap(
                symbol, start, end, broker_server, "history_query_returned_no_rows",
            )
            return False

        newest: Optional[datetime] = None
        def _history_order(item: Any) -> tuple[int, int, int]:
            row_index, row = item
            try:
                raw_time = row.get("time_msc")
                if raw_time is None:
                    raise ValueError("missing time_msc")
                numeric_time = float(raw_time)
                if not math.isfinite(numeric_time) or numeric_time <= 0:
                    raise ValueError("invalid time_msc")
                return 0, int(numeric_time), int(row_index)
            except (TypeError, ValueError, OverflowError):
                # Malformed rows are sorted after timestamped rows and rejected
                # by the row validator below. They must not abort the entire
                # broker-history batch or prevent the latest quote fallback.
                return 1, 0, int(row_index)

        ordered_rows = sorted(frame.iterrows(), key=_history_order)
        for _, row in ordered_rows:
            stamp = row.get("time_msc_datetime")
            if hasattr(stamp, "to_pydatetime"):
                stamp = stamp.to_pydatetime()
            if not isinstance(stamp, datetime) or stamp.tzinfo is None:
                continue
            stamp = stamp.astimezone(timezone.utc)
            if stamp < start - timedelta(milliseconds=1) or stamp > end + timedelta(milliseconds=1):
                continue
            time_seconds = row.get("time_source_epoch", row.get("time_server_epoch"))
            time_milliseconds = row.get("time_msc_source_epoch", row.get("time_msc_server_epoch"))
            if time_seconds is not None and time_milliseconds is not None:
                try:
                    if abs(float(time_milliseconds) / 1000.0 - float(time_seconds)) > 1.5:
                        continue
                # SAFE-FALLBACK: malformed broker timestamp pairs are excluded from verified replay.
                except (TypeError, ValueError, OverflowError):
                    continue
            if newest is None or stamp > newest:
                newest = stamp
            try:
                bid = float(row.get("bid", 0.0) or 0.0)
                ask = float(row.get("ask", 0.0) or 0.0)
                if not (math.isfinite(bid) and math.isfinite(ask) and bid > 0.0 and ask > 0.0):
                    # COPY_TICKS_ALL may include trade-only events. Preserve
                    # cursor progress but never invent a bid/ask quote from Last.
                    continue
                time_msc = row.get("time_msc")
                if time_msc is None:
                    time_msc = int(stamp.timestamp() * 1000)
                key = self._tick_key(time_msc, bid, ask, row.get("flags", 0))
            # SAFE-FALLBACK: malformed quote rows are skipped; no fabricated quote is routed.
            except (TypeError, ValueError, OverflowError):
                continue
            if not self._remember_history_tick(symbol, key):
                continue
            source_fields = (
                row.to_dict() if callable(getattr(row, "to_dict", None)) else dict(row)
            )
            raw_fields = {}
            for field_name, field_value in source_fields.items():
                if isinstance(field_value, datetime):
                    field_value = field_value.isoformat()
                elif callable(getattr(field_value, "item", None)):
                    field_value = field_value.item()
                if isinstance(field_value, float) and not math.isfinite(field_value):
                    field_value = None
                raw_fields[str(field_name)] = field_value
            raw_fields["time_msc_datetime"] = stamp.isoformat()
            self._router.on_tick(Tick(
                symbol=symbol,
                bid=bid,
                ask=ask,
                timestamp=stamp,
                source="mt5",
                source_timestamp_verified=True,
                quote_kind="bid_ask",
                spread_unit="unknown",
                raw_fields=raw_fields,
            ))
        if newest is None:
            self._persist_history_gap(
                symbol, start, end, broker_server, "history_rows_contained_no_valid_timestamps",
            )
            return False
        if newest is not None:
            if broker_server and self._checkpoint_store is not None:
                if not self._persist_history_checkpoint(symbol, newest, broker_server):
                    return False
            self._history_cursor[symbol] = newest
            return True
        return False

    def _poll_symbol(self, symbol: str) -> None:
        quote = self._pm.get_price(symbol)
        if quote is None:
            raise RuntimeError("latest MT5 quote unavailable")
        try:
            latest_bid = float(getattr(quote, "bid", 0.0) or 0.0)
            latest_ask = float(getattr(quote, "ask", 0.0) or 0.0)
        except (TypeError, ValueError, OverflowError) as exc:
            raise RuntimeError("latest MT5 quote contains invalid bid/ask") from exc
        if not (
            math.isfinite(latest_bid) and math.isfinite(latest_ask)
            and latest_bid > 0.0 and latest_ask > 0.0
        ):
            raise RuntimeError("latest MT5 quote contains non-positive or non-finite bid/ask")
        reported_symbol = str(getattr(quote, "symbol", "") or "").strip().upper()
        if not reported_symbol or reported_symbol != symbol.strip().upper():
            raise RuntimeError(
                f"MT5 tick symbol mismatch: requested={symbol}, received={reported_symbol}"
            )
        self._route_history_quotes(symbol, quote)
        # Always retain the current verified quote, including when the history
        # endpoint is temporarily unavailable or returns no rows. Fingerprints
        # suppress the overlap duplicate when history already delivered it.
        self._route_latest_quote(symbol, quote)

    def start(self) -> None:
        if self._running or not self._symbols:
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._poll_loop, daemon=True, name="mt5-tick-poller",
        )
        self._thread.start()
        logger.info("[mt5-poller] started for {} symbols", len(self._symbols))

    def stop(self) -> None:
        self._running = False
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        logger.info("[mt5-poller] stopped")

    def _poll_loop(self) -> None:
        error_counts: dict[str, int] = defaultdict(int)
        last_error_log: dict[str, float] = {}
        remove_after = 500
        removed: set[str] = set()
        # Transient failures (terminal hiccup, stale tick during a broker
        # reconnect) must NOT permanently retire a symbol — that silently
        # un-polls the FX book until a restart. Instead, after `remove_after`
        # consecutive errors a symbol enters a bounded cooldown and is retried
        # once it elapses. Only symbols the broker confirms are *not available*
        # are removed permanently (they will never recover this process).
        cooldown_until: dict[str, float] = {}
        COOLDOWN_SECONDS = 60.0

        while self._running:
            for sym in list(self._symbols):
                if not self._running:
                    break
                if sym in removed:
                    continue
                # Skip symbols whose market is closed (weekend FX/commodity/
                # index).  No broker call, no stale-tick warning, no failure
                # count — polling resumes automatically when the market reopens.
                if not self._should_poll(sym):
                    continue
                # Honour an active cooldown: a symbol that recently tripped the
                # failure threshold is paused (not removed) and retried once the
                # backoff window elapses.
                cd = cooldown_until.get(sym, 0.0)
                if cd:
                    if _time.monotonic() < cd:
                        continue
                    # Cooldown elapsed — give the symbol a fresh chance.
                    cooldown_until.pop(sym, None)
                    error_counts[sym] = 0
                try:
                    self._poll_symbol(sym)
                    error_counts[sym] = 0
                except Exception as exc:
                    error_counts[sym] += 1
                    now = _time.monotonic()
                    if now - last_error_log.get(sym, 0) >= 60.0:
                        last_error_log[sym] = now
                        logger.warning(
                            "[mt5-poller] {} tick error (count={}): {}",
                            sym, error_counts[sym], exc,
                        )
                    # A symbol confirmed unavailable on the broker will never
                    # recover — drop it immediately instead of spinning to the
                    # 500-failure threshold.
                    if "not available on broker" in str(exc):
                        logger.warning(
                            "[mt5-poller] {} removed from poll — not available on broker",
                            sym,
                        )
                        self._symbols.remove(sym)
                        removed.add(sym)
                        continue

                if error_counts.get(sym, 0) >= remove_after:
                    # Bounded backoff instead of permanent removal: pause the
                    # symbol for COOLDOWN_SECONDS, then retry. Keeps the FX book
                    # polled across transient broker disconnects/reconnects.
                    cooldown_until[sym] = _time.monotonic() + COOLDOWN_SECONDS
                    logger.warning(
                        "[mt5-poller] {} paused for {:.0f}s — {} consecutive "
                        "failures (will retry after cooldown)",
                        sym, COOLDOWN_SECONDS, remove_after,
                    )
            _time.sleep(self._interval)


class DerivTickAdapter:
    """Hooks into Deriv WebSocket tick stream and injects into TickRouter."""

    def __init__(
        self,
        platform_manager: PlatformManager,
        tick_router: TickRouter,
        symbols: list[str],
        poll_interval: float = 0.1,
        should_poll: Optional[Callable[[str], bool]] = None,
    ) -> None:
        self._pm = platform_manager
        self._router = tick_router
        self._symbols = list(symbols)
        self._interval = poll_interval
        # Gate that returns False when a symbol's market is currently closed.
        # Deriv serves both 24/7 synthetics (always polled) and session-gated
        # FX/commodity mirrors (skipped on the weekend) — the gate keeps the
        # weekend log quiet without dropping the 24/7 feed.
        self._should_poll = should_poll or (lambda _sym: True)
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._error_counts: dict[str, int] = {}
        self._last_error_log: dict[str, float] = {}

    def start(self) -> None:
        if self._running or not self._symbols:
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._poll_loop, daemon=True, name="deriv-tick-adapter",
        )
        self._thread.start()
        if not bool(getattr(self._pm, "deriv_connected", False)):
            logger.info(
                "[deriv-adapter] waiting for Deriv connection — no alternate-venue fallback"
            )
        logger.info("[deriv-adapter] started for {} symbols", len(self._symbols))

    def stop(self) -> None:
        self._running = False
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        logger.info("[deriv-adapter] stopped")

    def _poll_loop(self) -> None:
        error_counts: dict[str, int] = defaultdict(int)
        last_error_log: dict[str, float] = {}
        # Connection-level errors (disconnect / reconnecting) hit every symbol
        # at once and would otherwise flood the log with one line per symbol
        # per cycle. Collapse them into a single throttled summary covering all
        # affected symbols, and emit one line when the connection recovers.
        conn_down = False
        conn_first_log = 0.0
        conn_last_summary = 0.0
        conn_err_count = 0
        conn_symbols: set[str] = set()
        CONN_SUMMARY_INTERVAL = 60.0

        def _is_conn_error(msg: str) -> bool:
            m = msg.lower()
            return (
                "not connected" in m
                or "reconnect" in m
                or "request blocked" in m
            )

        while self._running:
            # Platform.BOTH symbols are intentionally polled by both source
            # adapters. PlatformManager.get_price() prefers MT5 for BOTH, so
            # Deriv must use its explicit connector and must never inherit MT5
            # quotes. Stay idle while Deriv is offline; reconnects resume polling.
            if not bool(getattr(self._pm, "deriv_connected", False)):
                _time.sleep(max(self._interval, 0.1))
                continue

            # Per-cycle connection stats. The connection state is evaluated once
            # at the end of the full symbol sweep — never mid-cycle — so a flaky
            # link that mixes good and bad ticks within a single pass cannot flap
            # the disconnect/recover log lines.
            cycle_good_ticks = 0
            cycle_conn_errors = 0
            cycle_conn_symbols: set[str] = set()

            for sym in self._symbols:
                if not self._running:
                    break
                # Skip closed-market symbols (weekend FX/commodity mirrors).
                # 24/7 synthetics always pass the gate and keep streaming.
                if not self._should_poll(sym):
                    continue
                try:
                    get_deriv_price = getattr(self._pm, "get_deriv_price", None)
                    if not callable(get_deriv_price):
                        raise RuntimeError("PlatformManager has no explicit Deriv price route")
                    td = get_deriv_price(sym)
                    if td is not None and td.bid > 0:
                        reported_symbol = str(getattr(td, "symbol", "") or "").strip().upper()
                        if not reported_symbol or reported_symbol != sym.strip().upper():
                            raise RuntimeError(
                                f"Deriv tick symbol mismatch: requested={sym}, received={reported_symbol}"
                            )
                        tick = Tick(
                            symbol=sym,
                            bid=td.bid,
                            ask=td.ask,
                            timestamp=td.time,
                            source="deriv",
                            source_timestamp_verified=(
                                getattr(td, "source_timestamp_verified", False) is True
                            ),
                            quote_kind=getattr(td, "quote_kind", "unknown"),
                            spread_unit=getattr(td, "spread_unit", "unknown"),
                            raw_fields=getattr(td, "raw_fields", {}),
                        )
                        # Route immediately — ticks must keep flowing regardless
                        # of connection-state bookkeeping.
                        self._router.on_tick(tick)
                        error_counts[sym] = 0
                        cycle_good_ticks += 1
                    else:
                        error_counts[sym] += 1
                except Exception as exc:
                    error_counts[sym] += 1
                    now = _time.monotonic()
                    if _is_conn_error(str(exc)):
                        # Tally connection-down errors for end-of-cycle evaluation;
                        # do not flip state or log here.
                        cycle_conn_symbols.add(sym)
                        cycle_conn_errors += 1
                        continue
                    # Genuine per-symbol error — keep the existing 60s throttle.
                    if now - last_error_log.get(sym, 0) >= 60.0:
                        last_error_log[sym] = now
                        logger.warning(
                            "[deriv-adapter] {} tick error (count={}): {}",
                            sym, error_counts[sym], exc,
                        )

            # ── Evaluate connection state once per full cycle (with hysteresis) ──
            now = _time.monotonic()
            if cycle_conn_errors > 0 and cycle_good_ticks == 0:
                # Entire cycle failed — connection is down.
                conn_symbols.update(cycle_conn_symbols)
                conn_err_count += cycle_conn_errors
                if not conn_down:
                    conn_down = True
                    conn_first_log = now
                    conn_last_summary = now
                    logger.warning(
                        "[deriv-adapter] Deriv disconnected — suppressing "
                        "per-symbol tick errors; a summary will follow "
                        "every {:.0f}s until reconnect", CONN_SUMMARY_INTERVAL,
                    )
                elif now - conn_last_summary >= CONN_SUMMARY_INTERVAL:
                    conn_last_summary = now
                    logger.warning(
                        "[deriv-adapter] still disconnected for {:.0f}s — "
                        "{} tick error(s) across {} symbol(s) suppressed",
                        now - conn_first_log, conn_err_count, len(conn_symbols),
                    )
            elif conn_down and cycle_good_ticks > 0 and cycle_conn_errors == 0:
                # A full, clean cycle after an outage — genuinely recovered.
                logger.info(
                    "[deriv-adapter] connection recovered — resuming tick "
                    "polling ({} suppressed error(s) across {} symbol(s) "
                    "during {:.0f}s outage)",
                    conn_err_count, len(conn_symbols), now - conn_first_log,
                )
                conn_down = False
                conn_err_count = 0
                conn_symbols = set()
            # Mixed cycle (some good, some conn errors): hold current state — no flap.

            _time.sleep(self._interval)
