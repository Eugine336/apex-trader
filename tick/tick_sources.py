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

import threading
import time as _time
from collections import defaultdict
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Callable, Optional

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
                    td = self._pm.get_price(sym)
                    if td is not None and td.bid > 0:
                        tick = Tick(
                            symbol=sym,
                            bid=td.bid,
                            ask=td.ask,
                            timestamp=datetime.now(timezone.utc),
                            source="mt5",
                        )
                        self._router.on_tick(tick)
                        error_counts[sym] = 0
                    else:
                        error_counts[sym] += 1
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
                    td = self._pm.get_price(sym)
                    if td is not None and td.bid > 0:
                        tick = Tick(
                            symbol=sym,
                            bid=td.bid,
                            ask=td.ask,
                            timestamp=datetime.now(timezone.utc),
                            source="deriv",
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
