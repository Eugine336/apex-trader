"""
APEX TRADER — Opportunity-Quality-Proportional Sizer (GAP 3)

The capital allocator sizes by STRATEGY FINGERPRINT (MARKET|SCALP, …), so a
stellar setup and a mediocre one with the same fingerprint get the same size.
This module sizes by the QUALITY of the specific opportunity: the best idea in a
cross-instrument cycle is sized up (toward ``max_boost``), weaker ideas are sized
down (toward ``min_cut``).

The multiplier it returns is applied ON TOP of the existing de-risking sizing
chain (vol × density × exec × cap × …). It can suggest a boost > 1.0, but the
downstream per-trade risk ceiling still clamps the final size — the sizer only
expresses a preference, it never overrides risk.

Quality is read from the opportunity's own evidence:
  * EV (R units) relative to ``ev_ref`` — the dominant signal,
  * confidence (0..1) — a secondary nudge,
  * cross-instrument rank position (best = top) — when the queue supplies it.

When disabled (the default) ``multiplier`` returns exactly ``1.0`` — an identity
no-op, so sizing is byte-for-byte unchanged.

Leaf module: stdlib + loguru only.
"""

from __future__ import annotations

from typing import Optional

from loguru import logger


class OpportunityQualitySizer:
    """Maps an opportunity's quality to a sizing multiplier in [min_cut, max_boost]."""

    def __init__(
        self,
        *,
        enabled: bool = False,
        max_boost: float = 1.3,
        min_cut: float = 0.7,
        ev_ref: float = 1.0,
    ) -> None:
        self.enabled = bool(enabled)
        self._max_boost = max(1.0, float(max_boost))
        self._min_cut = min(1.0, max(0.01, float(min_cut)))
        self._ev_ref = max(1e-6, float(ev_ref))

    def multiplier(
        self,
        *,
        ev: float = 0.0,
        confidence: float = 0.0,
        rank: Optional[int] = None,
        rank_total: Optional[int] = None,
    ) -> float:
        """Quality-proportional sizing multiplier.

        Returns ``1.0`` (identity) when disabled. Otherwise blends an EV term
        (EV/ev_ref, the primary driver), a confidence term, and — when the queue
        supplies the cross-instrument rank — a rank term (top of the queue sizes
        up, the tail sizes down). The blended quality in [0, 1] is mapped onto
        [min_cut, max_boost] with quality 0.5 → ~1.0 (neutral).
        """
        if not self.enabled:
            return 1.0
        try:
            ev_f = float(ev)
            conf_f = float(confidence)
            # Non-finite quality inputs are treated as "no signal" (neutral),
            # never as a boost — a NaN EV must not size a trade up.
            if not _isfinite(ev_f):
                ev_f = 0.0
            if not _isfinite(conf_f):
                conf_f = 0.0
            ev_q = _clamp01(0.5 + 0.5 * (ev_f / self._ev_ref))
            conf_q = _clamp01(conf_f)

            if rank is not None and rank_total and int(rank_total) > 1:
                # rank is 0-based, best = 0 → 1.0, worst → 0.0
                rank_q = 1.0 - (float(rank) / float(int(rank_total) - 1))
                rank_q = _clamp01(rank_q)
                quality = 0.5 * ev_q + 0.25 * conf_q + 0.25 * rank_q
            else:
                quality = 0.7 * ev_q + 0.3 * conf_q

            quality = _clamp01(quality)
            # Map [0,1] → [min_cut, max_boost], with 0.5 ≈ neutral (1.0).
            if quality >= 0.5:
                span = self._max_boost - 1.0
                mult = 1.0 + span * ((quality - 0.5) / 0.5)
            else:
                span = 1.0 - self._min_cut
                mult = 1.0 - span * ((0.5 - quality) / 0.5)
            return round(max(self._min_cut, min(self._max_boost, mult)), 4)
        except Exception as exc:  # noqa: BLE001 — sizing must never crash entry
            logger.debug("[quality-sizer] multiplier failed, neutral 1.0: {}", exc)
            return 1.0

    def status(self) -> dict:
        return {
            "enabled": self.enabled,
            "max_boost": self._max_boost,
            "min_cut": self._min_cut,
            "ev_ref": self._ev_ref,
        }


def _clamp01(x: float) -> float:
    if x < 0.0:
        return 0.0
    if x > 1.0:
        return 1.0
    return x


def _isfinite(x: float) -> bool:
    import math
    return math.isfinite(x)


__all__ = ["OpportunityQualitySizer"]
