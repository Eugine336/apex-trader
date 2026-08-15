"""APEX TRADER — Adaptive Evidence-Weight Provider (Phase 6).

Closes the last open link in the continuous-learning loop: the per-timeframe
evidence weights that drive the probabilistic bias model
(:data:`brain.decision_core._EVIDENCE_WEIGHTS`) were a *static* dict that no
learning code ever touched.  This provider tracks, per timeframe, how *useful*
that timeframe's observations were as context for opportunity quality, and nudges
the weights toward the timeframes whose observations actually help — within hard,
reversible safety bounds.

Constitutional note (§XXIX): a timeframe is credited whenever it produced a
*definite observation* (a confident structural read) and the opportunity it
contextualised turned out well — never for having a trend that pointed the same
way as the taken trade.  Direction is irrelevant; observation usefulness is the
learning target.  A timeframe that was ranging / silent contributes no
observation and is not scored that cycle.

Design properties
-----------------
* **Bounded.**  Every weight stays in ``[min_weight, max_weight]``, the full
  vector always renormalises to sum 1.0, and no single weight may move more
  than ``max_shift_per_cycle`` per recompute.  A pathological reading can
  therefore only ever drift weights slowly and within a tight envelope.
* **Conservative cold-start.**  Until at least ``min_trades`` outcomes have
  been recorded, :meth:`get_weights` returns the static defaults untouched —
  the system behaves exactly as before while it gathers evidence.
* **Graceful degradation.**  Construction, recording, and recompute are all
  exception-safe; on any internal fault the provider falls back to the static
  defaults so the bias model never breaks.
* **Reversible.**  :meth:`reset_to_defaults` is an instant escape hatch and
  state persists to JSON so a restart resumes (or, after a reset, starts) from
  a known-good vector.

Leaf module — stdlib + loguru only.  No learning-layer imports, so
``decision_core`` can read it without a cycle.
"""

from __future__ import annotations

import json
import threading
from collections import deque
from pathlib import Path
from typing import Deque, Dict, Mapping, Optional

from loguru import logger

# Canonical static weights live in ``brain.decision_core``; import them lazily
# (that module pulls in pandas/numpy via the brain stack) so this provider stays
# a true leaf — importable, and testable, without the heavy analysis deps. The
# fallback mirrors the canonical vector exactly and is only used if the import
# is unavailable (e.g. a minimal test environment).
_FALLBACK_WEIGHTS: Dict[str, float] = {
    "D1": 0.10,
    "H4": 0.20,
    "H1": 0.25,
    "M15": 0.20,
    "M5": 0.15,
    "M1": 0.10,
}


def _default_weights() -> Dict[str, float]:
    try:
        from brain.decision_core import _EVIDENCE_WEIGHTS as _canonical
        if isinstance(_canonical, dict) and _canonical:
            return {tf: float(w) for tf, w in _canonical.items()}
    except Exception:  # noqa: BLE001 — minimal env without the brain stack
        pass
    return dict(_FALLBACK_WEIGHTS)


def _has_observation(value) -> bool:
    """True when a timeframe produced a definite structural observation.

    An observation is "present" when the timeframe reported a confident
    structural state (any non-empty read, e.g. BULLISH / BEARISH / RANGING). A
    missing / empty / unknown read means the timeframe was silent this cycle and
    contributes no observation. Direction is deliberately NOT inspected — only
    whether an observation was made (Constitution §XXIX).
    """
    if value is None:
        return False
    raw = getattr(value, "value", None)
    raw = str(raw if raw is not None else value).strip().upper()
    return raw not in ("", "NONE", "UNKNOWN", "NEUTRAL")


def _as_quality(outcome_quality) -> float:
    """Coerce an outcome-quality signal to a bounded [0.0, 1.0] float.

    Accepts a bool (won/lost) or an already-bounded score. A trade win is the
    simplest proxy for "the opportunity this observation contextualised turned
    out well"; richer callers may pass a graded quality in [0, 1].
    """
    if isinstance(outcome_quality, bool):
        return 1.0 if outcome_quality else 0.0
    try:
        return max(0.0, min(1.0, float(outcome_quality)))
    except (TypeError, ValueError):
        return 0.0


class _TFObservationUsefulness:
    """Rolling observation-usefulness bookkeeping for one timeframe.

    Each entry is the outcome quality (0.0 .. 1.0) of an opportunity that this
    timeframe produced a definite observation for. Timeframes that were silent
    (no observation) on a given trade contribute nothing — usefulness is scored
    only over the opportunities the timeframe actually observed, regardless of
    whether its observation's direction matched the taken trade.
    """

    __slots__ = ("window",)

    def __init__(self, window_size: int) -> None:
        self.window: Deque[float] = deque(maxlen=max(1, int(window_size)))

    def record(self, quality: float) -> None:
        self.window.append(_as_quality(quality))

    @property
    def samples(self) -> int:
        return len(self.window)

    def accuracy(self) -> Optional[float]:
        n = len(self.window)
        if n == 0:
            return None
        return sum(self.window) / n


