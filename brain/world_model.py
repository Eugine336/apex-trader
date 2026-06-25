"""APEX TRADER — WorldModel & WorldModelStore (Phase 1).

The WorldModel is a per-symbol, frozen snapshot of every brain module's
analysis output, tagged by timeframe.  It is the single source of truth
for the execution plane (Phase 4) and the entry plane (Phase 7).

The WorldModelStore is a thread-safe publish/read container modelled on
platforms/candle_cache.py.  Writers (the analysis plane) publish atomically;
readers (execution plane, entry plane, dashboard) get frozen snapshots.
"""

from __future__ import annotations

import math
import threading
import time as _time
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Optional

from loguru import logger

from brain.fvg_detector import FairValueGap
from brain.order_block import OrderBlock
from brain.structure_engine import StructureAnalysis
from brain.liquidity_mapper import LiquidityMap
from brain.volume_analyzer import VolumeAnalysis
from brain.wyckoff_engine import WyckoffAnalysis
from brain.inducement_detector import InducementAnalysis

if TYPE_CHECKING:
    from entry.models import EntryZone


@dataclass(frozen=True)
class WorldModel:
    """Frozen per-symbol snapshot of all brain module outputs.

    Every analysis result is keyed by timeframe string (``"M5"``, ``"H1"``,
    ``"H4"``, ``"D1"``).  Consumers read whichever timeframe(s) they need.
    Immutable after creation so concurrent readers never see partial state.
    """

    symbol: str
    version: int
    timestamp: datetime

    # ── Multi-TF analysis results ─────────────────────────────────────
    # Each is a tuple of (timeframe, result) pairs so the frozen dataclass
    # stays hashable.  Helpers below provide dict-style access.

    fvgs: tuple[tuple[str, tuple[FairValueGap, ...]], ...] = ()
    order_blocks: tuple[tuple[str, tuple[OrderBlock, ...]], ...] = ()
    structure: tuple[tuple[str, StructureAnalysis], ...] = ()
    liquidity: tuple[tuple[str, LiquidityMap], ...] = ()
    volume: tuple[tuple[str, VolumeAnalysis], ...] = ()
    wyckoff: tuple[tuple[str, WyckoffAnalysis], ...] = ()
    inducement: tuple[tuple[str, InducementAnalysis], ...] = ()

    # Combined bias from StructureEngine.get_bias(h4, h1, d1) — already
    # multi-TF by nature, stored as a frozen dict snapshot.
    bias: tuple[tuple[str, Any], ...] = ()

    # ── Synthesized entry layer ───────────────────────────────────────
    # Actionable entry zones derived from the raw analysis above (FVG/OB
    # confluence, bias-filtered).  Populated by the analysis plane at
    # publish time so the WorldModel is the single source of truth for the
    # entry plane — consumers read this rather than re-deriving zones.
    entry_zones: tuple["EntryZone", ...] = ()

    # ── Non-ICT concept layer ─────────────────────────────────────────
    # Outputs of the concept generators (trend, mean-reversion, …) keyed by
    # timeframe, plus the classified volatility regime per timeframe.  These
    # broaden the analysis beyond the ICT/SMC modules so the data-driven
    # combiner can weight multiple schools of thought.  ConceptSignal values
    # are stored as opaque objects to avoid coupling WorldModel to pandas.
    concepts: tuple[tuple[str, tuple[Any, ...]], ...] = ()
    regime: tuple[tuple[str, str], ...] = ()

    # ── Consensus layer (directional votes + ranked opportunities) ────
    # Per-module directional votes and the opportunity-ranker's clustered
    # trade ideas, synthesized at publish time from the analysis above.  They
    # power the dashboard's module-votes + ranker panels and keep the
    # WorldModel the single source of truth for them.  Stored as opaque
    # objects (Vote / Opportunity) to avoid import coupling.
    votes: tuple[Any, ...] = ()
    candidates: tuple[Any, ...] = ()

    # ── Setup-quality layer (second-order analysis) ───────────────────
    # Real Opportunity/Entry Quality scores (0–10) and the per-symbol
    # RegimeAnalysis, synthesized at publish time by
    # ``brain.quality_layer.compute_quality_layer``.  Shared by the live and
    # backtest planes so both engines read identical quality signals.  ``None``
    # means "not computed this cycle" — consumers apply no quality pressure.
    opportunity_quality: Optional[float] = None
    entry_quality_long: Optional[float] = None
    entry_quality_short: Optional[float] = None
    regime_analysis: Optional[Any] = None

    # ── Helpers ───────────────────────────────────────────────────────

    def fvgs_by_tf(self) -> dict[str, tuple[FairValueGap, ...]]:
        return dict(self.fvgs)

    def order_blocks_by_tf(self) -> dict[str, tuple[OrderBlock, ...]]:
        return dict(self.order_blocks)

    def structure_by_tf(self) -> dict[str, StructureAnalysis]:
        return dict(self.structure)

    def liquidity_by_tf(self) -> dict[str, LiquidityMap]:
        return dict(self.liquidity)

    def volume_by_tf(self) -> dict[str, VolumeAnalysis]:
        return dict(self.volume)

    def wyckoff_by_tf(self) -> dict[str, WyckoffAnalysis]:
        return dict(self.wyckoff)

    def inducement_by_tf(self) -> dict[str, InducementAnalysis]:
        return dict(self.inducement)

    def bias_dict(self) -> dict[str, Any]:
        return dict(self.bias)

    def entry_zones_list(self) -> list["EntryZone"]:
        """Actionable entry zones synthesized at publish time."""
        return list(self.entry_zones)

    def concepts_by_tf(self) -> dict[str, tuple[Any, ...]]:
        """Non-ICT concept signals keyed by timeframe."""
        return dict(self.concepts)

    def regime_by_tf(self) -> dict[str, str]:
        """Classified volatility regime keyed by timeframe."""
        return dict(self.regime)

    def votes_list(self) -> list[Any]:
        """Per-module directional votes synthesized at publish time."""
        return list(self.votes)

    def candidates_list(self) -> list[Any]:
        """Ranked opportunity candidates synthesized at publish time."""
        return list(self.candidates)

    def all_fvgs(self) -> list[FairValueGap]:
        """Flat list of all FVGs across timeframes (highest TF first)."""
        out: list[FairValueGap] = []
        for _tf, fvg_list in self.fvgs:
            out.extend(fvg_list)
        return out

    def all_order_blocks(self) -> list[OrderBlock]:
        """Flat list of all OBs across timeframes (highest TF first)."""
        out: list[OrderBlock] = []
        for _tf, ob_list in self.order_blocks:
            out.extend(ob_list)
        return out

    def get_structural_targets(
        self,
        direction: str,
        current_price: float,
        *,
        min_distance: float = 0.0,
    ) -> list[float]:
        """Structural price levels ahead of price in the trade direction.

        The opportunistic-trading rewire derives take-profit targets from what
        the MARKET is actually showing — the next fair-value gap, order block,
        or liquidity pool in front of price — instead of a hardcoded
        reward-multiple. Returns candidate target prices sorted nearest-first.
        An empty list means the brain sees no structure ahead, and the entry
        layer falls back to a volatility/risk-multiple target.

        ``direction`` is ``"LONG"`` (targets above price) or ``"SHORT"`` (below).
        Levels within ``min_distance`` of ``current_price`` are skipped so a
        target is never effectively at the entry.
        """
        d = str(direction or "").upper()
        if d not in ("LONG", "SHORT") or current_price is None or current_price <= 0:
            return []
        is_long = d == "LONG"
        levels: list[float] = []

        def _consider(price: Any) -> None:
            try:
                p = float(price)
            except (TypeError, ValueError):
                return
            if not math.isfinite(p) or p <= 0:
                return
            if is_long and p > current_price + min_distance:
                levels.append(p)
            elif (not is_long) and p < current_price - min_distance:
                levels.append(p)

        # FVGs — the midpoint is a fair proxy for where price fills the gap.
        try:
            for fvg in self.all_fvgs():
                _consider(getattr(fvg, "midpoint", None))
        except Exception:  # never let a malformed object break entry geometry
            pass
        # Order blocks — the near edge price reaches first becomes the target.
        try:
            for ob in self.all_order_blocks():
                edge = getattr(ob, "bottom", None) if is_long else getattr(ob, "top", None)
                _consider(edge)
        except Exception:
            pass
        # Liquidity pools — price hunts buy-side liquidity above / sell-side below.
        try:
            for _tf, lq in self.liquidity:
                pools = (
                    getattr(lq, "buy_side_liquidity", None)
                    if is_long
                    else getattr(lq, "sell_side_liquidity", None)
                )
                for zone in (pools or []):
                    _consider(getattr(zone, "price", None))
        except Exception:
            pass

        # Nearest-first, de-duplicated.
        return sorted(set(levels), key=lambda p: abs(p - current_price))


