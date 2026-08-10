"""APEX TRADER — StructuralInteractionDetector (Violation #2).

Between candle closes the Brain's structural evidence is frozen: FVG zones,
order blocks, liquidity pools and key structure levels only refresh when the
:class:`~scanner.candle_close_handler.CandleCloseHandler` re-runs the brain
modules on an M5/M15/H1/H4/D1 close. A liquidity pool swept at 10:02 is invisible
until the M5 close at 10:05 — the Brain is blind to sweeps, failed breakouts,
zone reactions and FVG fills that develop *inside* a bar.

This is a **lightweight, tick-driven structural-interaction detector**. It does
NOT re-run the (expensive) brain modules. Instead it:

1. Caches the CONFIRMED WorldModel's already-computed structural levels per
   symbol (FVG/OB zones, liquidity pools, swing/BOS/CHOCH levels), refreshing
   the cache whenever a ``world_model_update`` fires (i.e. on candle close) and
   lazily on the first tick for a symbol.
2. On each ``tick`` event it checks the live mid-price against those cached
   levels and classifies any interaction:
     * ``level_touch`` — price reached a level (within tolerance),
     * ``level_sweep`` — price pierced through a level and reclaimed back
       (the classic stop-hunt / failed-breakout signature),
     * ``level_break`` — price crossed a level and *stayed* through it,
     * ``fvg_fill``    — price entered a fair-value-gap zone.
3. On a detected interaction it (a) records a short-lived, NON-directional
   :class:`~cognition.contracts.Evidence` so the Brain literally sees the
   interaction next time it consolidates, and (b) wakes the Brain immediately
   via :meth:`CognitionLoop.maybe_reason_on_change` so it reasons *now* rather
   than on the next scheduled cycle.

Everything is fail-safe: the EventBus swallows callback faults, and every public
method is guarded so a detector error can never break the tick pipeline. Pure
stdlib + the shared contracts — no pandas — so it is fully offline-testable.
"""

from __future__ import annotations

import threading
import time as _time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from loguru import logger

from cognition.contracts import Evidence, EvidenceDomain
from tick.event_bus import EventBus
from tick.models import Tick


# Interaction type tokens (also the measurement/observation vocabulary).
TOUCH = "level_touch"
SWEEP = "level_sweep"
BREAK = "level_break"
FVG_FILL = "fvg_fill"

_STRUCTURE_KINDS = frozenset({"fvg", "order_block", "structure"})


def _f(v: Any, default: float = 0.0) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    if f != f or f in (float("inf"), float("-inf")):
        return default
    return f


def _decimals_for(price: float) -> int:
    p = abs(price)
    if p >= 100.0:
        return 2
    if p >= 1.0:
        return 4
    return 6


@dataclass(frozen=True)
class StructuralLevel:
    """One structural price level or zone extracted from a WorldModel.

    ``low``/``high`` bound a zone (FVG / order block); for a point level
    (liquidity pool, swing / BOS / CHOCH) ``low == high == ref``. ``kind`` is the
    structural family (``fvg`` / ``order_block`` / ``liquidity`` / ``structure``)
    and ``side`` is a raw descriptive tag (e.g. ``bullish`` / ``sell_side`` /
    ``swing_high``) — never a trade direction.
    """

    kind: str
    tf: str
    ref: float
    low: float
    high: float
    side: str = ""

    @property
    def is_zone(self) -> bool:
        return self.high > self.low

    def key(self) -> str:
        dp = _decimals_for(self.ref or self.low or self.high)
        return (
            f"{self.kind}|{self.tf}|{round(self.ref, dp)}|"
            f"{round(self.low, dp)}|{round(self.high, dp)}"
        )

    def label(self) -> str:
        tag = f" {self.side}" if self.side else ""
        return f"{self.tf}{tag} {self.kind}".strip()


