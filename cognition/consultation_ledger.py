"""APEX TRADER — Consultation Ledger (Constitution Part XXI Art 9/10/11).

The Advisory Council is only institutional if the Brain *remembers* whom it
consulted and *grades* each advisor continuously. Crucially, the council is a
panel of INDEPENDENT COGNITIVE CONTRIBUTORS, never a directional voting bloc
(Part XVII Art 7 / Part XXIV): an advisor is valued for the QUALITY of its
reasoning, not for siding with the crowd. This ledger therefore grades advisors
on their thinking, never on agreement with a majority direction.

* **Consultation records (Art 11).** Every time the Reasoning Orchestrator fans
  a query out to the council, one compact record is kept — the symbol, the
  reasoning capability requested, which advisors were asked, which replied, and
  for each reply its *cognitive contribution* (a short thesis summary, thesis
  richness, whether it named a concrete opportunity, its calibration discipline,
  and whether it offered an independent perspective), plus how many DISTINCT
  perspectives the panel produced and the market uncertainty at the time. No
  majority direction and no "dispersion from the majority" are computed or
  stored — those are voting artefacts. These records are institutional memory
  (optionally appended to a JSONL file), never thrown away.

* **Per-advisor scorecards (Art 9/10).** From those records the ledger keeps a
  rolling scorecard per advisor graded on REASONING QUALITY: reply rate, average
  confidence and latency, thesis quality (how complete/structured its analysis
  is), opportunity-identification rate (did it surface a concrete, actionable
  opportunity), calibration discipline (did it express a multidimensional
  confidence profile and acknowledge what would invalidate it), and useful-
  dissent rate (how often it contributed a genuinely independent thesis rather
  than echoing peers). A per-capability breakdown lets the Brain learn *which
  advisor is strong in which domain* (Art 10) — strategic vs risk vs rapid
  inference, etc.

This is a *record and measurement* layer, not authority: it never reasons,
selects, votes, or decides — that remains the Reasoning Orchestrator (selection)
and the one Cognitive Brain (synthesis + decision). Outcome-based grading by
realised P&L is already handled by the Phase VIII influence ledger; this ledger
captures the *consultation itself* and each advisor's cognitive contribution,
which the influence layer cannot see.

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

def _f(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _clamp01(v: Any) -> float:
    return max(0.0, min(1.0, _f(v)))


def _cognition_of(op: Any) -> dict:
    """The advisor's FULL structured reasoning, or ``{}`` — never a vote."""
    cog = getattr(op, "cognition", None)
    return cog if isinstance(cog, dict) else {}


# The structured fields that make an advisor's thesis COMPLETE (Part XXV). Thesis
# quality is how much of this analysis the advisor actually produced — never
# whether it pointed a particular direction.
_THESIS_TEXT_FIELDS = (
    "regime", "primary_hypothesis", "opportunity", "opportunity_horizon",
    "invalidation", "key_uncertainty", "expected_favorable_excursion",
    "expected_adverse_excursion",
)
_THESIS_LIST_FIELDS = (
    "alternative_hypotheses", "what_would_change_my_mind", "supporting_evidence",
)
_CONF_DIMENSIONS = (
    "thesis_confidence", "opportunity_confidence", "timing_confidence",
    "execution_confidence",
)
_NO_OPPORTUNITY = {"", "none", "n/a", "na", "no opportunity", "untradeable"}


def _thesis_quality(cog: dict) -> float:
    """Completeness of the advisor's structured analysis, in ``[0, 1]``.

    Rewards a rich, multi-dimensional thesis (regime, hypotheses, opportunity +
    horizon, excursions, invalidation, uncertainties) — the hallmark of an
    independent thinker — with NO regard to its directional conclusion.
    """
    if not cog:
        return 0.0
    present = sum(1 for k in _THESIS_TEXT_FIELDS if str(cog.get(k, "") or "").strip())
    for k in _THESIS_LIST_FIELDS:
        v = cog.get(k)
        if isinstance(v, list) and any(str(i).strip() for i in v):
            present += 1
    total = len(_THESIS_TEXT_FIELDS) + len(_THESIS_LIST_FIELDS)
    return round(present / total, 4) if total else 0.0


def _identified_opportunity(cog: dict) -> bool:
    """True when the advisor surfaced a concrete, actionable opportunity."""
    if not cog:
        return False
    opp = str(cog.get("opportunity", "") or "").strip().lower()
    if opp and opp not in _NO_OPPORTUNITY:
        return True
    opps = cog.get("opportunities")
    return isinstance(opps, list) and any(isinstance(o, dict) and o for o in opps)


