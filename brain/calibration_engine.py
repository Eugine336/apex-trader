"""APEX TRADER — CalibrationEngine.

The single WRITER of per-instrument statistics.  Every other module READS the
calibrated view through ``brain.instrument_profile.get_profile`` (which this
engine backs via ``set_stats_provider``) or by reading an ``InstrumentStats``
snapshot from the store — no module computes its own instrument characteristics.

Responsibilities
----------------
* Own the :class:`~brain.instrument_stats.InstrumentStatsStore`.
* Ingest live/backtest data: ``update_candles`` (ATR + session/hour discovery),
  ``update_spread`` (spread median/p95), ``record_news_impact`` (learned news
  sensitivity), ``record_zone_outcome`` / ``record_structure_outcome``.
* Serve as the ``symbol -> InstrumentStats`` provider for ``get_profile`` so the
  calibrated, ATR-normalised geometry is used once a symbol warms up.
* Persist learned history to JSON on shutdown and reload it on startup, so the
  calibration is not lost across restarts.

All writes are serialised under one lock; reads on ``InstrumentStats`` are plain
attribute access (cheap, and a momentary partial read of a heuristic is
harmless).
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from typing import Any, Optional

from loguru import logger

from brain.instrument_stats import InstrumentStats, InstrumentStatsStore


class CalibrationEngine:
    """Single-writer owner of rolling instrument statistics + persistence."""

    def __init__(
        self,
        store: Optional[InstrumentStatsStore] = None,
        *,
        state_path: str = "data/calibration_state.json",
    ) -> None:
        self.store = store or InstrumentStatsStore()
        self.state_path = state_path
        self._lock = threading.RLock()

    # ── Provider hook (read side for get_profile) ─────────────────────────

    def __call__(self, symbol: str) -> Optional[InstrumentStats]:
        """``symbol -> InstrumentStats | None`` so the engine IS the provider."""
        return self.store.get(symbol)

    # ── Ingest (write side — the ONLY writer) ─────────────────────────────

    def update_candles(self, symbol: str, tf: str, df: Any, pip_size: float = 0.0001) -> None:
        """Fold a freshly-closed candle frame for ``symbol``/``tf`` into ATR + hour stats."""
        try:
            with self._lock:
                self.store.get_or_create(symbol, pip_size).update_candles(tf, df)
        except Exception as exc:  # noqa: BLE001 — calibration never breaks the feed
            logger.debug("[calibration] update_candles failed for {} {}: {}", symbol, tf, exc)

    def update_spread(self, symbol: str, spread_pips: float, pip_size: float = 0.0001) -> None:
        """Record a live spread sample (pips) for ``symbol``."""
        try:
            with self._lock:
                self.store.get_or_create(symbol, pip_size).update_spread(spread_pips)
        except Exception as exc:  # noqa: BLE001
            logger.debug("[calibration] update_spread failed for {}: {}", symbol, exc)

    def record_news_impact(
        self, symbol: str, currency: str, move_pips: float, atr_pips: float,
    ) -> None:
        """Record this symbol's measured reaction to a currency's news event."""
        try:
            with self._lock:
                self.store.get_or_create(symbol).record_news_impact(currency, move_pips, atr_pips)
        except Exception as exc:  # noqa: BLE001
            logger.debug("[calibration] record_news_impact failed for {}: {}", symbol, exc)

    def record_zone_outcome(
        self, symbol: str, *, touched: bool, held: Optional[bool] = None,
    ) -> None:
        """Record whether a zone was reached (touched) and respected (held)."""
        try:
            with self._lock:
                self.store.get_or_create(symbol).record_zone_outcome(touched=touched, held=held)
        except Exception as exc:  # noqa: BLE001
            logger.debug("[calibration] record_zone_outcome failed for {}: {}", symbol, exc)

    def record_structure_outcome(self, symbol: str, held: bool) -> None:
        """Record whether a BOS/CHOCH structure event subsequently held."""
        try:
            with self._lock:
                self.store.get_or_create(symbol).record_structure_outcome(held)
        except Exception as exc:  # noqa: BLE001
            logger.debug("[calibration] record_structure_outcome failed for {}: {}", symbol, exc)

    # ── Persistence ───────────────────────────────────────────────────────

    def save(self, path: Optional[str] = None) -> bool:
        """Atomically write all learned stats to JSON. Returns True on success."""
        target = path or self.state_path
        try:
            with self._lock:
                blob = self.store.to_dict()
            directory = os.path.dirname(target) or "."
            os.makedirs(directory, exist_ok=True)
            fd, tmp = tempfile.mkstemp(prefix=".calib_", dir=directory)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    json.dump(blob, fh)
                os.replace(tmp, target)
            finally:
                if os.path.exists(tmp):
                    os.unlink(tmp)
            logger.info("[calibration] saved {} symbol(s) → {}", len(blob), target)
            return True
        except Exception as exc:  # noqa: BLE001 — never let a save failure crash shutdown
            logger.warning("[calibration] save failed ({}): {}", target, exc)
            return False

    def load(self, path: Optional[str] = None) -> int:
        """Reload learned stats from JSON. Returns the number of symbols loaded."""
        target = path or self.state_path
        if not os.path.exists(target):
            return 0
        try:
            with open(target, encoding="utf-8") as fh:
                blob = json.load(fh)
            loaded = self.store.load_dict(blob)
            logger.info("[calibration] loaded {} symbol(s) from {}", loaded, target)
            return loaded
        except Exception as exc:  # noqa: BLE001
            logger.warning("[calibration] load failed ({}): {}", target, exc)
            return 0
