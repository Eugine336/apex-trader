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

import math
import threading
import time as _time
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
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
        time_msc = raw.get("time_msc")
        if time_msc is None:
            try:
                time_msc = int(timestamp.timestamp() * 1000)
            except Exception:
                return False
        key = self._tick_key(
            time_msc, getattr(quote, "bid", None), getattr(quote, "ask", None),
            raw.get("flags", 0),
        )
        if not self._remember_history_tick(symbol, key):
            return False
        tick = Tick(
            symbol=symbol,
            bid=float(quote.bid),
            ask=float(quote.ask),
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

    def _route_history_quotes(self, symbol: str, latest_quote: Any) -> bool:
        """Replay all verified bid/ask ticks since the last broker-time cursor.

        MT5's latest-quote polling can skip intermediate updates. The history
        API returns the broker's available quote tape, so the poller requests an
        overlapping range, validates the adapter's UTC attestation, deduplicates
        boundary repeats and routes every usable bid/ask event. Trade-only rows
        are not converted into synthetic bid/ask quotes.
        """
        fetch = getattr(self._pm, "get_tick_history", None)
        if not callable(fetch):
            return False
        end = getattr(latest_quote, "time", None)
        if not isinstance(end, datetime) or end.tzinfo is None:
            return False
        end = end.astimezone(timezone.utc)
        cursor = self._history_cursor.get(symbol)
        start = (
            cursor - timedelta(milliseconds=1)
            if cursor is not None
            else end - timedelta(seconds=2)
        )
        window_seconds = (end - start).total_seconds()
        if window_seconds < 0:
            return False
        if window_seconds > 300.0:
            # get_ticks only attests UTC normalization for a recent live window.
            # Do not relabel a long outage/weekend replay with today's offset:
            # record the gap, re-anchor at the current verified quote, and resume
            # live capture without fabricating timestamps for the missing span.
            self._history_gap_count[symbol] = self._history_gap_count.get(symbol, 0) + 1
            self._history_cursor[symbol] = end
            now = _time.monotonic()
            if now - self._last_error_log.get(f"history-gap:{symbol}", 0.0) >= 60.0:
                self._last_error_log[f"history-gap:{symbol}"] = now
                logger.error(
                    "[mt5-poller] {} tick-history gap {:.1f}s — cannot verify historical "
                    "UTC offset; re-anchoring at current broker quote (gap_count={})",
                    symbol, window_seconds, self._history_gap_count[symbol],
                )
            return False
        try:
            frame = fetch(symbol, start, end)
        except Exception as exc:
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
            or attrs.get("utc_normalization") != "live_server_offset"
        ):
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
            return False

        newest: Optional[datetime] = None
        ordered_rows = sorted(
            frame.iterrows(),
            key=lambda item: int(item[1].get("time_msc", 0) or 0),
        )
        for _, row in ordered_rows:
            stamp = row.get("time_msc_datetime")
            if hasattr(stamp, "to_pydatetime"):
                stamp = stamp.to_pydatetime()
            if not isinstance(stamp, datetime) or stamp.tzinfo is None:
                continue
            stamp = stamp.astimezone(timezone.utc)
            if stamp < start - timedelta(milliseconds=1) or stamp > end + timedelta(milliseconds=1):
                continue
            time_seconds = row.get("time_server_epoch")
            time_milliseconds = row.get("time_msc_server_epoch")
            if time_seconds is not None and time_milliseconds is not None:
                try:
                    if abs(float(time_milliseconds) / 1000.0 - float(time_seconds)) > 1.5:
                        continue
                except (TypeError, ValueError, OverflowError):
                    # SAFE-FALLBACK: malformed broker timestamp pairs are excluded from verified replay.
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
            except (TypeError, ValueError, OverflowError):
                # SAFE-FALLBACK: malformed quote rows are skipped; no fabricated quote is routed.
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
        if newest is not None:
            self._history_cursor[symbol] = newest
            return True
        return False

    def _poll_symbol(self, symbol: str) -> None:
        quote = self._pm.get_price(symbol)
        if quote is None or float(getattr(quote, "bid", 0.0) or 0.0) <= 0.0:
            raise RuntimeError("latest MT5 quote unavailable")
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
