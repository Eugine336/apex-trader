"""APEX TRADER — Zone Edge Tracker.

Makes the WorldModel's zone *conviction* data-driven instead of fixed
literals.  It learns a bounded conviction multiplier per ``(symbol,
direction)`` from realized trade outcomes, so the analysis combination
adapts to what actually works in each market rather than applying a static
ICT confluence score.

Design goals:
  * **Default-neutral** — until at least ``min_samples`` outcomes accrue for a
    ``(symbol, direction)``, the weight is exactly ``1.0`` so live behaviour is
    unchanged.  This makes rollout safe.
  * **Bounded** — the multiplier is clamped to ``[weight_floor, weight_ceil]``
    so a hot/cold streak can never blow up or zero out conviction.
  * **Recency-aware** — counts are decayed once a ``(symbol, direction)``
    exceeds ``window`` samples, so old regimes fade.
  * **Stdlib-only + thread-safe** — read from the analysis thread pool, written
    from the close path; guarded by a lock and persisted atomically to JSON.

This is the first step toward a data-driven (rather than hardcoded) analysis:
the realized-outcome feedback the system already collects now flows back into
how strongly each market's setups are scored.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from typing import Any

from loguru import logger


def _norm_direction(direction: str) -> str:
    return "LONG" if str(direction).upper() in ("BUY", "LONG") else "SHORT"


class ZoneEdgeTracker:
    """Learned, bounded conviction multiplier per ``(symbol, direction)``."""

    def __init__(
        self,
        db_path: str = "data/zone_edge.json",
        *,
        min_samples: int = 20,
        window: int = 300,
        baseline_win_rate: float = 0.5,
        sensitivity: float = 0.8,
        weight_floor: float = 0.6,
        weight_ceil: float = 1.4,
    ) -> None:
        self._path = db_path
        self._min_samples = max(1, int(min_samples))
        self._window = max(self._min_samples, int(window))
        self._baseline = float(baseline_win_rate)
        self._k = float(sensitivity)
        self._floor = float(weight_floor)
        self._ceil = float(weight_ceil)
        self._lock = threading.Lock()
        # key "SYMBOL|DIRECTION" -> {"wins": float, "losses": float}
        self._stats: dict[str, dict[str, float]] = {}
        self._load()

    # ── Public API ───────────────────────────────────────────────────

    def weight(self, symbol: str, direction: str) -> float:
        """Bounded conviction multiplier for ``(symbol, direction)``.

        Returns ``1.0`` (neutral) until ``min_samples`` outcomes have accrued.
        """
        key = self._key(symbol, direction)
        with self._lock:
            rec = self._stats.get(key)
            if rec is None:
                return 1.0
            wins = rec.get("wins", 0.0)
            losses = rec.get("losses", 0.0)
        total = wins + losses
        if total < self._min_samples:
            return 1.0
        win_rate = wins / total if total > 0 else self._baseline
        raw = 1.0 + self._k * (win_rate - self._baseline)
        return max(self._floor, min(self._ceil, raw))

    def record(self, symbol: str, direction: str, won: bool) -> None:
        """Record one realized outcome and persist (best-effort)."""
        if not symbol:
            return
        key = self._key(symbol, direction)
        with self._lock:
            rec = self._stats.setdefault(key, {"wins": 0.0, "losses": 0.0})
            if won:
                rec["wins"] += 1.0
            else:
                rec["losses"] += 1.0
            # Recency decay — fade old regimes once the window is exceeded.
            if rec["wins"] + rec["losses"] > self._window:
                rec["wins"] *= 0.5
                rec["losses"] *= 0.5
            self._persist_locked()

    def snapshot(self) -> dict[str, Any]:
        """Observability — current per-(symbol,direction) edge + weight."""
        out: dict[str, Any] = {}
        with self._lock:
            items = list(self._stats.items())
        for key, rec in items:
            wins = rec.get("wins", 0.0)
            losses = rec.get("losses", 0.0)
            total = wins + losses
            sym, _, direction = key.partition("|")
            out[key] = {
                "symbol": sym,
                "direction": direction,
                "wins": round(wins, 2),
                "losses": round(losses, 2),
                "samples": round(total, 2),
                "win_rate": round(wins / total, 4) if total > 0 else 0.0,
                "weight": self.weight(sym, direction),
            }
        return out

    # ── Internal ─────────────────────────────────────────────────────

    @staticmethod
    def _key(symbol: str, direction: str) -> str:
        return f"{str(symbol).upper()}|{_norm_direction(direction)}"

    def _load(self) -> None:
        try:
            if os.path.exists(self._path):
                with open(self._path, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
                if isinstance(data, dict):
                    self._stats = {
                        str(k): {
                            "wins": float(v.get("wins", 0.0)),
                            "losses": float(v.get("losses", 0.0)),
                        }
                        for k, v in data.items()
                        if isinstance(v, dict)
                    }
        except Exception as exc:  # noqa: BLE001
            logger.debug("[zone-edge] load failed: {}", exc)

    def _persist_locked(self) -> None:
        """Atomic JSON write.  Caller must hold ``self._lock``."""
        try:
            directory = os.path.dirname(self._path) or "."
            os.makedirs(directory, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=directory, suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    json.dump(self._stats, fh)
                os.replace(tmp, self._path)
            finally:
                if os.path.exists(tmp):
                    try:
                        os.remove(tmp)
                    except OSError:
                        pass
        except Exception as exc:  # noqa: BLE001
            logger.debug("[zone-edge] persist failed: {}", exc)
