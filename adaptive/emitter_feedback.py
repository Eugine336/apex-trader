"""
APEX TRADER — Emitter Feedback Service

The signal ledger records and grades every directional read.  This service is
the read side every module can use to *ask its own question*: "of the signals I
emitted, how many were right — and does it matter whether they became trades or
got blocked?"

That last split is the whole point.  A module whose blocked signals are highly
accurate is being over-filtered by a gate; a gate that blocks mostly-correct
signals is destroying edge.  ``signal_value_when_blocked`` and
``get_gate_effectiveness`` surface exactly that, turning the ledger's raw rows
into the per-emitter and per-gate verdicts a later phase will act on.

Purely observational — reads the ledger, changes nothing live.

Leaf module — standard library + loguru only.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from loguru import logger

from adaptive.signal_ledger import SignalLedger


@dataclass
class EmitterFeedbackRequest:
    """A module's request for feedback on its own recent signals."""

    emitter: str
    pair: Optional[str] = None
    lookback: int = 100
    include_blocked: bool = True


@dataclass
class EmitterFeedbackResponse:
    """The comprehensive answer: accuracy split by traded vs blocked, context."""

    emitter: str
    total_signals: int = 0
    traded_signals: int = 0
    blocked_signals: int = 0
    accuracy_all: float = 0.0
    accuracy_traded: float = 0.0
    accuracy_blocked: float = 0.0
    accuracy_by_pair: dict = field(default_factory=dict)
    accuracy_by_gate: dict = field(default_factory=dict)
    common_correct_context: dict = field(default_factory=dict)
    common_wrong_context: dict = field(default_factory=dict)
    trade_outcomes: list = field(default_factory=list)
    # How often blocked signals were actually correct — a direct measure of gate
    # over-filtering for this emitter (1.0 = every blocked signal would have been
    # directionally right).
    signal_value_when_blocked: float = 0.0


def _accuracy(rows: List[dict]) -> float:
    if not rows:
        return 0.0
    correct = sum(1 for r in rows if r.get("direction_correct"))
    return round(correct / len(rows), 4)


def _context_frequencies(rows: List[dict], top: int = 8) -> dict:
    """Count flat scalar context values across rows for at-a-glance patterns.

    Only hashable scalar values are tallied (e.g. ``timeframe='M5'``,
    ``pattern='engulfing'``); nested structures are skipped.
    """
    counter: Counter = Counter()
    for r in rows:
        ctx = r.get("context") or {}
        if not isinstance(ctx, dict):
            continue
        for k, v in ctx.items():
            if isinstance(v, bool) or isinstance(v, (str, int)):
                counter[f"{k}={v}"] += 1
            elif isinstance(v, float):
                counter[f"{k}={round(v, 3)}"] += 1
    return dict(counter.most_common(top))


class EmitterFeedbackService:
    """Read-only feedback layer over the :class:`SignalLedger`."""

    def __init__(self, ledger: SignalLedger) -> None:
        self._ledger = ledger

    def request_feedback(
        self, request: EmitterFeedbackRequest,
    ) -> EmitterFeedbackResponse:
        """Answer one emitter's feedback request. Never raises — returns an
        empty response with the emitter name on any failure."""
        try:
            rows = self._ledger.get_graded_signals(
                emitter=request.emitter,
                pair=request.pair,
                lookback=request.lookback,
            )
            if not request.include_blocked:
                rows = [r for r in rows if not (
                    (not r.get("trade_opened")) and r.get("gate_blocked_by")
                )]

            traded = [r for r in rows if r.get("trade_opened")]
            blocked = [r for r in rows if (not r.get("trade_opened")) and r.get("gate_blocked_by")]
            correct_rows = [r for r in rows if r.get("direction_correct")]
            wrong_rows = [r for r in rows if r.get("direction_correct") is False]

            by_pair: Dict[str, list] = {}
            for r in rows:
                by_pair.setdefault(r.get("pair", ""), []).append(r)
            accuracy_by_pair = {p: _accuracy(rs) for p, rs in by_pair.items() if p}

            by_gate: Dict[str, list] = {}
            for r in blocked:
                by_gate.setdefault(r.get("gate_blocked_by", ""), []).append(r)
            accuracy_by_gate = {g: _accuracy(rs) for g, rs in by_gate.items() if g}

            trade_outcomes = [
                r.get("trade_outcome")
                for r in traded
                if r.get("trade_outcome")
            ]

            return EmitterFeedbackResponse(
                emitter=request.emitter,
                total_signals=len(rows),
                traded_signals=len(traded),
                blocked_signals=len(blocked),
                accuracy_all=_accuracy(rows),
                accuracy_traded=_accuracy(traded),
                accuracy_blocked=_accuracy(blocked),
                accuracy_by_pair=accuracy_by_pair,
                accuracy_by_gate=accuracy_by_gate,
                common_correct_context=_context_frequencies(correct_rows),
                common_wrong_context=_context_frequencies(wrong_rows),
                trade_outcomes=trade_outcomes,
                signal_value_when_blocked=_accuracy(blocked),
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("[EmitterFeedback] request_feedback failed: {}", exc)
            return EmitterFeedbackResponse(emitter=request.emitter)

    def get_all_emitter_summaries(
        self, lookback: int = 100,
    ) -> Dict[str, EmitterFeedbackResponse]:
        """Feedback responses for every emitter in the ledger."""
        try:
            accuracy_all = self._ledger.get_emitter_accuracy_all(lookback_trades=lookback)
            return {
                emitter: self.request_feedback(
                    EmitterFeedbackRequest(emitter=emitter, lookback=lookback)
                )
                for emitter in accuracy_all.keys()
            }
        except Exception as exc:  # noqa: BLE001
            logger.debug("[EmitterFeedback] get_all_emitter_summaries failed: {}", exc)
            return {}

    def get_gate_effectiveness(self, lookback: int = 500) -> dict:
        """Per-gate verdict: what % of the signals each gate blocked would have
        been directionally correct.

        High ``blocked_accuracy`` ⇒ the gate is rejecting profitable signals
        (suspect — too strict). Low ⇒ it is correctly filtering noise (earning
        its keep). Aggregated across all emitters.
        """
        try:
            rows = self._ledger.get_graded_signals(lookback=lookback)
        except Exception as exc:  # noqa: BLE001
            logger.debug("[EmitterFeedback] get_gate_effectiveness failed: {}", exc)
            return {}

        gates: Dict[str, list] = {}
        for r in rows:
            if (not r.get("trade_opened")) and r.get("gate_blocked_by"):
                gates.setdefault(r["gate_blocked_by"], []).append(r)

        out: dict = {}
        for gate, items in gates.items():
            correct = sum(1 for r in items if r.get("direction_correct"))
            n = len(items)
            out[gate] = {
                "gate": gate,
                "blocked": n,
                "would_have_been_correct": correct,
                "blocked_accuracy": round(correct / n, 4) if n else 0.0,
            }
        return out
