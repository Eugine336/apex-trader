"""APEX TRADER — Management translator (Brain manage() → executable action).

Constitution Part VI (Campaign Management) + Part IX Art 4/5: the AI Cognitive
Brain continuously re-reasons over an OPEN campaign and emits a management
:class:`~cognition.contracts.DecisionPackage` (HOLD / SCALE_IN / SCALE_OUT /
PROTECT_PROFIT / TIGHTEN_RISK / EXIT / REVERSE / TERMINATE_CAMPAIGN). This module
turns that verdict into a *semantic* :class:`ManagementAction` the execution
plane can realise on the broker — the management-side twin of
:func:`cognition.campaign_translator.translate`.

It carries NO broker specifics (no ticket, no live price): those are resolved by
the execution sink that owns the broker. This keeps the translation pure and
fully offline-testable. Fail-safe: an unmappable/empty verdict yields ``None``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class ManagementAction:
    """A broker-agnostic instruction derived from a Brain management verdict.

    ``kind`` is one of: ``close`` (full exit), ``partial_close`` (reduce by
    ``fraction``), ``tighten_sl`` / ``protect_sl`` (move the stop — the sink
    computes the concrete level from the live position), ``reverse`` (close then
    open the opposite), ``scale_in`` (add to the winner). ``direction`` is the
    currently-held side; ``target_direction`` is where a reverse should end up.
    """

    symbol: str
    kind: str
    direction: str = ""                  # held side (LONG | SHORT)
    target_direction: str = ""           # for reverse: the opposite side
    fraction: float = 0.0                # for partial_close (0..1)
    confidence: float = 0.0
    reason: str = ""
    decision_id: str = ""

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "kind": self.kind,
            "direction": self.direction,
            "target_direction": self.target_direction,
            "fraction": round(self.fraction, 4),
            "confidence": round(self.confidence, 4),
            "reason": self.reason[:200],
            "decision_id": self.decision_id,
        }


_OPP = {"LONG": "SHORT", "SHORT": "LONG"}


def translate_management(
    output: Any,
    position: Any,
    *,
    scale_out_fraction: float = 0.5,
) -> Optional[ManagementAction]:
    """Map a Brain management ``BrainOutput`` → a :class:`ManagementAction`.

    Returns ``None`` for HOLD / observe / non-directional verdicts (nothing to
    do) and for any malformed input. Never raises.
    """
    try:
        decision = getattr(output, "decision", None)
        if decision is None:
            return None
        dtype = getattr(decision, "decision_type", None)
        name = getattr(dtype, "value", None) or str(dtype or "")
        name = str(name).strip().lower()
        symbol = str(getattr(decision, "symbol", "")
                     or getattr(position, "symbol", "") or "")
        if not symbol:
            return None
        held = str(getattr(position, "direction", "") or "").upper()
        conf = float(getattr(decision, "confidence", 0.0) or 0.0)
        reason = str(getattr(decision, "thesis", "") or "")[:200]
        dec_id = str(getattr(decision, "decision_id", "") or "")

        def _act(kind: str, **kw: Any) -> ManagementAction:
            return ManagementAction(
                symbol=symbol, kind=kind, direction=held,
                confidence=conf, reason=reason, decision_id=dec_id, **kw,
            )

        if name in ("exit", "terminate_campaign"):
            return _act("close")
        if name == "reverse":
            tgt = _OPP.get(held, "")
            if not tgt:
                return _act("close")   # can't reverse an unknown side — just exit
            return _act("reverse", target_direction=tgt)
        if name == "scale_out":
            frac = scale_out_fraction
            try:
                frac = float(scale_out_fraction)
            except (TypeError, ValueError):
                frac = 0.5
            frac = min(0.95, max(0.05, frac))
            return _act("partial_close", fraction=frac)
        if name == "protect_profit":
            return _act("protect_sl")
        if name == "tighten_risk":
            return _act("tighten_sl")
        if name == "scale_in":
            return _act("scale_in")
        # hold / continue_observing / open_campaign / reject_opportunity / unknown
        return None
    except Exception:  # noqa: BLE001 — translation must never raise
        return None


__all__ = ["ManagementAction", "translate_management"]