@dataclass
class _SymbolState:
    """Mutable per-symbol tick state used for crossing / sweep detection."""

    last_mid: float = 0.0
    signs: dict = field(default_factory=dict)      # level_key -> last sign (-1/0/1)
    inside: dict = field(default_factory=dict)     # level_key -> bool (in zone)
    pending: dict = field(default_factory=dict)    # level_key -> (crossed_to_side, epoch)
    last_emit: dict = field(default_factory=dict)  # (level_key, interaction) -> epoch
    last_wake: float = 0.0

    def prune(self, live_keys: "set[str]") -> None:
        """Drop per-level state for levels that no longer exist (bound memory)."""
        for store in (self.signs, self.inside, self.pending):
            for k in [k for k in store if k not in live_keys]:
                store.pop(k, None)
        for ke in [ke for ke in self.last_emit if ke[0] not in live_keys]:
            self.last_emit.pop(ke, None)


class StructuralInteractionDetector:
    """Tick-driven inter-candle structural interaction detector (Violation #2).

    Parameters
    ----------
    world_model_store :
        The CONFIRMED :class:`~brain.world_model.WorldModelStore`. Read (never
        written) for each symbol's pre-computed structural artifacts.
    event_bus :
        The Phase-2 :class:`~tick.event_bus.EventBus`. The detector subscribes to
        ``"tick"`` (the live price) and ``"world_model_update"`` (cache refresh).
    cognition_loop :
        Object exposing ``maybe_reason_on_change(symbol, magnitude)`` — the Brain
        wake. Optional; when absent the detector still records evidence.
    pip_size_provider :
        ``symbol -> pip_size`` callable for the touch/break tolerance. Defaults to
        ``config.get_pip_size``.
    tolerance_pips :
        How close (in pips) price must come to count as a level ``touch`` and how
        far beyond it must move to count as a decisive ``break``.
    sweep_window_seconds :
        A pierce that reclaims within this window is a ``level_sweep``; one that
        stays through past it is a confirmed ``level_break``.
    emit_cooldown_seconds :
        Per (level, interaction) debounce so a level being probed tick-by-tick
        does not spam identical evidence / wakes.
    evidence_ttl_seconds :
        Relevance horizon on the emitted Evidence — it ages out so a stale
        interaction cannot masquerade as current reality.
    max_levels :
        Upper bound on cached levels per symbol (bounds per-tick cost).
    enabled :
        Master switch. When False every hook is an immediate no-op.
    """

    def __init__(
        self,
        *,
        world_model_store: Any,
        event_bus: EventBus,
        cognition_loop: Optional[Any] = None,
        pip_size_provider: Optional[Callable[[str], float]] = None,
        tolerance_pips: float = 2.0,
        sweep_window_seconds: float = 12.0,
        emit_cooldown_seconds: float = 15.0,
        evidence_ttl_seconds: float = 45.0,
        max_levels: int = 48,
        enabled: bool = True,
    ) -> None:
        self._store = world_model_store
        self._bus = event_bus
        self._loop = cognition_loop
        self._pip_size_provider = pip_size_provider
        self._tolerance_pips = max(0.1, float(tolerance_pips))
        self._sweep_window = max(0.5, float(sweep_window_seconds))
        self._emit_cooldown = max(0.0, float(emit_cooldown_seconds))
        self._evidence_ttl = max(1.0, float(evidence_ttl_seconds))
        self._max_levels = max(1, int(max_levels))
        self._enabled = bool(enabled)

        self._lock = threading.RLock()
        self._levels: dict[str, list[StructuralLevel]] = {}
        self._level_keys: dict[str, set[str]] = {}
        self._state: dict[str, _SymbolState] = {}
        self._evidence: dict[str, list[Evidence]] = {}

        # Observability counters.
        self._ticks_seen = 0
        self._interactions = 0
        self._wakes = 0
        self._by_type: dict[str, int] = {}

        self._running = False

    # ── Lifecycle ─────────────────────────────────────────────────────────

    def start(self) -> None:
        """Subscribe to the tick + world-model event streams (idempotent)."""
        if not self._enabled or self._running:
            return
        self._bus.subscribe("tick", self._on_tick)
        self._bus.subscribe("world_model_update", self._on_world_model_update)
        self._running = True
        logger.info(
            "[structural-interaction] started (tol={}pips, sweep_window={}s)",
            self._tolerance_pips, self._sweep_window,
        )

    def shutdown(self) -> None:
        """Unsubscribe from the event streams (idempotent)."""
        if not self._running:
            return
        self._bus.unsubscribe("tick", self._on_tick)
        self._bus.unsubscribe("world_model_update", self._on_world_model_update)
        self._running = False

    def set_cognition_loop(self, loop: Optional[Any]) -> None:
        """Wire (or clear) the Brain-wake target after construction."""
        with self._lock:
            self._loop = loop

    # ── Evidence source (consumed by the EvidenceConsolidator) ─────────────

    def evidence_for(self, symbol: str, now: Optional[float] = None) -> "list[Evidence]":
        """Return the still-fresh structural-interaction Evidence for ``symbol``.

        A ``symbol -> list[Evidence]`` source the cognition consolidator can pull
        so a detected interaction rides into the MarketState the Brain reasons
        over. Prunes aged-out evidence as a side effect. Fail-safe: ``[]``.
        """
        try:
            sym = str(symbol or "")
            t = _time.time() if now is None else float(now)
            with self._lock:
                buf = self._evidence.get(sym)
                if not buf:
                    return []
                fresh = [e for e in buf if e.is_fresh(t)]
                self._evidence[sym] = fresh
                return list(fresh)
        except Exception as exc:  # noqa: BLE001 — a source read must never raise
            logger.debug("[structural-interaction] evidence_for({}) fault: {}", symbol, exc)
            return []

    def stats(self) -> dict:
        with self._lock:
            return {
                "ticks_seen": self._ticks_seen,
                "interactions": self._interactions,
                "wakes": self._wakes,
                "by_type": dict(self._by_type),
                "symbols_cached": len(self._levels),
            }

    # ── EventBus callbacks ─────────────────────────────────────────────────

    def _on_world_model_update(self, symbol: Any) -> None:
        """Refresh the cached structural levels for a symbol on candle close."""
        try:
            sym = symbol if isinstance(symbol, str) else str(getattr(symbol, "symbol", "") or "")
            if sym:
                self._refresh_levels(sym)
        except Exception as exc:  # noqa: BLE001
            logger.debug("[structural-interaction] refresh fault for {}: {}", symbol, exc)

    def _on_tick(self, tick: Tick) -> None:
        """Check the live tick against the cached levels (fast, fail-safe)."""
        if not self._enabled:
            return
        try:
            symbol = str(getattr(tick, "symbol", "") or "")
            mid = _f(getattr(tick, "mid", 0.0))
            if not symbol or mid <= 0:
                return
            epoch = _f(getattr(tick, "epoch", 0.0)) or _time.time()

            with self._lock:
                self._ticks_seen += 1
                levels = self._levels.get(symbol)
                if levels is None:
                    levels = self._refresh_levels_locked(symbol)
                if not levels:
                    st = self._state.get(symbol)
                    if st is not None:
                        st.last_mid = mid
                    return
                st = self._state.get(symbol)
                if st is None:
                    st = _SymbolState()
                    self._state[symbol] = st
                tol = self._tolerance_for(symbol)
                detections = self._scan_locked(symbol, st, levels, mid, epoch, tol)
                st.last_mid = mid
                if detections:
                    self._record_locked(symbol, detections, mid, epoch)

            # Wake the Brain OUTSIDE the lock (the loop only enqueues + signals).
            if detections:
                self._wake_brain(symbol, epoch)
        except Exception as exc:  # noqa: BLE001 — never break the tick pipeline
            logger.debug("[structural-interaction] tick fault: {}", exc)

    # ── Level cache ────────────────────────────────────────────────────────

    def _refresh_levels(self, symbol: str) -> list[StructuralLevel]:
        with self._lock:
            return self._refresh_levels_locked(symbol)

    def _refresh_levels_locked(self, symbol: str) -> list[StructuralLevel]:
        wm = None
        try:
            if self._store is not None:
                wm = self._store.get(symbol)
        except Exception as exc:  # noqa: BLE001
            logger.debug("[structural-interaction] store.get({}) fault: {}", symbol, exc)
            wm = None
        levels = _extract_levels(wm, self._max_levels) if wm is not None else []
        self._levels[symbol] = levels
        keys = {lv.key() for lv in levels}
        self._level_keys[symbol] = keys
        st = self._state.get(symbol)
        if st is not None:
            st.prune(keys)
        return levels

    def _tolerance_for(self, symbol: str) -> float:
        pip = 0.0
        try:
            if self._pip_size_provider is not None:
                pip = _f(self._pip_size_provider(symbol))
            else:
                from config import get_pip_size
                pip = _f(get_pip_size(symbol))
        except Exception:  # noqa: BLE001
            pip = 0.0
        if pip <= 0:
            pip = 0.0001
        return pip * self._tolerance_pips

    # ── Core scan (lock held) ──────────────────────────────────────────────

    def _scan_locked(
        self,
        symbol: str,
        st: _SymbolState,
        levels: "list[StructuralLevel]",
        mid: float,
        epoch: float,
        tol: float,
    ) -> "list[tuple[StructuralLevel, str, float]]":
        """Classify interactions of ``mid`` against every cached level.

        Returns a list of ``(level, interaction, distance_pips)`` — usually
        empty. Updates the per-level crossing / zone / pierce state in place.
        """
        out: list[tuple[StructuralLevel, str, float]] = []
        first_tick = st.last_mid <= 0
        for lv in levels:
            key = lv.key()
            if lv.is_zone:
                self._scan_zone(st, lv, key, mid, epoch, first_tick, out)
            else:
                self._scan_point(st, lv, key, mid, epoch, tol, first_tick, out)
        return out

    def _scan_zone(
        self,
        st: _SymbolState,
        lv: StructuralLevel,
        key: str,
        mid: float,
        epoch: float,
        first_tick: bool,
        out: list,
    ) -> None:
        inside = lv.low <= mid <= lv.high
        was_inside = bool(st.inside.get(key, False))
        st.inside[key] = inside
        # On the first observation we only learn the current state — we cannot
        # know whether price just entered the zone or was already sitting in it.
        if first_tick:
            return
        if inside and not was_inside:
            interaction = FVG_FILL if lv.kind == "fvg" else TOUCH
            if self._cooldown_ok(st, key, interaction, epoch):
                out.append((lv, interaction, 0.0))

    def _scan_point(
        self,
        st: _SymbolState,
        lv: StructuralLevel,
        key: str,
        mid: float,
        epoch: float,
        tol: float,
        first_tick: bool,
        out: list,
    ) -> None:
        ref = lv.ref
        if mid > ref + tol:
            cur_sign = 1
        elif mid < ref - tol:
            cur_sign = -1
        else:
            cur_sign = 0
        prev_sign = st.signs.get(key)
        st.signs[key] = cur_sign

        # Resolve any pending pierce first (sweep vs confirmed break).
        pend = st.pending.get(key)
        if pend is not None:
            crossed_to, t0 = pend
            reclaimed = cur_sign != 0 and cur_sign == -crossed_to
            if reclaimed:
                st.pending.pop(key, None)
                if self._cooldown_ok(st, key, SWEEP, epoch):
                    out.append((lv, SWEEP, self._pips(lv, mid)))
                return
            if (epoch - t0) >= self._sweep_window:
                st.pending.pop(key, None)
                if self._cooldown_ok(st, key, BREAK, epoch):
                    out.append((lv, BREAK, self._pips(lv, mid)))

        if prev_sign is None or first_tick:
            return
        # A fresh cross to the other side registers a pending pierce (which will
        # resolve into a sweep on reclaim or a break if it holds through).
        if prev_sign != 0 and cur_sign != 0 and cur_sign != prev_sign:
            if key not in st.pending:
                st.pending[key] = (cur_sign, epoch)
        # Reaching the level (entering the tolerance band) is a touch.
        elif cur_sign == 0 and prev_sign != 0:
            if self._cooldown_ok(st, key, TOUCH, epoch):
                out.append((lv, TOUCH, self._pips(lv, mid)))

    def _cooldown_ok(self, st: _SymbolState, key: str, interaction: str, epoch: float) -> bool:
        if self._emit_cooldown <= 0:
            return True
        last = st.last_emit.get((key, interaction), 0.0)
        if (epoch - last) < self._emit_cooldown:
            return False
        st.last_emit[(key, interaction)] = epoch
        return True

    @staticmethod
    def _pips(lv: StructuralLevel, mid: float) -> float:
        return abs(mid - lv.ref)

    # ── Emission ───────────────────────────────────────────────────────────

    def _record_locked(
        self,
        symbol: str,
        detections: "list[tuple[StructuralLevel, str, float]]",
        mid: float,
        epoch: float,
    ) -> None:
        buf = self._evidence.setdefault(symbol, [])
        # Prune aged-out evidence up front so the buffer stays bounded.
        buf[:] = [e for e in buf if e.is_fresh(epoch)]
        for lv, interaction, dist in detections:
            self._interactions += 1
            self._by_type[interaction] = self._by_type.get(interaction, 0) + 1
            buf.append(self._build_evidence(symbol, lv, interaction, mid, dist, epoch))
        # Keep only the most recent handful so a busy symbol cannot grow unbounded.
        if len(buf) > 24:
            del buf[:-24]

    def _build_evidence(
        self, symbol: str, lv: StructuralLevel, interaction: str, mid: float,
        dist: float, epoch: float,
    ) -> Evidence:
        dp = _decimals_for(lv.ref or mid)
        domain = (
            EvidenceDomain.LIQUIDITY if lv.kind == "liquidity"
            else EvidenceDomain.STRUCTURE
        )
        pretty = interaction.replace("_", " ")
        where = (
            f"{round(lv.low, dp)}-{round(lv.high, dp)}" if lv.is_zone
            else f"{round(lv.ref, dp)}"
        )
        observation = (
            f"{symbol} {lv.label()} {pretty} @ {where} (price {round(mid, dp)})"
        )[:300]
        measurements = {
            "interaction": interaction,
            "level_kind": lv.kind,
            "level_side": lv.side,
            "timeframe": lv.tf,
            "level_ref": round(lv.ref, dp),
            "zone_low": round(lv.low, dp),
            "zone_high": round(lv.high, dp),
            "price": round(mid, dp),
            "distance": round(dist, dp),
            "inter_candle": True,
        }
        # Non-directional by construction (polarity 0); the Brain forms the
        # direction. confidence reflects how *clean* the interaction is (a sweep
        # or fill is a sharper signal than a mere touch), never a lean.
        confidence = {
            SWEEP: 0.62, BREAK: 0.58, FVG_FILL: 0.55, TOUCH: 0.45,
        }.get(interaction, 0.5)
        return Evidence(
            source_module="market.structural_interaction",
            domain=domain,
            symbol=symbol,
            observation=observation,
            confidence=confidence,
            uncertainty=round(1.0 - confidence, 4),
            polarity=0.0,
            measurements=measurements,
            relevance_horizon_seconds=self._evidence_ttl,
            # Anchor freshness to the tick's market time (when the interaction
            # actually happened) so the TTL prune is consistent with the tick
            # clock the detector runs on. tick.epoch is a POSIX timestamp, the
            # same basis the consolidator's ``now`` uses.
            timestamp_epoch=epoch if epoch > 0 else 0.0,
        )

    def _wake_brain(self, symbol: str, epoch: float) -> None:
        loop = self._loop
        if loop is None:
            return
        nudge = getattr(loop, "maybe_reason_on_change", None)
        if not callable(nudge):
            return
        try:
            # A structural interaction is a DISCRETE market event, not a slow
            # magnitude drift, so it wakes the Brain with a bare nudge
            # (change_magnitude=None) — which always triggers subject to the
            # loop's own per-symbol floor — rather than a magnitude delta that a
            # concurrent caller could cancel out. Non-directional by contract.
            if nudge(symbol, None):
                with self._lock:
                    self._wakes += 1
        except Exception as exc:  # noqa: BLE001 — a wake must never break the tick path
            logger.debug("[structural-interaction] wake fault for {}: {}", symbol, exc)


