"""APEX TRADER — Zone Watcher (Phase 7).

Subscribes to WorldModel updates and maintains active entry zones
per symbol.  Zones are extracted from FVGs (OPEN status) and Order
Blocks (FRESH / TESTED status) in the WorldModel, ranked by
confluence type (FVG+OB overlap > FVG alone > OB alone).

Thread-safe: the internal zone dict is Lock-protected so tick
handlers and the analysis plane can read/write concurrently.
"""

from __future__ import annotations

import threading
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

from loguru import logger

from brain.fvg_detector import FairValueGap, FVGStatus
from brain.order_block import OrderBlock, OBStatus
from brain.world_model import WorldModel, WorldModelStore
from entry.models import EntryConfig, EntryZone, ZoneType


def _resolve_bias(struct_by_tf: dict) -> Optional[str]:
    """Derive directional bias from HTF structure (H4 > H1 > D1)."""
    for tf in ("H4", "H1", "D1"):
        sa = struct_by_tf.get(tf)
        if sa is None:
            continue
        trend = sa.trend.value if hasattr(sa.trend, "value") else str(sa.trend)
        if trend == "BULLISH":
            return "LONG"
        if trend == "BEARISH":
            return "SHORT"
    return None


def _invalidation(direction: str, bottom: float, top: float) -> float:
    zone_size = abs(top - bottom)
    buffer = zone_size * 0.5
    if direction == "LONG":
        return bottom - buffer
    return top + buffer


def _zone_identity(z: EntryZone) -> tuple:
    """Stable identity for a zone: price level + direction + source TF.

    Used to recognise the SAME zone across successive WorldModel publishes so
    its original ``created_at``/``expires_at`` survive — otherwise re-deriving
    zones every timeframe close perpetually resets the expiry clock and zones
    never age out of the 15-minute window.
    """
    return (z.direction, z.zone_type, round(z.top, 5), round(z.bottom, 5), z.timeframe)


def extract_entry_zones(
    model: WorldModel,
    config: Optional[EntryConfig] = None,
    edge_weight: Optional[Callable[..., float]] = None,
) -> list[EntryZone]:
    """Derive actionable entry zones from a WorldModel snapshot.

    This is the single, canonical zone-derivation implementation.  The
    analysis plane calls it at publish time to populate
    ``WorldModel.entry_zones``; ``ZoneWatcher`` reads that stored result
    rather than re-deriving.

    Priority: FVG+OB overlap > FVG alone > OB alone.
    Only OPEN/PARTIALLY FVGs and FRESH/TESTED OBs qualify.

    ``edge_weight`` (optional) is a learned, bounded multiplier looked up as
    ``edge_weight(symbol, direction, zone_type, regime) -> float``.  It scales
    the base conviction so a zone's score reflects its realized track record.
    It defaults to neutral (no change) and any lookup error is swallowed, so
    the analysis plane can never be broken by the learning layer.
    """
    cfg = config or EntryConfig()
    now = datetime.now(timezone.utc)
    # Expiry is now computed per-zone from its source timeframe (see below) —
    # a flat 15-minute TTL killed every H1/H4/D1 setup before price could
    # plausibly return to it. `expiry` here is kept only as the M5/legacy
    # fallback for any timeframe not in the per-TF table.
    expiry = now + timedelta(seconds=cfg.zone_expiry_seconds_default)
    zones: list[EntryZone] = []

    fvgs_by_tf = model.fvgs_by_tf()
    obs_by_tf = model.order_blocks_by_tf()
    regime_by_tf = model.regime_by_tf()

    def _conv(
        base: int,
        direction: str,
        zone_type: ZoneType,
        tf: str,
    ) -> int:
        """Scale a base conviction by the learned edge (default-neutral).

        Every zone receives its full base conviction × learned edge weight —
        there is no counter-trend haircut. Directional risk is judged solely by
        the downstream gates (EV gate, DecisionEngine, RiskGovernor) on each
        entry's own structural merit, not pre-penalised by a crude HTF bias
        label.
        """
        val = float(base)
        if edge_weight is not None:
            try:
                regime = regime_by_tf.get(tf)
                w = float(edge_weight(model.symbol, direction, zone_type.value, regime))
                val = base * w
            except Exception:  # noqa: BLE001 — learning must never break analysis
                val = float(base)
        return max(1, min(100, int(round(val))))

    all_fvgs: list[tuple[str, FairValueGap]] = []
    for tf, fvg_list in fvgs_by_tf.items():
        for fvg in fvg_list:
            if fvg.status in (FVGStatus.OPEN, FVGStatus.PARTIALLY):
                all_fvgs.append((tf, fvg))

    all_obs: list[tuple[str, OrderBlock]] = []
    for tf, ob_list in obs_by_tf.items():
        for ob in ob_list:
            if ob.status in (OBStatus.FRESH, OBStatus.TESTED):
                all_obs.append((tf, ob))

    used_fvgs: set[int] = set()
    used_obs: set[int] = set()

    for fi, (ftf, fvg) in enumerate(all_fvgs):
        for oi, (otf, ob) in enumerate(all_obs):
            if fvg.kind != ob.kind:
                continue
            overlap_top = min(fvg.top, ob.top)
            overlap_bottom = max(fvg.bottom, ob.bottom)
            if overlap_top <= overlap_bottom:
                continue

            direction = "LONG" if fvg.kind == "BULLISH" else "SHORT"

            inv = _invalidation(direction, overlap_bottom, overlap_top)
            # Overlap zones are confirmed on two timeframes — use whichever
            # gives the longer TTL (the more durable structure governs how
            # long the confluence stays tradeable).
            overlap_ttl = max(
                cfg.get_zone_expiry_seconds(ftf),
                cfg.get_zone_expiry_seconds(otf),
            )
            zones.append(EntryZone(
                symbol=model.symbol,
                direction=direction,
                zone_type=ZoneType.FVG_OB_OVERLAP,
                top=overlap_top,
                bottom=overlap_bottom,
                midpoint=(overlap_top + overlap_bottom) / 2,
                invalidation_level=inv,
                conviction=_conv(100, direction, ZoneType.FVG_OB_OVERLAP, ftf),
                created_at=now,
                expires_at=now + timedelta(seconds=overlap_ttl),
                timeframe=ftf,
                has_sweep=False,
                is_counter_trend=False,
                bias_direction="",
            ))
            used_fvgs.add(fi)
            used_obs.add(oi)

    for fi, (ftf, fvg) in enumerate(all_fvgs):
        if fi in used_fvgs:
            continue
        direction = "LONG" if fvg.kind == "BULLISH" else "SHORT"
        inv = _invalidation(direction, fvg.bottom, fvg.top)
        zones.append(EntryZone(
            symbol=model.symbol,
            direction=direction,
            zone_type=ZoneType.FVG_MIDPOINT,
            top=fvg.top,
            bottom=fvg.bottom,
            midpoint=fvg.midpoint,
            invalidation_level=inv,
            conviction=_conv(80, direction, ZoneType.FVG_MIDPOINT, ftf),
            created_at=now,
            expires_at=now + timedelta(seconds=cfg.get_zone_expiry_seconds(ftf)),
            timeframe=ftf,
            is_counter_trend=False,
            bias_direction="",
        ))

    for oi, (otf, ob) in enumerate(all_obs):
        if oi in used_obs:
            continue
        direction = "LONG" if ob.kind == "BULLISH" else "SHORT"
        inv = _invalidation(direction, ob.bottom, ob.top)
        zones.append(EntryZone(
            symbol=model.symbol,
            direction=direction,
            zone_type=ZoneType.OB_MIDPOINT,
            top=ob.top,
            bottom=ob.bottom,
            midpoint=ob.midpoint,
            invalidation_level=inv,
            conviction=_conv(70, direction, ZoneType.OB_MIDPOINT, otf),
            created_at=now,
            expires_at=now + timedelta(seconds=cfg.get_zone_expiry_seconds(otf)),
            timeframe=otf,
            is_counter_trend=False,
            bias_direction="",
        ))

    return zones


