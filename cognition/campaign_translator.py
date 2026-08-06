"""APEX TRADER — Campaign translator (Phase G: origination from the Brain's spec).

Constitution Part VI Art 2: execution consumes the Brain's
:class:`~cognition.contracts.CampaignSpecification` and faithfully realises it
WITHOUT reinterpreting the market thesis. This module performs that translation:
a :class:`CampaignSpecification` → an :class:`OriginationIntent` (symbol,
direction, size/stake, optional stop/target, provenance). It carries no market
judgment — it only sizes and shapes what the Brain already decided.

Pure standard library, fully offline-testable.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Optional


def _clampf(v: Any, lo: float, hi: float, default: float) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    if f != f:
        return default
    return min(hi, max(lo, f))


@dataclass
class OriginationIntent:
    """A Brain-originated order shaped from a CampaignSpecification.

    Execution validates feasibility and fills protective stops; this object only
    conveys the Brain's intent (direction + desired exposure) with provenance.
    """

    symbol: str
    direction: str                       # LONG | SHORT
    exposure: float = 0.0                # 0..1 normalised target exposure
    stake_usd: Optional[float] = None    # resolved from balance × risk × exposure
    lots: float = 0.0                    # 0 ⇒ executor/portfolio sizes
    sl: Optional[float] = None
    tp: Optional[float] = None
    reason: str = ""
    source: str = "ai_brain"
    campaign_id: str = ""
    decision_id: str = ""
    confidence: float = 0.0
    meta: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "direction": self.direction,
            "exposure": round(self.exposure, 4),
            "stake_usd": None if self.stake_usd is None else round(self.stake_usd, 2),
            "lots": round(self.lots, 4),
            "sl": self.sl,
            "tp": self.tp,
            "reason": self.reason,
            "source": self.source,
            "campaign_id": self.campaign_id,
            "decision_id": self.decision_id,
            "confidence": round(self.confidence, 4),
        }


def derive_protective_levels(
    direction: str,
    entry_price: float,
    *,
    stop_fraction: float = 0.004,
    reward_multiple: float = 2.0,
    min_rr: float = 1.0,
    structural_targets: Optional[list] = None,
) -> "tuple[Optional[float], Optional[float]]":
    """Deterministically derive a protective ``(sl, tp)`` for an originated entry.

    Constitution Part X: every entry must carry a deterministic protective stop.
    When the Brain's spec omits one, execution derives it here rather than
    trading unprotected (or refusing to trade at all). Instrument-agnostic:

    * ``sl`` sits ``stop_fraction`` of price on the protective side (0.4% by
      default) — a bounded, always-valid distance that scales across gold/FX.
    * ``tp`` is the nearest live structural level at least ``min_rr`` × risk
      ahead (from the WorldModel) when one exists, else a ``reward_multiple`` ×
      risk target.

    Returns ``(None, None)`` on any invalid input or if the computed levels are
    not correctly ordered around ``entry_price`` — the caller then declines to
    submit rather than send an unsafe order. Never raises.
    """
    try:
        d = str(direction or "").upper()
        price = float(entry_price)
        if d not in ("LONG", "SHORT") or price <= 0:
            return (None, None)
        frac = float(stop_fraction)
        if not (frac > 0.0):
            return (None, None)
        risk = price * frac
        if risk <= 0:
            return (None, None)
        is_long = d == "LONG"
        sl = price - risk if is_long else price + risk
        rr = max(float(min_rr), 0.0)
        min_ahead = risk * rr
        tp = None
        for t in list(structural_targets or []):
            try:
                tv = float(t)
            except (TypeError, ValueError):
                continue
            if is_long and tv >= price + min_ahead:
                tp = tv
                break
            if (not is_long) and tv <= price - min_ahead:
                tp = tv
                break
        if tp is None:
            reward = risk * max(float(reward_multiple), 0.1)
            tp = price + reward if is_long else price - reward
        sl = round(sl, 6)
        tp = round(tp, 6)
        if is_long and not (sl < price < tp):
            return (None, None)
        if (not is_long) and not (tp < price < sl):
            return (None, None)
        return (sl, tp)
    except Exception:  # noqa: BLE001 — derivation must never raise
        return (None, None)


def translate(
    spec: Any,
    *,
    balance: float = 0.0,
    risk_fraction: float = 0.01,
    max_exposure: float = 1.0,
) -> Optional[OriginationIntent]:
    """Translate a CampaignSpecification into an OriginationIntent.

    ``stake_usd`` = ``balance × risk_fraction × exposure`` when a balance is
    known (else ``None`` ⇒ the executor/portfolio sizes). Returns ``None`` for a
    non-directional / empty spec. Never raises.
    """
    try:
        direction = str(getattr(spec, "direction", "") or "").upper()
        symbol = str(getattr(spec, "symbol", "") or "")
        if direction not in ("LONG", "SHORT") or not symbol:
            return None
        exposure = _clampf(getattr(spec, "desired_exposure", 0.0), 0.0,
                           _clampf(max_exposure, 0.0, 1.0, 1.0), 0.0)
        stake = None
        if balance and balance > 0 and risk_fraction > 0 and exposure > 0:
            stake = float(balance) * float(risk_fraction) * float(exposure)
        intent_meta = getattr(spec, "initial_execution_intent", {}) or {}
        sl = intent_meta.get("sl") if isinstance(intent_meta, dict) else None
        tp = intent_meta.get("tp") if isinstance(intent_meta, dict) else None
        return OriginationIntent(
            symbol=symbol,
            direction=direction,
            exposure=exposure,
            stake_usd=stake,
            sl=sl,
            tp=tp,
            reason=str(getattr(spec, "thesis", "") or "")[:280],
            campaign_id=str(getattr(spec, "campaign_id", "") or ""),
            decision_id=str(getattr(spec, "decision_id", "") or ""),
            confidence=_clampf(getattr(spec, "confidence", 0.0), 0.0, 1.0, 0.0),
            meta={"objectives": list(getattr(spec, "objectives", []) or [])},
        )
    except Exception:  # noqa: BLE001 — translation must never raise
        return None


def compute_lot_size(
    *,
    risk_usd: float,
    stop_distance: float,
    tick_value: float,
    tick_size: float,
    vol_min: float,
    vol_step: float,
    vol_max: float = 1e9,
    max_lots: float = 0.0,
    floor_to_min: bool = True,
) -> float:
    """Deterministic, broker-valid lot size for a Brain-originated entry.

    The Brain decides *how much conviction* to put on a trade; this converts
    that into the actual lot a human would type — sized by risk and snapped to
    the broker's volume ladder so the order is always valid:

    * ``lots = risk_usd / loss_per_lot`` where ``loss_per_lot`` = the money lost
      on 1.0 lot if price travels ``stop_distance`` (from tick value/size).
    * Rounded **down** to ``vol_step`` (never round risk up), then bounded by
      ``vol_max`` and an optional ``max_lots`` cap.
    * If the risk-correct size is below the broker minimum: return ``vol_min``
      when ``floor_to_min`` (the opportunity-harvest opt-in), else ``0.0`` so the
      caller declines rather than sending an under-min order.

    Returns ``0.0`` (skip) on unusable inputs unless ``floor_to_min`` lets a
    valid ``vol_min`` stand. Never raises.
    """
    try:
        vmin = max(0.0, float(vol_min))
        vstep = max(0.0, float(vol_step))
        vmax = float(vol_max) if vol_max and vol_max > 0 else 1e9
        base = 0.0
        try:
            sd = float(stop_distance)
            tv = float(tick_value)
            ts = float(tick_size)
            ru = float(risk_usd)
            if sd > 0 and tv > 0 and ts > 0 and ru > 0:
                loss_per_lot = (sd / ts) * tv
                if loss_per_lot > 0:
                    base = ru / loss_per_lot
        except (TypeError, ValueError):
            base = 0.0
        # Snap DOWN to the broker volume step.
        if vstep > 0 and base > 0:
            base = math.floor(base / vstep + 1e-9) * vstep
        # Apply caps before the min-floor so a cap can never sit below min.
        if max_lots and max_lots > 0:
            base = min(base, float(max_lots))
        base = min(base, vmax)
        # Below the broker minimum: floor to min (harvest) or decline.
        if base < vmin or base <= 0:
            if floor_to_min and vmin > 0 and vmin <= vmax:
                return round(vmin, 2)
            return 0.0
        return round(base, 2)
    except Exception:  # noqa: BLE001 — sizing must never raise
        return 0.0


__all__ = ["OriginationIntent", "translate", "derive_protective_levels", "compute_lot_size"]
