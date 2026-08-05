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


__all__ = ["OriginationIntent", "translate"]
