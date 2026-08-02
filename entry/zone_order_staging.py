"""APEX TRADER — Zone Order Stager (Phase 2 Feature B).

Pre-stages pending LIMIT orders at a zone boundary as price *approaches* a zone,
instead of waiting for a zone touch → M1 close → market order. When price is
within a configurable ``staging_proximity_pips`` of an active zone's near edge,
the stager runs the FULL gate pipeline (EV gate, compliance, portfolio risk —
injected as ``gate_check``) and, if it passes, places a BUY_LIMIT (LONG zone) or
SELL_LIMIT (SHORT zone) at the boundary with a pre-computed SL (the zone
invalidation level) and TP (from the injected ``derive_targets``).

This is NOT a gate bypass — it is an *earlier* execution of the same gates so
the order rests at the boundary and fills the instant price arrives.

Lifecycle:
    * A staged order is tracked per zone identity.
    * A staleness watcher cancels the pending order when its zone leaves the
      active set (expired or invalidated by a structure change).
    * A configurable ``staging_cooldown_s`` throttles re-staging so a zone that
      keeps appearing/disappearing cannot flap stage/cancel rapidly.

The stager is deliberately decoupled from the broker and the gate stack: every
external dependency is an injected callable, so it is unit-testable in isolation
and the event-driven bootstrap wires it to the real MT5 connector / orchestrator
gate pipeline / position sizer.

Thread-safety: ``on_tick`` runs on the tick thread and ``on_zone_update`` on the
WorldModel-publish path, so all mutable state is guarded by a lock.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional

from loguru import logger

from brain.instrument_profile import get_profile
from entry.models import EntryConfig, EntryZone


@dataclass
class StagedOrder:
    """A resting pending LIMIT order the stager placed for a zone."""

    order_id: str
    symbol: str
    direction: str
    zone_key: str
    boundary: float
    stop_loss: float
    take_profit: float
    lots: float
    staged_at: float


def zone_key(zone: EntryZone) -> str:
    """Stable identity for a zone (price level + direction + type + source TF).

    Mirrors ``entry.zone_watcher._zone_identity`` so a zone recognised across
    successive WorldModel publishes maps to the same staged order.
    """
    return "|".join(
        (
            str(getattr(zone, "symbol", "")),
            str(getattr(zone, "direction", "")),
            str(getattr(getattr(zone, "zone_type", None), "value", "")),
            f"{float(getattr(zone, 'top', 0.0)):.5f}",
            f"{float(getattr(zone, 'bottom', 0.0)):.5f}",
            str(getattr(zone, "timeframe", "")),
        )
    )


class ZoneOrderStager:
    """Stages pending LIMIT orders at zone boundaries as price approaches."""

    def __init__(
        self,
        *,
        config: Optional[EntryConfig] = None,
        get_active_zones: Callable[[str], list[EntryZone]],
        place_pending_order: Callable[..., Any],
        cancel_pending_order: Callable[[str, str], bool],
        pip_size_lookup: Callable[[str], float],
        size_lookup: Callable[[str, str, float, float], float],
        derive_targets: Optional[Callable[[str, str, float, float], tuple[float, float]]] = None,
        gate_check: Optional[Callable[..., bool]] = None,
        profile_lookup: Optional[Callable[[str], Any]] = get_profile,
    ) -> None:
        """Wire the stager to its (all-injected) dependencies.

        ``place_pending_order`` must accept the MT5Connector signature
        ``(symbol, order_kind, entry_price, lots, sl, tp, comment,
        idempotency_key)`` and return an object with ``.success`` / ``.order_id``.
        ``cancel_pending_order(symbol, order_id) -> bool`` cancels a resting
        order.
        ``size_lookup(symbol, direction, entry_price, sl) -> lots`` sizes the
        pending order. ``gate_check(symbol, direction, zone, entry_price, sl,
        tp1, tp2) -> bool`` runs the full gate pipeline (None ⇒ always allow, for
        tests). ``derive_targets`` computes ``(tp1, tp2)`` (a risk-multiple
        fallback is used when omitted).
        """
        self._config = config or EntryConfig()
        self._get_active_zones = get_active_zones
        self._place_pending = place_pending_order
        self._cancel_pending = cancel_pending_order
        self._pip_size = pip_size_lookup
        self._size_lookup = size_lookup
        self._derive_targets = derive_targets
        self._gate_check = gate_check
        self._profile_lookup = profile_lookup

        # zone_key -> StagedOrder for every resting order we placed.
        self._staged: dict[str, StagedOrder] = {}
        # zone_key -> monotonic time of the last stage OR cancel, for cooldown.
        self._last_stage_time: dict[str, float] = {}
        self._lock = threading.RLock()

        self._stats = {
            "staged": 0,
            "cancelled": 0,
            "gate_rejects": 0,
            "cooldown_skips": 0,
            "stage_failures": 0,
        }

    @property
    def stats(self) -> dict[str, int]:
        with self._lock:
            return dict(self._stats)

    @property
    def staged_orders(self) -> dict[str, StagedOrder]:
        """Snapshot of currently resting staged orders (keyed by zone identity)."""
        with self._lock:
            return dict(self._staged)

    # ── Tuning resolution (profile → EntryConfig → literal) ───────────
    def _param(self, symbol: str, name: str, default: Any) -> Any:
        if self._profile_lookup is not None:
            try:
                prof = self._profile_lookup(symbol)
            except Exception:  # noqa: BLE001 — tuning lookup never breaks staging
                prof = None
            if prof is not None:
                val = getattr(prof, name, None)
                if val is not None:
                    return val
        val = getattr(self._config, name, None)
        return default if val is None else val

    def _enabled(self, symbol: str) -> bool:
        return bool(self._param(symbol, "pre_staging_enabled", False))

    # ── Price helpers ─────────────────────────────────────────────────
    @staticmethod
    def _tick_price(tick: Any) -> Optional[float]:
        """Mid price from a tick (mid property, else bid/ask mean)."""
        mid = getattr(tick, "mid", None)
        if isinstance(mid, (int, float)) and mid > 0:
            return float(mid)
        bid = getattr(tick, "bid", None)
        ask = getattr(tick, "ask", None)
        if isinstance(bid, (int, float)) and isinstance(ask, (int, float)) and bid > 0 and ask > 0:
            return (float(bid) + float(ask)) / 2.0
        for attr in ("price", "last", "close"):
            v = getattr(tick, attr, None)
            if isinstance(v, (int, float)) and v > 0:
                return float(v)
        return None

    @staticmethod
    def _boundary(zone: EntryZone) -> float:
        """Near edge price reaches first: the top for a LONG (support below
        price), the bottom for a SHORT (resistance above price)."""
        if str(getattr(zone, "direction", "")).upper() == "LONG":
            return float(getattr(zone, "top", 0.0))
        return float(getattr(zone, "bottom", 0.0))

    def _within_proximity(self, symbol: str, zone: EntryZone, price: float) -> bool:
        """True when price is approaching the zone boundary (within the
        configured proximity) but has NOT yet touched it."""
        pip_size = float(self._pip_size(symbol) or 0.0)
        if pip_size <= 0:
            return False
        proximity = float(self._param(symbol, "staging_proximity_pips", 5.0)) * pip_size
        if proximity <= 0:
            return False
        boundary = self._boundary(zone)
        if boundary <= 0:
            return False
        if str(getattr(zone, "direction", "")).upper() == "LONG":
            # Support below price: approaching = price above the boundary.
            distance = price - boundary
        else:
            # Resistance above price: approaching = price below the boundary.
            distance = boundary - price
        return 0.0 < distance <= proximity

    # ── Public entry points ───────────────────────────────────────────
    def on_tick(self, tick: Any) -> None:
        """Evaluate staging + staleness for the tick's symbol. Never raises."""
        try:
            symbol = str(getattr(tick, "symbol", "") or "")
            if not symbol or not self._enabled(symbol):
                return
            price = self._tick_price(tick)
            if price is None:
                return
            zones = list(self._get_active_zones(symbol) or [])
            # Staleness first: cancel orders whose zone is no longer active.
            self._reap_stale(symbol, zones)
            # Then stage any approaching zone that isn't already staged.
            for zone in zones:
                if self._within_proximity(symbol, zone, price):
                    self._stage_zone(symbol, zone)
        except Exception:  # noqa: BLE001 — staging never breaks the tick path
            logger.debug("[zone-stager] on_tick failed for {}", getattr(tick, "symbol", "?"))

    def on_zone_update(self, symbol: str) -> None:
        """ZoneWatcher callback — cancel staged orders for gone zones. Never raises."""
        try:
            if not symbol or not self._enabled(symbol):
                # Even when disabled, still reap so toggling off cancels orders.
                if symbol:
                    self._reap_stale(symbol, list(self._get_active_zones(symbol) or []))
                return
            zones = list(self._get_active_zones(symbol) or [])
            self._reap_stale(symbol, zones)
        except Exception:  # noqa: BLE001
            logger.debug("[zone-stager] on_zone_update failed for {}", symbol)

    def cancel_all(self, symbol: Optional[str] = None) -> None:
        """Cancel every staged order (optionally only for one symbol)."""
        with self._lock:
            keys = [
                k for k, so in self._staged.items()
                if symbol is None or so.symbol == symbol
            ]
        for key in keys:
            self._cancel_staged(key, "cancel_all")

    # ── Internals ─────────────────────────────────────────────────────
    def _reap_stale(self, symbol: str, zones: list[EntryZone]) -> None:
        """Cancel staged orders for this symbol whose zone is no longer active."""
        active_keys = {zone_key(z) for z in zones}
        with self._lock:
            stale = [
                key for key, so in self._staged.items()
                if so.symbol == symbol and key not in active_keys
            ]
        for key in stale:
            self._cancel_staged(key, "zone expired/invalidated")

    def _cancel_staged(self, key: str, reason: str) -> None:
        with self._lock:
            staged = self._staged.get(key)
        if staged is None:
            return
        ok = False
        try:
            ok = bool(self._cancel_pending(staged.symbol, staged.order_id))
        except Exception:  # noqa: BLE001 — a failed cancel is logged, not raised
            logger.debug("[zone-stager] cancel raised for {}", staged.order_id)
            ok = False
        with self._lock:
            self._staged.pop(key, None)
            # Start the re-stage cooldown from the cancel so a flapping zone
            # cannot immediately re-stage.
            self._last_stage_time[key] = time.monotonic()
            self._stats["cancelled"] += 1
        logger.info(
            "[zone-stager] {} {} pending cancel {} ({}) — {}",
            staged.symbol, staged.direction,
            "ok" if ok else "FAILED", staged.order_id, reason,
        )

    def _stage_zone(self, symbol: str, zone: EntryZone) -> None:
        key = zone_key(zone)
        direction = str(getattr(zone, "direction", "")).upper()
        with self._lock:
            if key in self._staged:
                return  # already staged — idempotent
            last = self._last_stage_time.get(key)
            cooldown = float(self._param(symbol, "staging_cooldown_s", 60.0))
            if last is not None and (time.monotonic() - last) < cooldown:
                self._stats["cooldown_skips"] += 1
                return

        boundary = self._boundary(zone)
        sl = float(getattr(zone, "invalidation_level", 0.0) or 0.0)
        if boundary <= 0 or sl <= 0:
            return

        tp1, tp2 = self._targets(symbol, direction, boundary, sl)

        # Full gate pipeline BEFORE staging — this is an earlier run of the same
        # gates, not a bypass. A None gate_check (tests) always allows.
        if self._gate_check is not None:
            try:
                allowed = bool(
                    self._gate_check(symbol, direction, zone, boundary, sl, tp1, tp2)
                )
            except Exception:  # noqa: BLE001 — a faulty gate fails closed
                logger.debug("[zone-stager] gate_check raised for {}", symbol)
                allowed = False
            if not allowed:
                with self._lock:
                    self._stats["gate_rejects"] += 1
                return

        try:
            lots = float(self._size_lookup(symbol, direction, boundary, sl) or 0.0)
        except Exception:  # noqa: BLE001
            logger.debug("[zone-stager] size_lookup raised for {}", symbol)
            lots = 0.0
        if lots <= 0:
            return

        order_kind = "BUY_LIMIT" if direction == "LONG" else "SELL_LIMIT"
        idem = f"stage:{key}"
        try:
            result = self._place_pending(
                symbol, order_kind, boundary, lots, sl, tp1, "APEX_STAGED", idem,
            )
        except Exception:  # noqa: BLE001
            logger.debug("[zone-stager] place_pending raised for {}", symbol)
            with self._lock:
                self._stats["stage_failures"] += 1
                self._last_stage_time[key] = time.monotonic()
            return

        if result is None or not bool(getattr(result, "success", False)):
            err = getattr(result, "error", "") if result is not None else "no result"
            logger.warning(
                "[zone-stager] {} {} stage FAILED @ {:.5f}: {}",
                symbol, direction, boundary, err,
            )
            with self._lock:
                self._stats["stage_failures"] += 1
                self._last_stage_time[key] = time.monotonic()
            return

        order_id = str(getattr(result, "order_id", "") or "")
        with self._lock:
            self._staged[key] = StagedOrder(
                order_id=order_id,
                symbol=symbol,
                direction=direction,
                zone_key=key,
                boundary=boundary,
                stop_loss=sl,
                take_profit=tp1,
                lots=lots,
                staged_at=time.monotonic(),
            )
            self._last_stage_time[key] = time.monotonic()
            self._stats["staged"] += 1
        logger.info(
            "[zone-stager] {} {} {} staged @ {:.5f} SL={:.5f} TP={:.5f} lots={} ({})",
            symbol, direction, order_kind, boundary, sl, tp1, lots, order_id,
        )

    def _targets(
        self, symbol: str, direction: str, entry_price: float, sl: float,
    ) -> tuple[float, float]:
        if self._derive_targets is not None:
            try:
                tp1, tp2 = self._derive_targets(symbol, direction, entry_price, sl)
                if tp1 and tp1 > 0:
                    return float(tp1), float(tp2 or tp1)
            except Exception:  # noqa: BLE001
                logger.debug("[zone-stager] derive_targets raised for {}", symbol)
        # Risk-multiple fallback (respects a sensible default R:R).
        risk = abs(entry_price - sl)
        if risk <= 0:
            risk = entry_price * 0.001 if entry_price > 0 else 1.0
        if direction == "LONG":
            return entry_price + risk * 1.5, entry_price + risk * 3.0
        return entry_price - risk * 1.5, entry_price - risk * 3.0
