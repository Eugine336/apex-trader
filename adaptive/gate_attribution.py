"""APEX TRADER — Gate parameter attribution (per-gate counterfactual).

The :class:`adaptive.gate_tuner.GateTuner` learns bounded, loosening-only
offsets for three *quality* entry gates — ``ev_gate``, ``entry_engine`` and
``htf_alignment``. Those offsets lower the live thresholds when the gate's
rejected setups keep winning. The open question they raise is the one this
module answers:

    *When a trade is opened, was a learned gate adjustment **responsible** for
    letting it through, or would it have passed on the operator's default
    thresholds anyway? And do the trades that learning enabled actually pay?*

The :class:`governance.health_assessor.HealthAssessor` already answers the
*aggregate* question ("is learning collectively helping?"). :class:`GateAttributor`
adds the *per-gate* attribution underneath it: for every trade it snapshots the
gate parameters at entry, replays the gate decision with the **default**
(no-learning) thresholds at close, and classifies each tunable gate as:

* ``DECISIVE``   — the trade would have been **rejected** on the default
  threshold; the learned loosening is what opened it.
* ``SUPPORTING`` — the trade would have passed anyway; learning did not change
  the outcome.
* ``NEUTRAL``    — the gate carried no learned offset (or there is no replay
  input for it), so learning had no influence to attribute.

A trade is **learner-decisive** when *any* gate is ``DECISIVE``. Aggregated over
a bounded rolling window, the count of learner-decisive trades that went on to
**lose** (``decisive_negative_ev_count``) is the early-warning signal: learning
is opening trades that the default thresholds would have (correctly) rejected.

Replay precision, by gate:

* ``entry_engine`` — replayed precisely against the trade's actual entry score
  (the conviction the gate scored), so ``DECISIVE`` means the score genuinely
  sat below the default bar.
* ``ev_gate`` — the trade's *actual* EV value is not threaded out of the entry
  gate, so the close path supplies the **effective** EV threshold as a proxy.
  With that input a loosened ``ev_gate`` reads as ``DECISIVE`` whenever its
  offset is non-zero — a deliberate, conservative over-approximation that flags
  every trade that passed through a loosened EV bar rather than under-counting.
* ``htf_alignment`` — there is no alignment value on the close path to replay
  against, so it is reported ``NEUTRAL`` (its offset is still recorded for
  observability). It is never claimed ``DECISIVE`` without evidence.

Design principles (mirror the rest of the learning/governance leaves):

* **Observational only.** Attribution never changes a threshold, blocks a
  trade, or freezes a tunable. It records and reports.
* **Fail-safe.** Every public method is wrapped — any internal fault is logged
  and a safe default returned. A snapshot/replay error must *never* break the
  entry or close path.
* **Bounded memory.** Closed attributions live in a ``deque(maxlen=...)``; the
  open-snapshot map is popped on close.
* **Thread-safe.** An ``RLock`` guards all mutable state so the entry thread,
  the close thread and a dashboard reader can touch it concurrently.

Leaf module — standard library + loguru only. It never imports a broker, the
WorldModel, or any brain module.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Deque, Dict, Optional

from loguru import logger

# Operator-default thresholds (mirror entry.models.EntryConfig). Used as the
# fallback when the supplied config omits a value, so attribution replays
# against the same neutral bar the live gate falls back to.
_DEFAULT_MIN_EV = 0.3
_DEFAULT_MIN_SCORE = 85.0
_DEFAULT_MIN_HTF_ALIGNMENT = -0.5

# Gates whose default threshold is replayable from the close-path inputs.
# ``htf_alignment`` is intentionally absent — it has no replay input and is
# always reported NEUTRAL (its offset is still recorded for observability).
_REPLAYABLE = ("ev_gate", "entry_engine")

# Floating-point tolerance for "is this offset effectively zero?".
_EPS = 1e-9


class GateImpact(str, Enum):
    """How a single tunable gate's learned offset influenced a trade."""

    DECISIVE = "DECISIVE"        # trade would have been rejected on the default
    SUPPORTING = "SUPPORTING"    # trade would have passed anyway
    NEUTRAL = "NEUTRAL"          # no learned offset / no replay input

    def __str__(self) -> str:  # pragma: no cover - cosmetic
        return self.value


