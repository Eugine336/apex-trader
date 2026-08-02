"""APEX TRADER — News Calendar Pre-planner (Phase 3 Feature A).

The existing feedparser news module (:class:`brain.session_engine.NewsGuard`)
*blocks* entries around scheduled high-impact events (CPI, NFP, FOMC, …). A
professional desk does the opposite: it knows exactly what is scheduled and
*pre-plans both directions*, resting a breakout order above and below the
pre-event range so the fill happens the instant the release moves price —
before a human (or an M1-close market order) could ever react.

This module implements that pre-planning. For each configured symbol, on a
periodic tick (the event-driven watchdog calls :meth:`update` every ~60s) it:

    1. Detects upcoming scheduled HIGH-impact events within
       ``news_pre_stage_minutes`` of now (via the injected ``get_events``).
    2. Computes the current price range from the last
       ``news_range_lookback_bars`` candles (via the injected ``get_candles``).
    3. Stages TWO pending stop orders straddling the range — a BUY_STOP at
       ``range_high + news_breakout_buffer_pips`` and a SELL_STOP at
       ``range_low - news_breakout_buffer_pips`` — auto-sized DOWN by
       ``news_risk_multiplier`` (news spreads widen, so half size).
    4. Runs OCO-style: as soon as one leg fills, the opposite pending order is
       cancelled immediately. A tiny per-event state machine tracks this
       (``PENDING → ONE_FILLED → DONE``).
    5. After the event passes by ``news_post_event_cooldown_s`` seconds, any
       still-resting (unfilled) pending order is cancelled.

The planner REPLACES the old news block: when ``news_pre_planning_enabled`` is
True for a symbol the event-driven news gate stops blocking and lets the staged
breakout orders do the work. When False, the old blocking behaviour is
preserved unchanged and this planner stages nothing.

Like the :class:`~entry.zone_order_staging.ZoneOrderStager`, every external
dependency is an injected callable, so the planner is unit-testable in isolation
and the event-driven bootstrap wires it to the real broker connector, the news
feed and the position sizer. Tuning resolves per-symbol: the
``InstrumentProfile`` value first, then the ``EntryConfig`` default, then the
literal — exactly the resolution the compression detector / zone stager use.

Thread-safety: :meth:`update` runs on the watchdog thread and :meth:`cancel_all`
on the shutdown path, so all mutable state is guarded by an ``RLock``.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional

from loguru import logger

from brain.instrument_profile import get_profile
from entry.models import EntryConfig

# ── Per-event OCO state machine ──────────────────────────────────────────────
STATE_PENDING = "PENDING"      # both breakout orders resting, neither filled
STATE_ONE_FILLED = "ONE_FILLED"  # one leg filled, the opposite was cancelled
STATE_DONE = "DONE"            # event window closed / cleaned up

# ── Per-order lifecycle ──────────────────────────────────────────────────────
ORDER_PENDING = "PENDING"
ORDER_FILLED = "FILLED"
ORDER_CANCELLED = "CANCELLED"


@dataclass
class NewsPendingOrder:
    """One resting breakout stop order staged for a news event."""

    order_id: str
    direction: str          # "LONG" (BUY_STOP) / "SHORT" (SELL_STOP)
    order_kind: str         # "BUY_STOP" / "SELL_STOP"
    price: float
    stop_loss: float
    take_profit: float
    lots: float
    status: str = ORDER_PENDING


@dataclass
class NewsEventPlan:
    """OCO breakout pair staged around one scheduled high-impact event."""

    key: str
    symbol: str
    title: str
    event_time: datetime
    orders: list[NewsPendingOrder] = field(default_factory=list)
    state: str = STATE_PENDING
    staged_at: float = 0.0

    def resting_orders(self) -> list[NewsPendingOrder]:
        return [o for o in self.orders if o.status == ORDER_PENDING]


def _get_attr(row: Any, key: str) -> Optional[float]:
    """Read ``key`` from a mapping-or-object candle row (duck-typed)."""
    if isinstance(row, dict):
        val = row.get(key)
    else:
        val = getattr(row, key, None)
    if isinstance(val, (int, float)):
        return float(val)
    return None


def event_key(symbol: str, event: Any) -> str:
    """Stable identity for a scheduled event (symbol + title + currency + time).

    Two feed reads of the same release map to the same plan so a periodic
    ``update`` never double-stages the same event.
    """
    t = getattr(event, "time_utc", None)
    t_key = t.isoformat() if isinstance(t, datetime) else str(t)
    return "|".join(
        (
            str(symbol),
            str(getattr(event, "title", "")),
            str(getattr(event, "currency", "")),
            t_key,
        )
    )


class NewsPlanner:
    """Pre-plans OCO breakout orders around scheduled high-impact events."""

    def __init__(
        self,
        *,
        config: Optional[EntryConfig] = None,
        get_symbols: Callable[[], list[str]],
        get_events: Callable[[str, float, datetime], list[Any]],
        get_candles: Callable[[str, int], Any],
        place_pending_order: Callable[..., Any],
        cancel_pending_order: Callable[[str, str], bool],
        is_filled: Callable[[str, str], bool],
        pip_size_lookup: Callable[[str], float],
        size_lookup: Callable[[str, str, float, float, float], float],
        profile_lookup: Optional[Callable[[str], Any]] = get_profile,
    ) -> None:
        """Wire the planner to its (all-injected) dependencies.

        ``get_symbols() -> list[str]`` is the active symbol universe.
        ``get_events(symbol, within_minutes, now) -> list`` returns upcoming
        HIGH-impact events (each with ``time_utc`` / ``title`` / ``currency``)
        within the window. ``get_candles(symbol, bars)`` returns the last
        ``bars`` candles (a pandas DataFrame or a sequence of high/low rows) for
        the pre-event range. ``place_pending_order`` mirrors the MT5Connector
        signature ``(symbol, order_kind, entry_price, lots, sl, tp, comment,
        idempotency_key)`` and returns an object with ``.success`` / ``.order_id``.
        ``cancel_pending_order(symbol, order_id) -> bool`` cancels a resting
        order. ``is_filled(symbol, order_id) -> bool`` reports whether a staged
        order has filled into a position. ``size_lookup(symbol, direction,
        entry_price, sl, risk_multiplier) -> lots`` sizes each leg with the
        news risk multiplier already applied.
        """
        self._config = config or EntryConfig()
        self._get_symbols = get_symbols
        self._get_events = get_events
        self._get_candles = get_candles
        self._place_pending = place_pending_order
        self._cancel_pending = cancel_pending_order
        self._is_filled = is_filled
        self._pip_size = pip_size_lookup
        self._size_lookup = size_lookup
        self._profile_lookup = profile_lookup

        self._plans: dict[str, NewsEventPlan] = {}
        self._lock = threading.RLock()

        self._stats = {
            "events_staged": 0,
            "orders_placed": 0,
            "oco_cancels": 0,
            "cooldown_cancels": 0,
            "stage_failures": 0,
            "filled": 0,
        }

    @property
    def stats(self) -> dict[str, int]:
        with self._lock:
            return dict(self._stats)

    @property
    def active_plans(self) -> dict[str, NewsEventPlan]:
        """Snapshot of the tracked event plans (keyed by event identity)."""
        with self._lock:
            return dict(self._plans)

    # ── Tuning resolution (profile → EntryConfig → literal) ───────────
    def _param(self, symbol: str, name: str, default: Any) -> Any:
        if self._profile_lookup is not None:
            try:
                prof = self._profile_lookup(symbol)
            except Exception:  # noqa: BLE001 — tuning lookup never breaks planning
                prof = None
            if prof is not None:
                val = getattr(prof, name, None)
                if val is not None:
                    return val
        val = getattr(self._config, name, None)
        return default if val is None else val

    def _enabled(self, symbol: str) -> bool:
        return bool(self._param(symbol, "news_pre_planning_enabled", False))

    # ── Public entry points ───────────────────────────────────────────
    def update(self, now: Optional[datetime] = None) -> None:
        """Stage / monitor / clean up news breakout orders. Never raises.

        Called periodically (every ~60s) by the event-driven watchdog. First
        stages any newly-upcoming events for each enabled symbol, then advances
        every tracked plan's OCO + cooldown state machine.
        """
        if now is None:
            now = datetime.now(timezone.utc)
        elif now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)

        try:
            symbols = list(self._get_symbols() or [])
        except Exception:  # noqa: BLE001 — a symbol-list fault never breaks planning
            logger.debug("[news-planner] get_symbols failed")
            symbols = []

        for symbol in symbols:
            try:
                self._plan_symbol(symbol, now)
            except Exception:  # noqa: BLE001 — one symbol never breaks the rest
                logger.debug("[news-planner] plan failed for {}", symbol)

        # Monitor + clean up ALL tracked plans (independent of the events fetch,
        # so a plan whose event has already passed is still reaped).
        with self._lock:
            keys = list(self._plans.keys())
        for key in keys:
            try:
                self._monitor_plan(key, now)
            except Exception:  # noqa: BLE001
                logger.debug("[news-planner] monitor failed for {}", key)

    def cancel_all(self, symbol: Optional[str] = None) -> None:
        """Cancel every resting staged order (optionally only for one symbol)."""
        with self._lock:
            keys = [
                k for k, p in self._plans.items()
                if symbol is None or p.symbol == symbol
            ]
        for key in keys:
            self._cancel_resting(key, "cancel_all")
            with self._lock:
                plan = self._plans.get(key)
                if plan is not None:
                    plan.state = STATE_DONE

    # ── Staging ────────────────────────────────────────────────────────
    def _plan_symbol(self, symbol: str, now: datetime) -> None:
        if not self._enabled(symbol):
            return
        pre_stage_minutes = float(self._param(symbol, "news_pre_stage_minutes", 5))
        try:
            events = list(self._get_events(symbol, pre_stage_minutes, now) or [])
        except Exception:  # noqa: BLE001 — a feed fault stages nothing this cycle
            logger.debug("[news-planner] get_events failed for {}", symbol)
            return
        for event in events:
            key = event_key(symbol, event)
            with self._lock:
                if key in self._plans:
                    continue  # already staged / handled — idempotent
            self._stage_event(symbol, key, event, now)

    def _stage_event(
        self, symbol: str, key: str, event: Any, now: datetime,
    ) -> None:
        rng = self._compute_range(symbol)
        if rng is None:
            logger.debug("[news-planner] {} no range — skip staging {}", symbol, key)
            return
        low, high = rng
        pip = float(self._pip_size(symbol) or 0.0)
        if pip <= 0 or high <= low:
            return

        buffer = float(self._param(symbol, "news_breakout_buffer_pips", 5.0)) * pip
        risk_mult = float(self._param(symbol, "news_risk_multiplier", 0.5))

        buy_price = high + buffer
        buy_sl = low - buffer
        sell_price = low - buffer
        sell_sl = high + buffer

        title = str(getattr(event, "title", "") or "")
        event_time = getattr(event, "time_utc", now)
        if not isinstance(event_time, datetime):
            event_time = now
        elif event_time.tzinfo is None:
            event_time = event_time.replace(tzinfo=timezone.utc)

        legs = (
            ("LONG", "BUY_STOP", buy_price, buy_sl),
            ("SHORT", "SELL_STOP", sell_price, sell_sl),
        )
        orders: list[NewsPendingOrder] = []
        for direction, order_kind, price, sl in legs:
            order = self._place_leg(
                symbol, key, direction, order_kind, price, sl, risk_mult,
            )
            if order is not None:
                orders.append(order)

        if not orders:
            with self._lock:
                self._stats["stage_failures"] += 1
            return

        plan = NewsEventPlan(
            key=key,
            symbol=symbol,
            title=title,
            event_time=event_time,
            orders=orders,
            state=STATE_PENDING,
            staged_at=time.monotonic(),
        )
        with self._lock:
            self._plans[key] = plan
            self._stats["events_staged"] += 1
            self._stats["orders_placed"] += len(orders)
        logger.info(
            "[news-planner] {} staged breakout OCO for '{}' @ {} — "
            "BUY_STOP {:.5f} / SELL_STOP {:.5f} (range {:.5f}-{:.5f}, ×{:.2f} risk)",
            symbol, title, event_time.isoformat(),
            buy_price, sell_price, low, high, risk_mult,
        )

    def _place_leg(
        self,
        symbol: str,
        key: str,
        direction: str,
        order_kind: str,
        price: float,
        sl: float,
        risk_mult: float,
    ) -> Optional[NewsPendingOrder]:
        try:
            lots = float(
                self._size_lookup(symbol, direction, price, sl, risk_mult) or 0.0
            )
        except Exception:  # noqa: BLE001
            logger.debug("[news-planner] size_lookup raised for {} {}", symbol, direction)
            lots = 0.0
        if lots <= 0:
            return None

        tp = self._target(direction, price, sl)
        idem = f"news:{key}:{order_kind}"
        try:
            result = self._place_pending(
                symbol, order_kind, price, lots, sl, tp, "APEX_NEWS", idem,
            )
        except Exception:  # noqa: BLE001
            logger.debug("[news-planner] place_pending raised for {} {}", symbol, order_kind)
            return None

        if result is None or not bool(getattr(result, "success", False)):
            err = getattr(result, "error", "") if result is not None else "no result"
            logger.warning(
                "[news-planner] {} {} stage FAILED @ {:.5f}: {}",
                symbol, order_kind, price, err,
            )
            return None

        return NewsPendingOrder(
            order_id=str(getattr(result, "order_id", "") or ""),
            direction=direction,
            order_kind=order_kind,
            price=price,
            stop_loss=sl,
            take_profit=tp,
            lots=lots,
        )

    @staticmethod
    def _target(direction: str, entry_price: float, sl: float) -> float:
        """A 2R breakout target (risk = |entry - sl|)."""
        risk = abs(entry_price - sl)
        if risk <= 0:
            risk = entry_price * 0.001 if entry_price > 0 else 1.0
        return entry_price + 2.0 * risk if direction == "LONG" else entry_price - 2.0 * risk

    def _compute_range(self, symbol: str) -> Optional[tuple[float, float]]:
        """Return ``(low, high)`` from the last ``news_range_lookback_bars`` candles."""
        lookback = int(self._param(symbol, "news_range_lookback_bars", 10))
        if lookback <= 0:
            return None
        try:
            candles = self._get_candles(symbol, lookback)
        except Exception:  # noqa: BLE001
            logger.debug("[news-planner] get_candles raised for {}", symbol)
            return None
        return self._extract_low_high(candles, lookback)

    @staticmethod
    def _extract_low_high(candles: Any, lookback: int) -> Optional[tuple[float, float]]:
        if candles is None:
            return None
        # pandas DataFrame (has a ``columns`` attribute and column indexing).
        if getattr(candles, "columns", None) is not None:
            try:
                if len(candles) == 0:
                    return None
                tail = candles.tail(lookback)
                low = float(tail["low"].min())
                high = float(tail["high"].max())
                if high <= low:
                    return None
                return low, high
            except Exception:  # noqa: BLE001
                return None
        # Sequence of dict / object rows with ``high`` / ``low``.
        try:
            seq = list(candles)
        except TypeError:
            return None
        seq = seq[-lookback:]
        highs: list[float] = []
        lows: list[float] = []
        for row in seq:
            h = _get_attr(row, "high")
            low_v = _get_attr(row, "low")
            if h is None or low_v is None:
                continue
            highs.append(h)
            lows.append(low_v)
        if not highs or not lows:
            return None
        low = min(lows)
        high = max(highs)
        if high <= low:
            return None
        return low, high

    # ── Monitoring: OCO + cooldown ─────────────────────────────────────
    def _monitor_plan(self, key: str, now: datetime) -> None:
        with self._lock:
            plan = self._plans.get(key)
        if plan is None or plan.state == STATE_DONE:
            return

        # A symbol toggled OFF mid-session — cancel any resting orders and close.
        if not self._enabled(plan.symbol):
            self._cancel_resting(key, "pre-planning disabled")
            with self._lock:
                plan.state = STATE_DONE
            return

        # ── OCO fill check ────────────────────────────────────────────
        filled_leg: Optional[NewsPendingOrder] = None
        for order in plan.orders:
            if order.status != ORDER_PENDING:
                continue
            try:
                if bool(self._is_filled(plan.symbol, order.order_id)):
                    order.status = ORDER_FILLED
                    filled_leg = order
                    with self._lock:
                        self._stats["filled"] += 1
                    logger.info(
                        "[news-planner] {} {} breakout FILLED @ {:.5f} ({})",
                        plan.symbol, order.direction, order.price, order.order_id,
                    )
            except Exception:  # noqa: BLE001 — a fill read never breaks monitoring
                logger.debug(
                    "[news-planner] is_filled raised for {} {}",
                    plan.symbol, order.order_id,
                )

        if filled_leg is not None and plan.state == STATE_PENDING:
            # OCO: cancel every OTHER still-resting leg immediately.
            for order in plan.orders:
                if order.status == ORDER_PENDING:
                    self._cancel_order(plan.symbol, order, "OCO — opposite filled")
                    with self._lock:
                        self._stats["oco_cancels"] += 1
            with self._lock:
                plan.state = STATE_ONE_FILLED

        # ── Post-event cooldown: reap any still-resting orders ─────────
        cooldown = float(self._param(plan.symbol, "news_post_event_cooldown_s", 300.0))
        if now >= plan.event_time + timedelta(seconds=cooldown):
            for order in plan.orders:
                if order.status == ORDER_PENDING:
                    self._cancel_order(plan.symbol, order, "post-event cooldown")
                    with self._lock:
                        self._stats["cooldown_cancels"] += 1
            with self._lock:
                plan.state = STATE_DONE
            self._purge_if_stale(key, now, cooldown)

    def _cancel_resting(self, key: str, reason: str) -> None:
        with self._lock:
            plan = self._plans.get(key)
        if plan is None:
            return
        for order in plan.orders:
            if order.status == ORDER_PENDING:
                self._cancel_order(plan.symbol, order, reason)

    def _cancel_order(
        self, symbol: str, order: NewsPendingOrder, reason: str,
    ) -> None:
        ok = False
        try:
            ok = bool(self._cancel_pending(symbol, order.order_id))
        except Exception:  # noqa: BLE001 — a failed cancel is logged, not raised
            logger.debug("[news-planner] cancel raised for {}", order.order_id)
            ok = False
        order.status = ORDER_CANCELLED
        logger.info(
            "[news-planner] {} {} pending cancel {} ({}) — {}",
            symbol, order.direction, "ok" if ok else "FAILED", order.order_id, reason,
        )

    def _purge_if_stale(
        self, key: str, now: datetime, cooldown: float,
    ) -> None:
        """Drop a DONE plan once well past its event so memory stays bounded."""
        with self._lock:
            plan = self._plans.get(key)
            if plan is None:
                return
            grace = timedelta(seconds=cooldown + 3600.0)
            if now >= plan.event_time + grace:
                self._plans.pop(key, None)
