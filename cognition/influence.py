"""APEX TRADER — Adaptive influence + Brain calibration (Constitution Part VIII).

Part VIII: evidence-source influence and the Brain's own reasoning quality are
graded by *demonstrated decision quality*, and adaptation is deliberate — driven
by statistically-meaningful samples, robustness over recent profit, never a
knee-jerk on one outcome.

Two pure, fail-safe learners live here (stdlib only, natively importable):

* :class:`InfluenceLedger` — per ``Evidence.source_module`` win/loss tally that
  earns or loses a bounded *influence weight* around the neutral 1.0. A source
  earns influence only once it has enough samples to be significant; the weight
  is a function of its full-sample win rate (not the last few trades), so a lucky
  streak cannot inflate a source. The consolidator multiplies each source's
  contribution to :meth:`MarketState.consolidation` by this weight.

* :class:`CalibrationTracker` — records (predicted-confidence, realised-outcome)
  pairs and reports how well the Brain's stated confidence matches reality
  (reliability gap + Brier score). This surfaces *reasoning quality* so the Brain
  can be held to its own probabilities.

Both are observational by default: they always *learn*, but whether their output
is *applied* (influence weighting the live consolidation) is gated by the caller
(default shadow), mirroring the constitution's shadow → authoritative rollout.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Optional

logger = logging.getLogger("apex.cognition.influence")


def _clampf(v: Any, lo: float, hi: float, default: float) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    if f != f or f in (float("inf"), float("-inf")):
        return default
    return min(hi, max(lo, f))


class InfluenceLedger:
    """Per-source influence weight learned from realised campaign outcomes.

    ``weight_for(source)`` returns the neutral ``1.0`` until a source has at
    least ``min_samples`` outcomes (statistical-significance floor); thereafter a
    bounded weight ``clamp(1 + gain·(win_rate − 0.5), min_weight, max_weight)``.
    Thread-safe and fail-safe.
    """

    def __init__(
        self,
        *,
        min_samples: int = 20,
        min_weight: float = 0.5,
        max_weight: float = 1.5,
        gain: float = 1.0,
    ) -> None:
        self.min_samples = max(1, int(min_samples))
        self.min_weight = _clampf(min_weight, 0.0, 1.0, 0.5)
        self.max_weight = max(1.0, _clampf(max_weight, 1.0, 100.0, 1.5))
        self.gain = max(0.0, float(gain))
        self._wins: dict = {}
        self._losses: dict = {}
        # Article XIX — running per-source reasoning-quality average (separate
        # from win/loss). Observational: surfaces how DEEP each source's
        # reasoning tends to be, independent of whether it won.
        self._quality_sum: dict = {}
        self._quality_count: dict = {}
        self._lock = threading.Lock()

    def observe(self, source_module: str, won: bool) -> None:
        """Record one realised outcome for an evidence source. Fail-safe."""
        try:
            src = str(source_module or "").strip()
            if not src:
                return
            with self._lock:
                if won:
                    self._wins[src] = self._wins.get(src, 0) + 1
                else:
                    self._losses[src] = self._losses.get(src, 0) + 1
        except Exception as exc:  # noqa: BLE001 — learning must never raise
            logger.debug("[influence] observe fault: %s", exc)

    def observe_many(self, source_modules: Any, won: bool) -> None:
        """Record the same outcome for a collection of sources."""
        for s in list(source_modules or []):
            self.observe(s, won)

    def observe_quality(self, source: str, quality_score: float) -> None:
        """Record one reasoning-quality observation for a source (Article XIX).

        Maintains a running average per source, separate from win/loss. Purely
        observational — it never affects ``weight_for``. Fail-safe."""
        try:
            src = str(source or "").strip()
            if not src:
                return
            q = _clampf(quality_score, 0.0, 1.0, 0.0)
            with self._lock:
                self._quality_sum[src] = self._quality_sum.get(src, 0.0) + q
                self._quality_count[src] = self._quality_count.get(src, 0) + 1
        except Exception as exc:  # noqa: BLE001 — learning must never raise
            logger.debug("[influence] observe_quality fault: %s", exc)

    def quality_for(self, source: str) -> float:
        """Running-average reasoning quality for a source (1.0 until observed)."""
        try:
            src = str(source or "").strip()
            with self._lock:
                n = self._quality_count.get(src, 0)
                if n <= 0:
                    return 1.0
                return round(self._quality_sum.get(src, 0.0) / n, 4)
        except Exception as exc:  # noqa: BLE001
            logger.debug("[influence] quality_for fault: %s", exc)
            return 1.0

    def _attempts(self, src: str) -> int:
        return self._wins.get(src, 0) + self._losses.get(src, 0)

    def weight_for(self, source_module: str) -> float:
        """Bounded influence weight for a source (1.0 until significant)."""
        try:
            src = str(source_module or "").strip()
            with self._lock:
                attempts = self._attempts(src)
                if attempts < self.min_samples:
                    return 1.0
                win_rate = self._wins.get(src, 0) / attempts
            raw = 1.0 + self.gain * (win_rate - 0.5)
            return round(min(self.max_weight, max(self.min_weight, raw)), 4)
        except Exception as exc:  # noqa: BLE001
            logger.debug("[influence] weight_for fault: %s", exc)
            return 1.0

    def weights(self) -> dict:
        """All sources' current weights (only those past the significance floor)."""
        with self._lock:
            attempts = {s: self._wins.get(s, 0) + self._losses.get(s, 0)
                        for s in (set(self._wins) | set(self._losses))}
        out: dict = {}
        for s in attempts:
            w = self.weight_for(s)
            if w != 1.0 or attempts[s] >= self.min_samples:
                out[s] = w
        return out

    def get_status(self) -> dict:
        # Snapshot counts under the lock, then compute weights OUTSIDE it —
        # weight_for() takes the same (non-reentrant) lock, so calling it while
        # holding the lock would deadlock.
        with self._lock:
            wins = dict(self._wins)
            losses = dict(self._losses)
        sources = sorted(set(wins) | set(losses))
        detail = {
            s: {
                "wins": wins.get(s, 0),
                "losses": losses.get(s, 0),
                "weight": self.weight_for(s),
            }
            for s in sources[:50]
        }
        return {
            "min_samples": self.min_samples,
            "min_weight": self.min_weight,
            "max_weight": self.max_weight,
            "tracked_sources": len(detail),
            "sources": detail,
        }


