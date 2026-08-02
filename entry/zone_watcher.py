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
from typing import Any, Callable, Optional

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


# When ``vwap_zone_proximity_pips`` is 0 (no explicit pip distance) the "near
# the band" test uses a volatility-scaled proximity derived from the band's own
# deviation unit (the standard deviation that defines the bands). Half a
# standard deviation keeps the trigger tight enough that mid-band price never
# fires while a genuine band touch always does.
_VWAP_PROXIMITY_STD_FRACTION = 0.5


def _resolve_vwap_param(profile: Optional[Any], cfg: Any, name: str, default: Any) -> Any:
    """Resolve a VWAP tuning knob: InstrumentProfile first, then EntryConfig.

    Falls back to ``default`` when neither source supplies the field. A profile
    value of ``None`` (field absent) defers to the EntryConfig global.
    """
    if profile is not None:
        val = getattr(profile, name, None)
        if val is not None:
            return val
    if cfg is not None:
        val = getattr(cfg, name, None)
        if val is not None:
            return val
    return default


def _build_vwap_zones(
    *,
    model: WorldModel,
    cfg: EntryConfig,
    profile: Optional[Any],
    m5_df: Any,
    session_open_minutes: int,
    current_price: float,
    pip_size: float,
    now: datetime,
    conv: Callable[..., int],
) -> list[EntryZone]:
    """Build session-VWAP deviation-band entry zones (LONG/SHORT support/resistance).

    Returns at most one zone: a LONG when the HTF trend is up and price sits
    within proximity of the lower band, or a SHORT when the trend is down and
    price sits within proximity of the upper band. Empty when the trend is
    undecided, the VWAP/bands are unavailable, or price is not near a band edge.
    """
    from brain.session_vwap import vwap_with_bands

    bias = _resolve_bias(model.structure_by_tf())
    if bias not in ("LONG", "SHORT"):
        return []

    num_std = float(_resolve_vwap_param(profile, cfg, "vwap_zone_deviation", 1.5))
    bands = vwap_with_bands(m5_df, int(session_open_minutes or 0), num_std=num_std)
    if bands is None:
        return []
    vwap, upper_band, lower_band = bands

    price = float(current_price or 0.0)
    if price <= 0:
        try:
            price = float(m5_df["close"].iloc[-1])
        except Exception:  # noqa: BLE001 — malformed frame ⇒ no VWAP zone
            return []
    if price <= 0:
        return []

    # Proximity: explicit pip distance when configured, else a volatility-scaled
    # fraction of the band's own deviation unit (no pip_size needed).
    prox_pips = float(_resolve_vwap_param(profile, cfg, "vwap_zone_proximity_pips", 0.0))
    if prox_pips > 0 and pip_size and pip_size > 0:
        proximity = prox_pips * float(pip_size)
    else:
        std = (upper_band - vwap) / num_std if num_std > 0 else (upper_band - vwap)
        proximity = abs(std) * _VWAP_PROXIMITY_STD_FRACTION
    if proximity <= 0:
        return []

    conviction_base = int(_resolve_vwap_param(profile, cfg, "vwap_zone_conviction", 65))
    expiry_s = float(_resolve_vwap_param(profile, cfg, "vwap_zone_expiry_seconds", 900.0))
    tf = "M5"  # session VWAP is an intraday, M5-anchored construct

    if bias == "LONG" and abs(price - lower_band) <= proximity:
        top = lower_band + proximity
        bottom = lower_band - proximity
        return [EntryZone(
            symbol=model.symbol,
            direction="LONG",
            zone_type=ZoneType.VWAP_BAND,
            top=top,
            bottom=bottom,
            midpoint=lower_band,
            invalidation_level=_invalidation("LONG", bottom, top),
            conviction=conv(conviction_base, "LONG", ZoneType.VWAP_BAND, tf),
            created_at=now,
            expires_at=now + timedelta(seconds=expiry_s),
            timeframe=tf,
            has_sweep=False,
            is_counter_trend=False,
            bias_direction=bias,
        )]

    if bias == "SHORT" and abs(price - upper_band) <= proximity:
        top = upper_band + proximity
        bottom = upper_band - proximity
        return [EntryZone(
            symbol=model.symbol,
            direction="SHORT",
            zone_type=ZoneType.VWAP_BAND,
            top=top,
            bottom=bottom,
            midpoint=upper_band,
            invalidation_level=_invalidation("SHORT", bottom, top),
            conviction=conv(conviction_base, "SHORT", ZoneType.VWAP_BAND, tf),
            created_at=now,
            expires_at=now + timedelta(seconds=expiry_s),
            timeframe=tf,
            has_sweep=False,
            is_counter_trend=False,
            bias_direction=bias,
        )]

    return []


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
    *,
    m5_df: Optional[Any] = None,
    session_open_minutes: int = 0,
    current_price: float = 0.0,
    profile: Optional[Any] = None,
    pip_size: float = 0.0,
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

    ``m5_df`` / ``session_open_minutes`` / ``current_price`` / ``profile`` /
    ``pip_size`` power the optional VWAP-as-zone layer: when the
    ``vwap_zone_enabled`` flag is set (per-instrument ``profile`` first, else
    the ``EntryConfig`` global) and an M5 DataFrame is supplied, session VWAP ±
    deviation bands become entry zones alongside the FVG/OB zones.  When no M5
    frame is passed (the legacy call shape) no VWAP zones are emitted and
    behaviour is unchanged.
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

    # ── VWAP-as-zone ──────────────────────────────────────────────────────
    # When ``vwap_zone_enabled`` is set (per-instrument ``profile`` first, else
    # the ``EntryConfig`` global) and an M5 frame is supplied, session VWAP ±
    # deviation bands (brain/session_vwap.py) become entry zones: a touch of the
    # lower band in an uptrend → LONG (buy the dip to VWAP support), a touch of
    # the upper band in a downtrend → SHORT (sell the rally to VWAP resistance),
    # with the band edge as the invalidation reference. These VWAP zones are
    # appended to the same list as FVG/OB zones so they flow through the same
    # tick-entry-detector, M1 confirmation, gate, and sizing pipeline.
    if _resolve_vwap_param(profile, cfg, "vwap_zone_enabled", False) and m5_df is not None:
        try:
            zones.extend(
                _build_vwap_zones(
                    model=model,
                    cfg=cfg,
                    profile=profile,
                    m5_df=m5_df,
                    session_open_minutes=session_open_minutes,
                    current_price=current_price,
                    pip_size=pip_size,
                    now=now,
                    conv=_conv,
                )
            )
        except Exception as exc:  # noqa: BLE001 — VWAP must never break analysis
            logger.debug(
                "[zone-extract] {} VWAP zone build failed: {}", model.symbol, exc
            )

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
        # Observers notified (with the symbol) after each zone-set update, so
        # downstream consumers (e.g. the Phase 2 ZoneOrderStager) can react to
        # zones appearing / expiring / being invalidated. Best-effort dispatch.
        self._update_callbacks: list[Callable[[str], None]] = []

    def register_update_callback(self, callback: Callable[[str], None]) -> None:
        """Register a ``callback(symbol)`` fired after each zone-set update."""
        self._update_callbacks.append(callback)

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

        # Notify observers (e.g. the ZoneOrderStager) that this symbol's zone
        # set changed — fired after the lock is released so callbacks may safely
        # call back into get_active_zones. Best-effort — a faulty observer never
        # breaks the WorldModel update path.
        for cb in list(self._update_callbacks):
            try:
                cb(symbol)
            except Exception:  # noqa: BLE001
                logger.debug("[zone-watcher] update callback failed for {}", symbol)

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