# ── WorldModel → normalized levels ─────────────────────────────────────────


def _extract_levels(wm: Any, max_levels: int) -> "list[StructuralLevel]":
    """Flatten a WorldModel's active structural artifacts into price levels.

    Only levels that are still *interactable* are kept — open/partially-filled
    FVGs, un-broken order blocks, un-swept liquidity pools, and the current
    swing / BOS / CHOCH structure levels. Fail-safe: returns whatever it could
    parse, capped at ``max_levels`` (closest-to-price families first).
    """
    levels: list[StructuralLevel] = []
    try:
        levels.extend(_fvg_levels(wm))
    except Exception:  # noqa: BLE001
        pass
    try:
        levels.extend(_order_block_levels(wm))
    except Exception:  # noqa: BLE001
        pass
    try:
        levels.extend(_liquidity_levels(wm))
    except Exception:  # noqa: BLE001
        pass
    try:
        levels.extend(_structure_levels(wm))
    except Exception:  # noqa: BLE001
        pass
    # De-duplicate by key, preserving first occurrence, and cap.
    seen: set[str] = set()
    out: list[StructuralLevel] = []
    for lv in levels:
        k = lv.key()
        if k in seen:
            continue
        seen.add(k)
        out.append(lv)
        if len(out) >= max_levels:
            break
    return out