def _calibration(cog: dict) -> float:
    """Calibration DISCIPLINE at consultation time, in ``[0, 1]``.

    Realised-outcome calibration is the influence ledger's job; here we measure
    the *intellectual honesty* observable in the reply itself: a multidimensional
    confidence profile (not one bald scalar) and an explicit acknowledgement of
    what it is unsure of / what would prove it wrong.
    """
    if not cog:
        return 0.0
    score = 0.0
    if any(isinstance(cog.get(k), (int, float)) and not isinstance(cog.get(k), bool)
           for k in _CONF_DIMENSIONS):
        score += 0.5
    if str(cog.get("key_uncertainty", "") or "").strip() or \
            str(cog.get("invalidation", "") or "").strip():
        score += 0.3
    for k in ("what_would_change_my_mind", "missing_information"):
        v = cog.get(k)
        if isinstance(v, list) and any(str(i).strip() for i in v):
            score += 0.2
            break
    return round(min(1.0, score), 4)


def _norm_token(v: Any) -> str:
    return " ".join(str(v or "").strip().lower().split()[:6])


def _thesis_fingerprint(cog: dict) -> str:
    """A direction-free signature of an advisor's thesis (regime + hypothesis +
    opportunity), used only to measure independent perspective — never a vote."""
    return "|".join((
        _norm_token(cog.get("regime")),
        _norm_token(cog.get("primary_hypothesis")),
        _norm_token(cog.get("opportunity")),
    ))


def _thesis_summary(cog: dict, rationale: str = "", limit: int = 160) -> str:
    """A short, readable thesis line for institutional memory (not a vote)."""
    parts = []
    regime = str(cog.get("regime", "") or "").strip()
    if regime:
        parts.append(regime)
    primary = str(cog.get("primary_hypothesis", "") or "").strip()
    if primary:
        parts.append(primary)
    opp = str(cog.get("opportunity", "") or "").strip()
    if opp:
        parts.append(f"opportunity: {opp}")
    if not parts and rationale:
        parts.append(str(rationale).strip())
    return (" · ".join(parts))[:limit]