@dataclass
class GateSnapshot:
    """The gate parameters captured at entry, keyed by order id at close."""

    gate_offsets: Dict[str, float] = field(default_factory=dict)
    effective_thresholds: Dict[str, float] = field(default_factory=dict)
    default_thresholds: Dict[str, float] = field(default_factory=dict)
    entry_time: str = ""
    learner_enabled: bool = False

    def to_dict(self) -> dict:
        return {
            "gate_offsets": {k: round(float(v), 6) for k, v in self.gate_offsets.items()},
            "effective_thresholds": {
                k: round(float(v), 6) for k, v in self.effective_thresholds.items()
            },
            "default_thresholds": {
                k: round(float(v), 6) for k, v in self.default_thresholds.items()
            },
            "entry_time": self.entry_time,
            "learner_enabled": bool(self.learner_enabled),
        }


@dataclass
class GateAttribution:
    """The per-gate verdict for one closed trade."""

    order_id: str
    realized_r: float
    learner_decisive: bool
    per_gate: Dict[str, str] = field(default_factory=dict)  # gate -> GateImpact value
    closed_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {
            "order_id": self.order_id,
            "realized_r": round(float(self.realized_r), 4),
            "learner_decisive": bool(self.learner_decisive),
            "per_gate": dict(self.per_gate),
            "closed_at": self.closed_at,
        }