class CalibrationTracker:
    """Tracks how well the Brain's predicted confidence matches realised outcomes.

    Reliability gap = |mean predicted confidence − realised win rate| (0 is
    perfectly calibrated). Brier score = mean((confidence − outcome)²) (lower is
    better). Thread-safe, fail-safe, bounded history.
    """

    def __init__(self, *, history_limit: int = 500, min_samples: int = 30) -> None:
        self._limit = max(1, int(history_limit))
        self.min_samples = max(1, int(min_samples))
        self._samples: list = []          # list[(confidence, won)]
        self._lock = threading.Lock()

    def observe(self, confidence: Any, won: bool) -> None:
        """Record one (predicted confidence, realised outcome) pair. Fail-safe."""
        try:
            c = _clampf(confidence, 0.0, 1.0, 0.0)
            with self._lock:
                self._samples.append((c, 1.0 if won else 0.0))
                if len(self._samples) > self._limit:
                    self._samples = self._samples[-self._limit:]
        except Exception as exc:  # noqa: BLE001
            logger.debug("[calibration] observe fault: %s", exc)

    def metrics(self) -> dict:
        with self._lock:
            samples = list(self._samples)
        n = len(samples)
        if n == 0:
            return {"samples": 0, "mean_confidence": 0.0, "win_rate": 0.0,
                    "reliability_gap": 0.0, "brier": 0.0}
        mean_conf = sum(c for c, _ in samples) / n
        win_rate = sum(o for _, o in samples) / n
        brier = sum((c - o) ** 2 for c, o in samples) / n
        return {
            "samples": n,
            "mean_confidence": round(mean_conf, 4),
            "win_rate": round(win_rate, 4),
            "reliability_gap": round(abs(mean_conf - win_rate), 4),
            "brier": round(brier, 4),
        }

    def calibration_adjustment(self) -> float:
        """Return a multiplicative correction factor for the Brain's confidence.

        If the Brain consistently over-predicts (states 0.70 confidence but
        only wins 50% of the time), the factor is < 1.0 to attenuate.
        If the Brain under-predicts, the factor is > 1.0 to amplify.
        Bounded [0.7, 1.3] and requires ``min_samples`` before activating.
        Returns 1.0 (neutral) when insufficient data. Never raises.
        """
        try:
            with self._lock:
                samples = list(self._samples)
            n = len(samples)
            if n < self.min_samples:
                return 1.0
            mean_conf = sum(c for c, _ in samples) / n
            win_rate = sum(o for _, o in samples) / n
            if mean_conf > 0 and win_rate > 0:
                raw_factor = win_rate / mean_conf
                clamped = min(1.3, max(0.7, raw_factor))
                return round(clamped, 4)
            return 1.0
        except Exception as exc:  # noqa: BLE001 — calibration must never raise
            logger.debug("[calibration] adjustment fault: %s", exc)
            return 1.0

    def get_status(self) -> dict:
        return self.metrics()


__all__ = ["InfluenceLedger", "CalibrationTracker"]