class _AdvisorScore:
    """Rolling reasoning-quality stats for one advisor (overall + per capability).

    Graded on the QUALITY of the advisor's thinking — thesis completeness,
    opportunity identification, calibration discipline and useful dissent —
    never on agreement with a council majority (Part XVII Art 7). Mutable.
    """

    __slots__ = ("consulted", "replies", "sum_conf", "sum_latency",
                 "sum_thesis_quality", "opportunities", "sum_calibration",
                 "useful_dissents", "by_capability")

    def __init__(self) -> None:
        self.consulted = 0
        self.replies = 0
        self.sum_conf = 0.0
        self.sum_latency = 0.0
        self.sum_thesis_quality = 0.0     # Σ thesis completeness in [0,1]
        self.opportunities = 0            # replies naming a concrete opportunity
        self.sum_calibration = 0.0        # Σ calibration discipline in [0,1]
        self.useful_dissents = 0          # replies with an independent thesis
        # capability -> dict(consulted, replies, sum_thesis_quality,
        #                    opportunities, sum_calibration, useful_dissents)
        self.by_capability: dict = {}

    def observe(self, *, replied: bool, confidence: float, latency_ms: float,
                thesis_quality: float, identified_opportunity: bool,
                calibration: float, useful_dissent: bool, capability: str) -> None:
        self.consulted += 1
        cap = self.by_capability.setdefault(
            capability or "",
            {"consulted": 0, "replies": 0, "sum_thesis_quality": 0.0,
             "opportunities": 0, "sum_calibration": 0.0, "useful_dissents": 0})
        cap["consulted"] += 1
        if replied:
            self.replies += 1
            self.sum_conf += _clamp01(confidence)
            self.sum_latency += max(0.0, latency_ms)
            self.sum_thesis_quality += _clamp01(thesis_quality)
            self.sum_calibration += _clamp01(calibration)
            cap["replies"] += 1
            cap["sum_thesis_quality"] += _clamp01(thesis_quality)
            cap["sum_calibration"] += _clamp01(calibration)
            if identified_opportunity:
                self.opportunities += 1
                cap["opportunities"] += 1
            if useful_dissent:
                self.useful_dissents += 1
                cap["useful_dissents"] += 1

    def to_dict(self) -> dict:
        replies = self.replies
        caps = {
            k: {
                "consulted": v["consulted"],
                "replies": v["replies"],
                "thesis_quality": (round(v["sum_thesis_quality"] / v["replies"], 4)
                                   if v["replies"] else None),
                "opportunity_rate": (round(v["opportunities"] / v["replies"], 4)
                                     if v["replies"] else None),
                "calibration": (round(v["sum_calibration"] / v["replies"], 4)
                                if v["replies"] else None),
                "useful_dissent_rate": (round(v["useful_dissents"] / v["replies"], 4)
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
            "thesis_quality": round(self.sum_thesis_quality / replies, 4) if replies else None,
            "opportunity_rate": round(self.opportunities / replies, 4) if replies else None,
            "calibration": round(self.sum_calibration / replies, 4) if replies else None,
            "useful_dissent_rate": round(self.useful_dissents / replies, 4) if replies else None,
            "by_capability": caps,
        }


def _perspectives(opinions: list) -> int:
    """Count DISTINCT thesis fingerprints among replies (cognitive diversity).

    A measure of how many genuinely independent perspectives the panel produced
    — the opposite of a majority vote. One replier ⇒ one perspective.
    """
    seen = {_thesis_fingerprint(_cognition_of(o)) for o in opinions}
    return len(seen)


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

            # Index replies by engine for O(1) lookup while walking consulted.
            replied: dict = {}
            for o in opinions:
                eng = str(getattr(o, "engine", "") or "").strip()
                if eng:
                    replied[eng] = o

            # Precompute each reply's cognitive contribution (never a vote). A
            # thesis fingerprint counts how many repliers share the SAME regime/
            # hypothesis/opportunity read, so an advisor with a unique thesis is
            # credited with useful, independent dissent — the opposite of grading
            # by agreement with a majority direction.
            metrics: dict = {}
            fp_counts: dict = {}
            for eng, o in replied.items():
                cog = _cognition_of(o)
                fp = _thesis_fingerprint(cog)
                fp_counts[fp] = fp_counts.get(fp, 0) + 1
                metrics[eng] = {
                    "cog": cog,
                    "fp": fp,
                    "thesis_quality": _thesis_quality(cog),
                    "identified_opportunity": _identified_opportunity(cog),
                    "calibration": _calibration(cog),
                    "confidence": _clamp01(getattr(o, "confidence", 0.0)),
                    "latency_ms": _f(getattr(o, "latency_ms", 0.0)),
                    "rationale": str(getattr(o, "rationale", "") or ""),
                }

            n_replied = len(replied)

            def _is_useful_dissent(m: dict) -> bool:
                # An independent perspective: a substantive thesis that no peer
                # duplicated. Meaningless with a single replier.
                return (n_replied >= 2 and m["thesis_quality"] > 0.0
                        and fp_counts.get(m["fp"], 0) == 1)

            ts = _f(now) if now is not None else self._clock()
            unc = None if uncertainty is None else round(max(0.0, min(1.0, _f(uncertainty))), 4)
            op_rows = []
            with self._lock:
                self._total += 1
                # Grade every advisor that was ASKED (reply or not) — on the
                # quality of its reasoning, never on siding with the crowd.
                names = consulted or list(replied.keys())
                for name in names:
                    score = self._advisors.setdefault(name, _AdvisorScore())
                    m = metrics.get(name)
                    if m is not None:
                        score.observe(
                            replied=True, confidence=m["confidence"],
                            latency_ms=m["latency_ms"],
                            thesis_quality=m["thesis_quality"],
                            identified_opportunity=m["identified_opportunity"],
                            calibration=m["calibration"],
                            useful_dissent=_is_useful_dissent(m),
                            capability=cap,
                        )
                    else:
                        score.observe(
                            replied=False, confidence=0.0, latency_ms=0.0,
                            thesis_quality=0.0, identified_opportunity=False,
                            calibration=0.0, useful_dissent=False, capability=cap,
                        )
                opportunities_identified = 0
                sum_quality = 0.0
                for o in opinions:
                    eng = str(getattr(o, "engine", "") or "")
                    m = metrics.get(eng, {})
                    cog = m.get("cog", _cognition_of(o))
                    identified = bool(m.get("identified_opportunity", False))
                    quality = float(m.get("thesis_quality", 0.0))
                    opportunities_identified += 1 if identified else 0
                    sum_quality += quality
                    op_rows.append({
                        "engine": eng,
                        "thesis": _thesis_summary(cog, m.get("rationale", "")),
                        "thesis_quality": round(quality, 4),
                        "identified_opportunity": identified,
                        "calibration": round(float(m.get("calibration", 0.0)), 4),
                        "useful_dissent": _is_useful_dissent(m) if m else False,
                        "confidence": round(_clamp01(getattr(o, "confidence", 0.0)), 4),
                        "latency_ms": round(_f(getattr(o, "latency_ms", 0.0)), 1),
                    })
                rec = {
                    "ts": round(ts, 3),
                    "symbol": symbol,
                    "capability": cap,
                    "consulted": names,
                    "replies": len(op_rows),
                    # Cognitive-contribution aggregates — NOT a majority/vote.
                    "perspectives": _perspectives(opinions),
                    "opportunities_identified": opportunities_identified,
                    "avg_thesis_quality": (round(sum_quality / len(op_rows), 4)
                                           if op_rows else None),
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
