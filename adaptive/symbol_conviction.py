"""
APEX TRADER — Symbol-Relative Conviction Store (Learning ⑦)

The Consensus Division turns a vote panel into a ``ConsensusThesis`` whose
``conviction`` ∈ [0, 1] gates entry (``conviction >= conviction_threshold``) and
feeds sizing. That raw conviction is computed identically for every instrument —
so ``EURUSD conviction=0.82`` and ``XAUUSD conviction=0.82`` are treated as the
same strength even though each instrument has its own conviction distribution
(a quiet major routinely prints 0.75–0.85; a volatile metal rarely clears 0.70).
A flat threshold therefore over-trades the instruments that print high
convictions cheaply and under-trades the ones where a high conviction is genuinely
exceptional.

This store closes that gap WITHOUT touching how conviction is produced. It keeps
a bounded, per-symbol history of raw convictions and exposes
:meth:`normalize` — the raw value re-expressed *relative to that symbol's own
distribution* (its percentile rank), then blended back toward the raw value by a
configurable factor. Downstream consumers (the trigger gate, sizing) then compare
like-with-like across instruments.

Safety / behaviour properties (this is on the live entry path):

* **Cold-start neutral.** Until a symbol has ``min_samples`` recorded
  convictions, :meth:`normalize` returns the raw value unchanged — byte-for-byte
  the legacy behaviour. A fresh install / cleared data repo therefore behaves
  exactly as before until each symbol warms up.
* **Bounded blend.** ``blend=0`` ⇒ raw passthrough (feature effectively off);
  ``blend=1`` ⇒ fully symbol-relative (percentile). The default is a partial
  blend so the absolute signal is never fully discarded.
* **Per-symbol isolation.** Each symbol owns an independent history — one
  instrument's convictions never shift another's normalization.
* **Per-user isolated persistence.** State is JSON under
  :func:`runtime_paths.data_dir` (honours ``APEX_DATA_DIR`` in multi-tenant
  mode), so users never share or overwrite each other's conviction history.

Leaf module — standard library + loguru + runtime_paths only.
"""

from __future__ import annotations

import bisect
import json
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional

from loguru import logger

from runtime_paths import data_dir as _data_dir