class GateAttributor:
    """Per-gate parameter counterfactual: which learned adjustments opened a trade."""

    def __init__(
        self,
        *,
        gate_tuner: Optional[object] = None,
        config: Optional[object] = None,
        max_history: int = 200,
    ) -> None:
        self._gate_tuner = gate_tuner
        # Operator-default (no-learning) thresholds, read from the entry config
        # with the EntryConfig defaults as the fallback. These are the bars the
        # replay compares against.
        self._defaults: Dict[str, float] = {
            "ev_gate": float(getattr(config, "min_entry_ev", _DEFAULT_MIN_EV)),
            "entry_engine": float(getattr(config, "min_entry_score", _DEFAULT_MIN_SCORE)),
            "htf_alignment": float(
                getattr(config, "min_htf_alignment", _DEFAULT_MIN_HTF_ALIGNMENT)
            ),
        }
        self._snapshots: Dict[str, GateSnapshot] = {}
        self._closed: Deque[GateAttribution] = deque(maxlen=max(1, int(max_history)))
        self._lock = threading.RLock()

    # ── Defaults / introspection ─────────────────────────────────────────

    def default_thresholds(self) -> Dict[str, float]:
        """The operator-default thresholds the replay compares against."""
        return dict(self._defaults)

    # ── Entry ─────────────────────────────────────────────────────────────

    def record_entry(
        self,
        order_id: str,
        gate_offsets: Dict[str, float],
        effective_thresholds: Dict[str, float],
        default_thresholds: Optional[Dict[str, float]] = None,
    ) -> Optional[GateSnapshot]:
        """Snapshot the gate parameters at entry. Fail-safe — returns None on fault.

        ``learner_enabled`` is True when *any* gate offset is non-zero (a learned
        adjustment influenced the thresholds this trade cleared).
        """
        try:
            oid = str(order_id)
            offsets = {str(k): float(v) for k, v in (gate_offsets or {}).items()}
            effective = {str(k): float(v) for k, v in (effective_thresholds or {}).items()}
            defaults = (
                {str(k): float(v) for k, v in default_thresholds.items()}
                if default_thresholds
                else dict(self._defaults)
            )
            learner_enabled = any(abs(v) > _EPS for v in offsets.values())
            snap = GateSnapshot(
                gate_offsets=offsets,
                effective_thresholds=effective,
                default_thresholds=defaults,
                entry_time=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                learner_enabled=learner_enabled,
            )
            with self._lock:
                self._snapshots[oid] = snap
            return snap
        except Exception as exc:  # noqa: BLE001 — attribution must never break entry
            logger.debug("[gate-attr] record_entry ignored a fault: {}", exc)
            return None

    # ── Close ─────────────────────────────────────────────────────────────

    def record_close(
        self,
        order_id: str,
        realized_r: float,
        entry_ev: float,
        entry_score: float,
    ) -> Optional[GateAttribution]:
        """Replay the gate decision with default thresholds and attribute impact.

        Fail-safe — any fault is logged and ``None`` returned. A missing snapshot
        (entry not captured) also returns ``None``.
        """
        try:
            oid = str(order_id)
            with self._lock:
                snap = self._snapshots.pop(oid, None)
            if snap is None:
                return None

            r = float(realized_r)
            if r != r:  # NaN guard (NaN != NaN)
                r = 0.0
            ev = float(entry_ev)
            score = float(entry_score)

            per_gate: Dict[str, str] = {}
            decisive = False
            for family, default_thr in snap.default_thresholds.items():
                offset = snap.gate_offsets.get(family, 0.0)
                impact = self._classify_gate(family, offset, default_thr, ev, score)
                per_gate[family] = impact.value
                if impact == GateImpact.DECISIVE:
                    decisive = True

            attribution = GateAttribution(
                order_id=oid,
                realized_r=r,
                learner_decisive=decisive,
                per_gate=per_gate,
            )
            with self._lock:
                self._closed.append(attribution)
            return attribution
        except Exception as exc:  # noqa: BLE001 — attribution must never break close
            logger.debug("[gate-attr] record_close ignored a fault: {}", exc)
            return None

    def _classify_gate(
        self,
        family: str,
        offset: float,
        default_threshold: float,
        entry_ev: float,
        entry_score: float,
    ) -> GateImpact:
        """Classify one gate's learned influence on the trade.

        A gate is DECISIVE when the trade's metric sits *below* the default
        (no-learning) threshold — i.e. it only cleared because the gate was
        loosened. SUPPORTING when it would have cleared the default anyway.
        NEUTRAL when there is no learned offset, or the gate has no replay input.
        """
        if abs(offset) <= _EPS:
            return GateImpact.NEUTRAL
        if family not in _REPLAYABLE:
            # e.g. htf_alignment — offset recorded, but no replay metric on the
            # close path, so we never claim it DECISIVE without evidence.
            return GateImpact.NEUTRAL

        if family == "ev_gate":
            metric = entry_ev
        elif family == "entry_engine":
            metric = entry_score
        else:  # pragma: no cover — guarded by _REPLAYABLE
            return GateImpact.NEUTRAL

        return (
            GateImpact.DECISIVE
            if metric < default_threshold
            else GateImpact.SUPPORTING
        )

    # ── Aggregate summary ─────────────────────────────────────────────────

    def summarize(self) -> dict:
        """Rolling aggregate of closed attributions. Fail-safe — never raises."""
        try:
            with self._lock:
                closed = list(self._closed)

            total = len(closed)
            decisive = [a for a in closed if a.learner_decisive]
            supporting = [a for a in closed if not a.learner_decisive]

            decisive_count = len(decisive)
            supporting_count = len(supporting)
            decisive_avg_r = (
                sum(a.realized_r for a in decisive) / decisive_count
                if decisive_count
                else 0.0
            )
            supporting_avg_r = (
                sum(a.realized_r for a in supporting) / supporting_count
                if supporting_count
                else 0.0
            )
            decisive_negative_ev_count = sum(
                1 for a in decisive if a.realized_r < 0.0
            )

            # Per-gate breakdown: how often each gate was decisive/supporting and
            # the realized R of the trades it was decisive on.
            per_gate: Dict[str, dict] = {}
            for family in self._defaults:
                dec_rs = [
                    a.realized_r
                    for a in closed
                    if a.per_gate.get(family) == GateImpact.DECISIVE.value
                ]
                sup_n = sum(
                    1
                    for a in closed
                    if a.per_gate.get(family) == GateImpact.SUPPORTING.value
                )
                per_gate[family] = {
                    "decisive_count": len(dec_rs),
                    "supporting_count": sup_n,
                    "decisive_avg_r": round(
                        sum(dec_rs) / len(dec_rs), 4
                    ) if dec_rs else 0.0,
                }

            return {
                "total_closed": total,
                "decisive_count": decisive_count,
                "supporting_count": supporting_count,
                "decisive_avg_r": round(decisive_avg_r, 4),
                "supporting_avg_r": round(supporting_avg_r, 4),
                "decisive_negative_ev_count": decisive_negative_ev_count,
                "per_gate": per_gate,
            }
        except Exception as exc:  # noqa: BLE001 — observational, never raise
            logger.debug("[gate-attr] summarize ignored a fault: {}", exc)
            return {
                "total_closed": 0,
                "decisive_count": 0,
                "supporting_count": 0,
                "decisive_avg_r": 0.0,
                "supporting_avg_r": 0.0,
                "decisive_negative_ev_count": 0,
                "per_gate": {},
            }

    def reset(self) -> None:
        """Drop all snapshots and closed attributions (tests / manual ops reset)."""
        with self._lock:
            self._snapshots.clear()
            self._closed.clear()


__all__ = ["GateAttributor", "GateAttribution", "GateSnapshot", "GateImpact"]