def _status_name(obj: Any) -> str:
    status = getattr(obj, "status", None)
    return str(getattr(status, "value", status) or "").upper()


def _fvg_levels(wm: Any) -> "list[StructuralLevel]":
    out: list[StructuralLevel] = []
    by_tf = wm.fvgs_by_tf() if hasattr(wm, "fvgs_by_tf") else {}
    for tf, fvgs in by_tf.items():
        for fvg in fvgs or ():
            if _status_name(fvg) in ("FILLED", "MITIGATED"):
                continue
            top = _f(getattr(fvg, "top", 0.0))
            bottom = _f(getattr(fvg, "bottom", 0.0))
            mid = _f(getattr(fvg, "midpoint", 0.0)) or ((top + bottom) / 2.0)
            if top <= 0 or bottom <= 0 or top < bottom:
                continue
            out.append(StructuralLevel(
                kind="fvg", tf=str(tf), ref=mid, low=bottom, high=top,
                side=str(getattr(fvg, "kind", "") or "").lower(),
            ))
    return out


def _order_block_levels(wm: Any) -> "list[StructuralLevel]":
    out: list[StructuralLevel] = []
    by_tf = wm.order_blocks_by_tf() if hasattr(wm, "order_blocks_by_tf") else {}
    for tf, obs in by_tf.items():
        for ob in obs or ():
            if _status_name(ob) == "BROKEN":
                continue
            top = _f(getattr(ob, "top", 0.0))
            bottom = _f(getattr(ob, "bottom", 0.0))
            mid = _f(getattr(ob, "midpoint", 0.0)) or ((top + bottom) / 2.0)
            if top <= 0 or bottom <= 0 or top < bottom:
                continue
            out.append(StructuralLevel(
                kind="order_block", tf=str(tf), ref=mid, low=bottom, high=top,
                side=str(getattr(ob, "kind", "") or "").lower(),
            ))
    return out