class SymbolConvictionStore:
    """Accumulates per-symbol conviction history and normalizes relative to it.

    Thread-safe. Reads/writes are guarded by a single lock; the work per call is
    a small append + a binary search, so the cost on the (per-candle, not
    per-tick) consensus path is negligible.
    """

    def __init__(
        self,
        *,
        enabled: bool = True,
        min_samples: int = 30,
        max_history: int = 300,
        blend: float = 0.5,
        persist: bool = True,
        path: Optional[str | Path] = None,
        save_interval_seconds: float = 60.0,
    ) -> None:
        self._enabled = bool(enabled)
        self._min_samples = max(1, int(min_samples))
        self._max_history = max(self._min_samples, int(max_history))
        self._blend = min(max(float(blend), 0.0), 1.0)
        self._persist = bool(persist)
        self._path = (
            Path(path) if path is not None else (_data_dir() / "symbol_conviction.json")
        )
        self._save_interval = max(0.0, float(save_interval_seconds))
        self._last_save = 0.0
        self._lock = threading.Lock()
        # symbol -> chronological list of raw convictions (most-recent appended).
        self._history: Dict[str, List[float]] = {}
        self._dirty = False
        if self._persist:
            self._load()

    # ── Read API ──────────────────────────────────────────────────────────

    @property
    def enabled(self) -> bool:
        return self._enabled

    def sample_count(self, symbol: str) -> int:
        with self._lock:
            return len(self._history.get(symbol, ()))

    def normalize(self, symbol: str, raw_conviction: float) -> float:
        """Return ``raw_conviction`` re-expressed relative to ``symbol``'s history.

        Cold-start safe: returns the raw value unchanged while the feature is
        off or the symbol has fewer than ``min_samples`` recorded convictions.
        Never raises — any fault returns the raw value.
        """
        try:
            raw = float(raw_conviction)
        except (TypeError, ValueError):
            return raw_conviction
        if not self._enabled or self._blend <= 0.0 or not symbol:
            return raw
        with self._lock:
            hist = self._history.get(symbol)
            if hist is None or len(hist) < self._min_samples:
                return raw
            ordered = sorted(hist)
        # Percentile rank of ``raw`` within the symbol's own distribution: the
        # fraction of historical convictions at or below it, in [0, 1].
        n = len(ordered)
        rank = bisect.bisect_right(ordered, raw) / n
        normalized = (1.0 - self._blend) * raw + self._blend * rank
        return float(min(1.0, max(0.0, normalized)))

    # ── Write API ─────────────────────────────────────────────────────────

    def record(self, symbol: str, raw_conviction: float) -> None:
        """Append a raw conviction observation for ``symbol`` (bounded history)."""
        if not self._enabled or not symbol:
            return
        try:
            raw = float(raw_conviction)
        except (TypeError, ValueError):
            return
        if not (0.0 <= raw <= 1.0):
            raw = min(1.0, max(0.0, raw))
        with self._lock:
            hist = self._history.setdefault(symbol, [])
            hist.append(raw)
            if len(hist) > self._max_history:
                # Drop the oldest so the distribution tracks recent regime.
                del hist[: len(hist) - self._max_history]
            self._dirty = True

    def record_and_normalize(self, symbol: str, raw_conviction: float) -> float:
        """Record the observation, then return its symbol-relative value.

        The single call the consensus path uses: it both grows the per-symbol
        distribution and returns the normalized conviction in one step. Cold-start
        neutral (returns raw until the symbol clears ``min_samples``).
        """
        self.record(symbol, raw_conviction)
        normalized = self.normalize(symbol, raw_conviction)
        self._maybe_autosave()
        return normalized

    def _maybe_autosave(self) -> None:
        """Persist on a time throttle so per-candle records do not hammer disk
        yet state survives a crash without explicit shutdown wiring."""
        if not self._persist or self._save_interval <= 0:
            return
        now = time.time()
        if now - self._last_save < self._save_interval:
            return
        self._last_save = now
        self.save()

    # ── Persistence (atomic, best-effort) ─────────────────────────────────

    def save(self) -> None:
        if not self._persist:
            return
        with self._lock:
            if not self._dirty:
                return
            payload = {
                "version": 1,
                "saved_at": time.time(),
                "min_samples": self._min_samples,
                "max_history": self._max_history,
                "history": {s: list(h) for s, h in self._history.items()},
            }
            self._dirty = False
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            try:
                from persistence.atomic_write import atomic_write_text

                atomic_write_text(self._path, json.dumps(payload))
            except Exception:
                self._path.write_text(json.dumps(payload), encoding="utf-8")
        except Exception as exc:  # noqa: BLE001
            logger.warning("[symbol-conviction] save failed: {}", exc)

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            logger.warning("[symbol-conviction] load failed — starting empty: {}", exc)
            return
        hist = data.get("history") if isinstance(data, dict) else None
        if not isinstance(hist, dict):
            return
        loaded: Dict[str, List[float]] = {}
        for sym, vals in hist.items():
            if not isinstance(vals, list):
                continue
            clean = [float(v) for v in vals if isinstance(v, (int, float))]
            if clean:
                loaded[str(sym)] = clean[-self._max_history :]
        with self._lock:
            self._history = loaded

    # ── Dashboard / observability ─────────────────────────────────────────

    def get_status(self) -> dict:
        """Per-symbol sample counts + summary stats for the dashboard / ops."""
        with self._lock:
            out: Dict[str, dict] = {}
            for sym, hist in self._history.items():
                n = len(hist)
                if n == 0:
                    continue
                mean = sum(hist) / n
                out[sym] = {
                    "samples": n,
                    "warmed": n >= self._min_samples,
                    "mean": round(mean, 4),
                    "min": round(min(hist), 4),
                    "max": round(max(hist), 4),
                }
            return {
                "enabled": self._enabled,
                "min_samples": self._min_samples,
                "blend": self._blend,
                "symbols": out,
            }


__all__ = ["SymbolConvictionStore"]