class WorldModelStore:
    """Thread-safe publish/read store for WorldModel snapshots.

    Modelled on ``platforms/candle_cache.py``: a Lock-protected dict that
    supports atomic publish (one writer at a time) and snapshot reads
    (readers get a frozen copy, never a mutable reference).

    Writers call ``publish()`` with a new WorldModel; readers call
    ``get()`` for a single symbol or ``snapshot()`` for the full dict.
    """

    def __init__(self) -> None:
        self._store: dict[str, WorldModel] = {}
        self._lock = threading.RLock()
        self._version_counter: int = 0

    def next_version(self) -> int:
        """Return the next monotonically increasing version number."""
        with self._lock:
            self._version_counter += 1
            return self._version_counter

    def publish(self, model: WorldModel) -> None:
        """Atomically publish a WorldModel for ``model.symbol``.

        Only replaces the stored model if the new version is >= the current.
        """
        with self._lock:
            existing = self._store.get(model.symbol)
            if existing is not None and model.version < existing.version:
                logger.debug(
                    "[world-model] skip stale publish for {} v{} < v{}",
                    model.symbol, model.version, existing.version,
                )
                return
            self._store[model.symbol] = model

    def get(self, symbol: str) -> Optional[WorldModel]:
        """Return the current WorldModel for ``symbol``, or None."""
        with self._lock:
            return self._store.get(symbol)

    def snapshot(self) -> dict[str, WorldModel]:
        """Return a shallow copy of the full store (frozen values)."""
        with self._lock:
            return dict(self._store)

    def symbols(self) -> list[str]:
        """Return list of symbols with published models."""
        with self._lock:
            return list(self._store.keys())

    def clear(self) -> None:
        """Remove all stored models."""
        with self._lock:
            self._store.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._store)