class ZoneWatcher:
    """Watches WorldModel for actionable entry zones."""

    def __init__(
        self,
        world_model_store: WorldModelStore,
        config: Optional[EntryConfig] = None,
    ) -> None:
        self._store = world_model_store
        self._config = config or EntryConfig()
        self._zones: dict[str, list[EntryZone]] = {}
        self._lock = threading.Lock()
        self._last_versions: dict[str, int] = {}

    def on_world_model_update(self, symbol: str) -> None:
        """Called when a WorldModel for *symbol* is published.

        Reads the latest WorldModel from the store, extracts zones,
        and replaces the zone set for that symbol atomically.
        """
        model = self._store.get(symbol)
        if model is None:
            logger.debug("[zone-watcher] no WorldModel for {}", symbol)
            return

        last_v = self._last_versions.get(symbol, -1)
        if model.version <= last_v:
            return
        self._last_versions[symbol] = model.version

        # The WorldModel is the single source of truth: read the zones the
        # analysis plane synthesized at publish time.  Fall back to deriving
        # them on the fly only if the producer did not populate them (e.g.
        # a WorldModel built by an older code path or a test fixture).
        if model.entry_zones:
            zones = list(model.entry_zones)
        else:
            zones = extract_entry_zones(model, self._config)
        with self._lock:
            if zones:
                # Preserve the original created_at/expires_at for any zone whose
                # identity already existed. extract_entry_zones stamps a fresh
                # ``now`` expiry on every publish; without this carry-over the
                # 900s expiry would be perpetually refreshed and zones would
                # never age out.
                prev = {_zone_identity(z): z for z in self._zones.get(symbol, [])}
                merged: list[EntryZone] = []
                for z in zones:
                    old = prev.get(_zone_identity(z))
                    if old is not None:
                        z = replace(
                            z,
                            created_at=old.created_at,
                            expires_at=old.expires_at,
                        )
                    merged.append(z)
                self._zones[symbol] = merged
            else:
                self._zones.pop(symbol, None)

        if zones:
            logger.debug(
                "[zone-watcher] {} → {} zone(s) (v{})",
                symbol, len(zones), model.version,
            )

    def get_active_zones(self, symbol: str) -> list[EntryZone]:
        """Return current entry zones for *symbol* (may be empty)."""
        now = datetime.now(timezone.utc)
        with self._lock:
            raw = self._zones.get(symbol, [])
            return [z for z in raw if z.expires_at > now]

    def all_symbols_with_zones(self) -> list[str]:
        """Symbols that currently have at least one non-expired zone."""
        now = datetime.now(timezone.utc)
        with self._lock:
            return [
                sym for sym, zones in self._zones.items()
                if any(z.expires_at > now for z in zones)
            ]

    def clear(self, symbol: Optional[str] = None) -> None:
        with self._lock:
            if symbol:
                self._zones.pop(symbol, None)
            else:
                self._zones.clear()

    def _extract_zones(self, model: WorldModel) -> list[EntryZone]:
        """Derive entry zones from a WorldModel snapshot.

        DEPRECATED / unused: the analysis plane populates
        ``WorldModel.entry_zones`` via the canonical module-level
        :func:`extract_entry_zones` at publish time, and ``ZoneWatcher`` reads
        that stored result rather than re-deriving. Kept only as a thin wrapper
        for backward compatibility; no live code path calls it.
        """
        return extract_entry_zones(model, self._config)