class AdaptiveWeightProvider:
    """Tracks per-TF predictive accuracy and serves bounded adapted weights.

    Thread-safe.  All public methods are exception-safe and fall back to the
    static defaults on any internal error.
    """

    def __init__(
        self,
        *,
        enabled: bool = True,
        min_trades: int = 30,
        min_weight: float = 0.05,
        max_weight: float = 0.40,
        max_shift_per_cycle: float = 0.03,
        window_size: int = 200,
        adapt_gain: float = 0.5,
        state_path: Optional[str] = None,
    ) -> None:
        self.enabled = bool(enabled)
        self._min_trades = max(1, int(min_trades))
        self._min_weight = float(min_weight)
        self._max_weight = float(max_weight)
        self._max_shift = abs(float(max_shift_per_cycle))
        self._window_size = max(1, int(window_size))
        # How aggressively accuracy divergence pulls a weight (0 = never move).
        self._adapt_gain = max(0.0, float(adapt_gain))

        self._defaults: Dict[str, float] = _default_weights()
        # Live, adapted vector (starts at defaults).
        self._weights: Dict[str, float] = dict(self._defaults)
        self._acc: Dict[str, _TFObservationUsefulness] = {
            tf: _TFObservationUsefulness(self._window_size) for tf in self._defaults
        }
        self._total_trades = 0
        self._recompute_count = 0
        self._last_change: Dict[str, float] = {}

        self._lock = threading.RLock()

        self._state_path: Optional[Path] = (
            Path(state_path) if state_path else None
        )
        if self._state_path is not None:
            self._load()

    # ── Read side (consumed by decision_core.compute_bias) ───────────────

    def get_weights(self) -> Dict[str, float]:
        """Return the active per-TF weight vector.

        Falls back to the static defaults while disabled or until the minimum
        sample count is reached, so cold-start behaviour is unchanged.
        """
        try:
            with self._lock:
                if not self.enabled or self._total_trades < self._min_trades:
                    return dict(self._defaults)
                return dict(self._weights)
        except Exception as exc:  # noqa: BLE001 — never break the bias model
            logger.debug("[adaptive-weights] get_weights fell back: {}", exc)
            return dict(self._defaults)

    # ── Write side (fed from the trade-close path) ───────────────────────

    def record_observation_outcome(
        self, per_tf_observations: Mapping[str, str], outcome_quality,
    ) -> None:
        """Record one closed opportunity's per-TF observations and its quality.

        ``per_tf_observations`` maps a timeframe (e.g. ``"H1"``) to the
        structural observation it produced at entry. A timeframe is credited with
        the opportunity's ``outcome_quality`` whenever it produced a *definite
        observation* — regardless of whether that observation's direction matched
        the taken trade. Silent / ranging-less timeframes contribute nothing.
        ``outcome_quality`` is a bool (won/lost) or a bounded [0, 1] score. Never
        raises.
        """
        try:
            if not per_tf_observations:
                return
            quality = _as_quality(outcome_quality)
            with self._lock:
                self._total_trades += 1
                for tf, observation in per_tf_observations.items():
                    acc = self._acc.get(tf)
                    if acc is None:
                        continue
                    if _has_observation(observation):
                        acc.record(quality)
        except Exception as exc:  # noqa: BLE001
            logger.debug("[adaptive-weights] record fell back: {}", exc)

    # ── Adaptation ───────────────────────────────────────────────────────

    def recompute(self) -> bool:
        """Nudge weights toward the more *useful* timeframes (bounded).

        Returns ``True`` if any weight changed.  Safe to call on a timer; a
        no-op (insufficient samples, disabled, or already converged) just
        returns ``False``.  Never raises.
        """
        try:
            with self._lock:
                if not self.enabled or self._total_trades < self._min_trades:
                    return False

                # Per-TF observation usefulness, defaulting to 0.5 (neutral) when
                # a TF has no samples yet so it neither gains nor loses weight.
                accs: Dict[str, float] = {}
                for tf in self._defaults:
                    a = self._acc[tf].accuracy()
                    accs[tf] = 0.5 if a is None else a
                mean_acc = sum(accs.values()) / len(accs)

                # Target weight: scale the default by how far this TF's observation
                # usefulness sits above/below the mean, gated by adapt_gain.
                # >mean → up, <mean → down. Then clamp + limit per-cycle shift +
                # renormalise.
                targets: Dict[str, float] = {}
                for tf, base in self._defaults.items():
                    factor = 1.0 + self._adapt_gain * (accs[tf] - mean_acc)
                    targets[tf] = base * max(0.0, factor)

                # Limit how far each weight moves this cycle, then clamp.
                staged: Dict[str, float] = {}
                for tf, cur in self._weights.items():
                    desired = targets.get(tf, cur)
                    delta = desired - cur
                    if delta > self._max_shift:
                        delta = self._max_shift
                    elif delta < -self._max_shift:
                        delta = -self._max_shift
                    moved = cur + delta
                    moved = min(self._max_weight, max(self._min_weight, moved))
                    staged[tf] = moved

                normed = self._renormalize(staged)
                changed = any(
                    abs(normed[tf] - self._weights[tf]) > 1e-9
                    for tf in normed
                )
                if changed:
                    self._last_change = {
                        tf: round(normed[tf] - self._weights[tf], 6)
                        for tf in normed
                    }
                    self._weights = normed
                self._recompute_count += 1
                if changed:
                    self._save()
                    logger.info(
                        "[adaptive-weights] recomputed (trades={}) → {}",
                        self._total_trades,
                        {k: round(v, 3) for k, v in normed.items()},
                    )
                return changed
        except Exception as exc:  # noqa: BLE001
            logger.debug("[adaptive-weights] recompute fell back: {}", exc)
            return False

    def reset_to_defaults(self) -> None:
        """Emergency escape hatch — revert weights and clear usefulness history."""
        with self._lock:
            self._weights = dict(self._defaults)
            self._acc = {
                tf: _TFObservationUsefulness(self._window_size) for tf in self._defaults
            }
            self._total_trades = 0
            self._last_change = {}
            self._save()
        logger.warning("[adaptive-weights] reset to static defaults")

    # ── Bounds helper ────────────────────────────────────────────────────

    def _renormalize(self, weights: Dict[str, float]) -> Dict[str, float]:
        """Clamp to [min, max] then scale to sum 1.0 (clamp wins on tension)."""
        clamped = {
            tf: min(self._max_weight, max(self._min_weight, float(w)))
            for tf, w in weights.items()
        }
        total = sum(clamped.values())
        if total <= 0:
            return dict(self._defaults)
        scaled = {tf: w / total for tf, w in clamped.items()}
        # A single renormalise pass can push a value back outside the band; a
        # couple of clamp+renormalise iterations settle it within tolerance.
        for _ in range(3):
            out_of_band = any(
                v < self._min_weight - 1e-9 or v > self._max_weight + 1e-9
                for v in scaled.values()
            )
            if not out_of_band:
                break
            scaled = {
                tf: min(self._max_weight, max(self._min_weight, v))
                for tf, v in scaled.items()
            }
            total = sum(scaled.values())
            scaled = {tf: v / total for tf, v in scaled.items()}
        return scaled

    # ── Observability ────────────────────────────────────────────────────

    def status(self) -> dict:
        """Snapshot for the dashboard / ops."""
        with self._lock:
            accs = {
                tf: (None if self._acc[tf].accuracy() is None
                     else round(self._acc[tf].accuracy(), 4))
                for tf in self._defaults
            }
            samples = {tf: self._acc[tf].samples for tf in self._defaults}
            adapting = self.enabled and self._total_trades >= self._min_trades
            return {
                "enabled": self.enabled,
                "adapting": adapting,
                "total_trades": self._total_trades,
                "min_trades": self._min_trades,
                "recompute_count": self._recompute_count,
                "defaults": {k: round(v, 4) for k, v in self._defaults.items()},
                "weights": {k: round(v, 4) for k, v in self._weights.items()},
                "accuracy": accs,
                "samples": samples,
                "last_change": dict(self._last_change),
                "bounds": {
                    "min_weight": self._min_weight,
                    "max_weight": self._max_weight,
                    "max_shift_per_cycle": self._max_shift,
                },
            }

    # ── Persistence ──────────────────────────────────────────────────────

    def _save(self) -> None:
        if self._state_path is None:
            return
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "weights": self._weights,
                "total_trades": self._total_trades,
                "recompute_count": self._recompute_count,
                "accuracy_window": {
                    tf: list(self._acc[tf].window) for tf in self._acc
                },
            }
            tmp = self._state_path.with_suffix(self._state_path.suffix + ".tmp")
            tmp.write_text(json.dumps(payload), encoding="utf-8")
            tmp.replace(self._state_path)
        except Exception as exc:  # noqa: BLE001
            logger.debug("[adaptive-weights] save failed: {}", exc)

    def _load(self) -> None:
        if self._state_path is None or not self._state_path.exists():
            return
        try:
            payload = json.loads(self._state_path.read_text(encoding="utf-8"))
            saved = payload.get("weights") or {}
            # Only accept timeframes we know about; renormalise defensively.
            merged = dict(self._defaults)
            for tf, w in saved.items():
                if tf in merged:
                    merged[tf] = float(w)
            self._weights = self._renormalize(merged)
            self._total_trades = int(payload.get("total_trades", 0) or 0)
            self._recompute_count = int(payload.get("recompute_count", 0) or 0)
            windows = payload.get("accuracy_window") or {}
            for tf, vals in windows.items():
                acc = self._acc.get(tf)
                if acc is None:
                    continue
                for v in list(vals)[-self._window_size:]:
                    acc.window.append(1 if v else 0)
            logger.info(
                "[adaptive-weights] loaded state (trades={}) → {}",
                self._total_trades,
                {k: round(v, 3) for k, v in self._weights.items()},
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("[adaptive-weights] load failed, using defaults: {}", exc)
            self._weights = dict(self._defaults)


__all__ = ["AdaptiveWeightProvider"]