def _liquidity_levels(wm: Any) -> "list[StructuralLevel]":
    out: list[StructuralLevel] = []
    by_tf = wm.liquidity_by_tf() if hasattr(wm, "liquidity_by_tf") else {}
    for tf, lq in by_tf.items():
        if lq is None:
            continue
        for attr, side in (("buy_side_liquidity", "buy_side"),
                           ("sell_side_liquidity", "sell_side")):
            for zone in (getattr(lq, attr, None) or []):
                if bool(getattr(zone, "swept", False)):
                    continue
                price = _f(getattr(zone, "price", 0.0))
                if price <= 0:
                    continue
                out.append(StructuralLevel(
                    kind="liquidity", tf=str(tf), ref=price, low=price, high=price,
                    side=side,
                ))
    return out


def _structure_levels(wm: Any) -> "list[StructuralLevel]":
    out: list[StructuralLevel] = []
    by_tf = wm.structure_by_tf() if hasattr(wm, "structure_by_tf") else {}
    for tf, sa in by_tf.items():
        if sa is None:
            continue
        for attr in ("swing_high", "swing_low", "last_bos_level", "last_choch_level"):
            price = _f(getattr(sa, attr, None))
            if price <= 0:
                continue
            out.append(StructuralLevel(
                kind="structure", tf=str(tf), ref=price, low=price, high=price,
                side=attr,
            ))
    return out


__all__ = [
    "StructuralInteractionDetector",
    "StructuralLevel",
    "TOUCH",
    "SWEEP",
    "BREAK",
    "FVG_FILL",
]
