"""APEX TRADER — Consultation Ledger (Constitution Part XXI Art 9/10/11).

The Advisory Council is only institutional if the Brain *remembers* whom it
consulted and *grades* each advisor continuously. This is that memory:

* **Consultation records (Art 11).** Every time the Reasoning Orchestrator fans
  a query out to the council, one compact record is kept — the symbol, the
  reasoning capability requested, which advisors were asked, which replied, each
  reply's direction/confidence/latency, the council's majority direction and its
  dispersion (how split the council was), and the market uncertainty at the
  time. These records are institutional memory (optionally appended to a JSONL
  file), never thrown away.

* **Per-advisor scorecards (Art 9/10).** From those records the ledger keeps a
  rolling scorecard per advisor: how often it was consulted, how often it
  actually replied (reply rate), its average confidence and latency, and how
  often it agreed with the council majority. A per-capability breakdown lets the
  Brain learn *which advisor is strong in which domain* (Art 10) — strategic vs
  risk vs rapid inference, etc.

This is a *record and measurement* layer, not authority: it never reasons,
selects, votes, or decides — that remains the Reasoning Orchestrator (selection)
and the one Cognitive Brain (synthesis + decision). Outcome-based grading by
realised P&L is already handled by the Phase VIII influence ledger; this ledger
captures the *consultation itself* and agreement structure, which the influence
layer cannot see.

Pure standard library, thread-safe and fail-safe throughout: a fault while
recording yields a poorer scorecard, never an exception into the cognition loop.
Secret-safe — it stores only advisor names and their opinions, never keys.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from typing import Any, Callable, Optional

from loguru import logger

_DIRS = ("LONG", "SHORT", "FLAT")


def _norm_dir(d: Any) -> str:
    s = str(d or "").strip().upper()
    return s if s in _DIRS else "FLAT"


def _f(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


class _AdvisorScore:
    """Rolling stats for one advisor (overall + per capability). Mutable."""

    __slots__ = ("consulted", "replies", "sum_conf", "sum_latency",
                 "agree_majority", "by_capability")

    def __init__(self) -> None:
        self.consulted = 0
        self.replies = 0
        self.sum_conf = 0.0
        self.sum_latency = 0.0
        self.agree_majority = 0
        # capability -> dict(consulted, replies, agree_majority)
        self.by_capability: dict = {}

    def observe(self, *, replied: bool, confidence: float, latency_ms: float,
                agreed: bool, capability: str) -> None:
        self.consulted += 1
        cap = self.by_capability.setdefault(
            capability or "", {"consulted": 0, "replies": 0, "agree_majority": 0})
        cap["consulted"] += 1
        if replied:
            self.replies += 1
            self.sum_conf += max(0.0, min(1.0, confidence))
            self.sum_latency += max(0.0, latency_ms)
            cap["replies"] += 1
            if agreed:
                self.agree_majority += 1
                cap["agree_majority"] += 1

    def to_dict(self) -> dict:
        replies = self.replies
        caps = {
            k: {
                "consulted": v["consulted"],
                "replies": v["replies"],
                "agreement_rate": (round(v["agree_majority"] / v["replies"], 4)
                                   if v["replies"] else None),
            }
            for k, v in sorted(self.by_capability.items())
        }
        return {
            "consulted": self.consulted,
            "replies": replies,
            "reply_rate": round(replies / self.consulted, 4) if self.consulted else None,
            "avg_confidence": round(self.sum_conf / replies, 4) if replies else None,
            "avg_latency_ms": round(self.sum_latency / replies, 1) if replies else None,
            "agreement_rate": round(self.agree_majority / replies, 4) if replies else None,
            "by_capability": caps,
        }


def _council_majority(opinions: list) -> "tuple[str, float]":
    """Return (majority_direction, dispersion) among the replied opinions.

    Majority is by count of directions (ties broken by summed confidence);
    dispersion is the fraction of repliers NOT in the majority (0 = unanimous).
    """
    if not opinions:
        return "FLAT", 0.0
    counts: dict = {d: 0 for d in _DIRS}
    conf: dict = {d: 0.0 for d in _DIRS}
    for o in opinions:
        d = _norm_dir(getattr(o, "direction", "FLAT"))
        counts[d] += 1
        conf[d] += max(0.0, min(1.0, _f(getattr(o, "confidence", 0.0))))
    majority = max(_DIRS, key=lambda d: (counts[d], conf[d]))
    total = sum(counts.values())
    dispersion = 1.0 - (counts[majority] / total) if total else 0.0
    return majority, round(dispersion, 4)


class ConsultationLedger:
    """Records council consultations and grades each advisor (Art 9/10/11).

    Thread-safe and fail-safe. ``record`` is called by the consolidator right
    after the orchestrator replies. Persistence is optional and best-effort: a
    ``persist_path`` (JSONL) appends every record as institutional memory.
    """

    def __init__(
        self,
        *,
        max_records: int = 500,
        persist_path: Optional[str] = None,
        clock: Optional[Callable[[], float]] = None,
    ) -> None:
        self._records: deque = deque(maxlen=max(1, int(max_records)))
        self._advisors: dict = {}          # name -> _AdvisorScore
        self._total = 0
        self._persist_path = str(persist_path) if persist_path else ""
        self._clock = clock or time.time
        self._lock = threading.Lock()

    def record(
        self,
        consultation: Any,
        *,
        uncertainty: Optional[float] = None,
        capability: Optional[str] = None,
        now: Optional[float] = None,
    ) -> Optional[dict]:
        """Record one consultation + update per-advisor scorecards. Fail-safe."""
        try:
            consulted = [str(n) for n in (getattr(consultation, "consulted", None) or [])]
            opinions = list(getattr(consultation, "opinions", None) or [])
            if not consulted and not opinions:
                return None
            cap = str(
                capability if capability is not None
                else getattr(consultation, "capability", "") or ""
            ).strip().lower()
            symbol = str(getattr(consultation, "symbol", "") or "")
            majority, dispersion = _council_majority(opinions)

            # Index replies by engine for O(1) lookup while walking consulted.
            replied: dict = {}
            for o in opinions:
                eng = str(getattr(o, "engine", "") or "").strip()
                if eng:
                    replied[eng] = o

            ts = _f(now) if now is not None else self._clock()
            unc = None if uncertainty is None else round(max(0.0, min(1.0, _f(uncertainty))), 4)
            op_rows = []
            with self._lock:
                self._total += 1
                # Grade every advisor that was ASKED (reply or not).
                names = consulted or list(replied.keys())
                for name in names:
                    score = self._advisors.setdefault(name, _AdvisorScore())
                    op = replied.get(name)
                    if op is not None:
                        d = _norm_dir(getattr(op, "direction", "FLAT"))
                        c = max(0.0, min(1.0, _f(getattr(op, "confidence", 0.0))))
                        lat = _f(getattr(op, "latency_ms", 0.0))
                        score.observe(replied=True, confidence=c, latency_ms=lat,
                                      agreed=(d == majority), capability=cap)
                    else:
                        score.observe(replied=False, confidence=0.0, latency_ms=0.0,
                                      agreed=False, capability=cap)
                for o in opinions:
                    op_rows.append({
                        "engine": str(getattr(o, "engine", "") or ""),
                        "direction": _norm_dir(getattr(o, "direction", "FLAT")),
                        "confidence": round(max(0.0, min(1.0, _f(getattr(o, "confidence", 0.0)))), 4),
                        "latency_ms": round(_f(getattr(o, "latency_ms", 0.0)), 1),
                    })
                rec = {
                    "ts": round(ts, 3),
                    "symbol": symbol,
                    "capability": cap,
                    "consulted": names,
                    "replies": len(op_rows),
                    "majority": majority,
                    "dispersion": dispersion,
                    "uncertainty": unc,
                    "opinions": op_rows,
                }
                self._records.append(rec)
            self._persist(rec)
            return rec
        except Exception as exc:  # noqa: BLE001 — recording must never break the loop
            logger.debug("[consultation-ledger] record fault: {}", exc)
            return None

    def scorecard(self, name: str) -> Optional[dict]:
        with self._lock:
            score = self._advisors.get(str(name or ""))
            return score.to_dict() if score is not None else None

    def scorecards(self) -> dict:
        with self._lock:
            return {n: s.to_dict() for n, s in sorted(self._advisors.items())}

    def recent(self, limit: int = 20) -> list:
        with self._lock:
            n = max(1, int(limit))
            return list(self._records)[-n:]

    def get_status(self) -> dict:
        with self._lock:
            last = self._records[-1] if self._records else None
            advisors = {n: s.to_dict() for n, s in sorted(self._advisors.items())}
            total = self._total
            kept = len(self._records)
        return {
            "consultations": total,
            "records_kept": kept,
            "advisor_count": len(advisors),
            "advisors": advisors,
            "last": last,
        }

    def _persist(self, record: dict) -> None:
        if not self._persist_path:
            return
        try:
            import json
            with open(self._persist_path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, separators=(",", ":")) + "\n")
        except Exception as exc:  # noqa: BLE001 — persistence is best-effort
            logger.debug("[consultation-ledger] persist fault: {}", exc)


def build_consultation_ledger(
    *,
    enabled: bool = True,
    max_records: int = 500,
    persist: bool = False,
) -> Optional[ConsultationLedger]:
    """Construct a :class:`ConsultationLedger`, or ``None`` when disabled.

    When ``persist`` is true a JSONL path under the runtime data dir is used
    (best-effort); otherwise the ledger is purely in-memory. Never raises.
    """
    if not enabled:
        return None
    path = None
    if persist:
        try:
            from runtime_paths import data_dir as _data_dir
            path = str(_data_dir() / "apex_consultations.jsonl")
        except Exception:  # noqa: BLE001 — fall back to in-memory only
            path = None
    try:
        return ConsultationLedger(max_records=max_records, persist_path=path)
    except Exception as exc:  # noqa: BLE001
        logger.debug("[consultation-ledger] build fault: {}", exc)
        return None


__all__ = ["ConsultationLedger", "build_consultation_ledger"]