def build_world_model(
    *,
    symbol: str,
    version: int,
    timestamp: Optional[datetime] = None,
    fvgs: Optional[dict[str, list[FairValueGap]]] = None,
    order_blocks: Optional[dict[str, list[OrderBlock]]] = None,
    structure: Optional[dict[str, StructureAnalysis]] = None,
    liquidity: Optional[dict[str, LiquidityMap]] = None,
    volume: Optional[dict[str, VolumeAnalysis]] = None,
    wyckoff: Optional[dict[str, WyckoffAnalysis]] = None,
    inducement: Optional[dict[str, InducementAnalysis]] = None,
    bias: Optional[dict[str, Any]] = None,
    entry_zones: Optional[list["EntryZone"]] = None,
    concepts: Optional[dict[str, list]] = None,
    regime: Optional[dict[str, str]] = None,
    votes: Optional[list] = None,
    candidates: Optional[list] = None,
) -> WorldModel:
    """Convenience builder: accepts mutable dicts, freezes them into tuples.

    Timeframe ordering is preserved as-given; callers should pass highest-TF
    first (H4, H1, M15, M5) for consistent iteration order.
    """
    ts = timestamp or datetime.now(timezone.utc)

    def _freeze_lists(
        d: Optional[dict[str, list]],
    ) -> tuple[tuple[str, tuple], ...]:
        if not d:
            return ()
        return tuple((tf, tuple(items)) for tf, items in d.items())

    def _freeze_lists_copy(
        d: Optional[dict[str, list]],
    ) -> tuple[tuple[str, tuple], ...]:
        """Like ``_freeze_lists`` but deep-copies each element into the snapshot.

        The frozen dataclass only freezes the *containers* (lists → tuples); the
        contained ``OrderBlock`` / ``FairValueGap`` objects are mutable and their
        ``.status`` is mutated in place by the detectors. Copying them at publish
        time isolates the published snapshot so a producer re-mutating its own
        objects can never retroactively change what concurrent readers see.
        """
        if not d:
            return ()
        return tuple(
            (tf, tuple(deepcopy(item) for item in items)) for tf, items in d.items()
        )

    def _freeze_scalars(
        d: Optional[dict[str, Any]],
    ) -> tuple[tuple[str, Any], ...]:
        if not d:
            return ()
        return tuple((k, v) for k, v in d.items())

    return WorldModel(
        symbol=symbol,
        version=version,
        timestamp=ts,
        fvgs=_freeze_lists_copy(fvgs),
        order_blocks=_freeze_lists_copy(order_blocks),
        structure=_freeze_scalars(structure),
        liquidity=_freeze_scalars(liquidity),
        volume=_freeze_scalars(volume),
        wyckoff=_freeze_scalars(wyckoff),
        inducement=_freeze_scalars(inducement),
        bias=_freeze_scalars(bias),
        entry_zones=tuple(entry_zones) if entry_zones else (),
        concepts=_freeze_lists(concepts),
        regime=_freeze_scalars(regime),
        votes=tuple(votes) if votes else (),
        candidates=tuple(candidates) if candidates else (),
    )
