"""APEX TRADER — Operational intelligence author (Constitution Part IX, Article 9).

After reasoning, the Brain may determine that an *operational* action should
occur — a recurring failure should become a GitHub issue, a significant discovery
should be surfaced to the operator, today's campaigns deserve a report. This
module turns such patterns (drawn from institutional memory and campaign
outcomes) into **objective requests** — the semantic vocabulary of Article 2.

It is deliberately Brain-side and dependency-light: it emits plain
:class:`OperationalIntent` records (stdlib only, no ``action`` import) so the
``cognition`` package stays natively importable. The Action Planner
(:mod:`action.planner`) consumes these — it reads the same field names via
duck-typing — selects a provider, and submits them through governed Composio.

The author holds **no market opinion** and never touches execution. It is
observational and default-off; every emission is throttled + de-duplicated so a
persistent condition produces one objective per cooldown, not a storm.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Optional

logger = logging.getLogger("apex.cognition.operations")

# Semantic capability names — must match action.capabilities (kept as literals so
# this module does not import the action layer and stays natively importable).
CAP_OPERATOR_NOTIFY = "operator.notify"
CAP_GITHUB_CREATE_ISSUE = "github.create_issue"
CAP_REPORT_PUBLISH = "report.publish"

# Post-mortem verdicts the author reacts to (mirror brain.campaign values).
VERDICT_DESERVED_LOSS = "deserved_loss"
VERDICT_VALIDATED = "validated"


@dataclass
class OperationalIntent:
    """A Brain-authored operational objective in semantic terms (Article 2).

    Field names mirror :class:`action.planner.ObjectiveRequest` so the Action
    Planner can consume an intent directly via duck-typing.
    """

    intent: str
    objective: str = ""
    params: dict = field(default_factory=dict)
    confidence: float = 0.6
    priority: int = 3
    source: str = "ai_brain"
    evidence_ref: str = ""
    preferred_provider: str = ""

    def to_dict(self) -> dict:
        return {
            "intent": self.intent,
            "objective": self.objective,
            "params": dict(self.params),
            "confidence": round(float(self.confidence), 4),
            "priority": int(self.priority),
            "source": self.source,
            "evidence_ref": self.evidence_ref,
        }


class OperationsAuthor:
    """Authors operational objectives from campaign outcomes + memory (Article 9).

    Feed realised outcomes via :meth:`observe_campaign_outcome`; call :meth:`tick`
    periodically to drain queued objectives plus the cadence-driven report. Every
    emission is de-duplicated within ``cooldown_seconds``. Fail-safe and pure —
    it emits intents; it never executes them.
    """

    def __init__(
        self,
        *,
        enabled: bool = False,
        notify_on_validated: bool = True,
        issue_on_repeated_loss: bool = True,
        loss_streak_threshold: int = 3,
        report_period_seconds: float = 86_400.0,
        cooldown_seconds: float = 3_600.0,
    ) -> None:
        self.enabled = bool(enabled)
        self.notify_on_validated = bool(notify_on_validated)
        self.issue_on_repeated_loss = bool(issue_on_repeated_loss)
        self.loss_streak_threshold = max(1, int(loss_streak_threshold))
        self.report_period_seconds = max(1.0, float(report_period_seconds))
        self.cooldown_seconds = max(0.0, float(cooldown_seconds))
        self._loss_streak: dict = {}          # (symbol,direction) → consecutive deserved losses
        self._pending: list = []              # queued OperationalIntent
        self._last_emit: dict = {}            # dedup key → epoch of last emission
        self._last_report_epoch = 0.0
        self._authored = 0
        self._suppressed = 0

    # ── Signals in ────────────────────────────────────────────────────────

    def observe_campaign_outcome(
        self,
        *,
        symbol: str,
        direction: str,
        verdict: str,
        reasoning_quality: float = 0.0,
        realized_pnl: float = 0.0,
        evidence_ref: str = "",
    ) -> None:
        """Fold one terminated-campaign outcome into the author's state. Fail-safe.

        A run of ``deserved_loss`` verdicts on the same book queues a GitHub issue
        (a recurring failure worth engineering attention); a ``validated`` win
        queues an operator notification (a significant, high-quality discovery).
        """
        if not self.enabled:
            return
        try:
            sym = str(symbol or "")
            d = str(direction or "").upper()
            key = (sym, d)
            v = str(verdict or "").strip().lower()
            if v == VERDICT_DESERVED_LOSS:
                self._loss_streak[key] = self._loss_streak.get(key, 0) + 1
                if (self.issue_on_repeated_loss
                        and self._loss_streak[key] >= self.loss_streak_threshold):
                    streak = self._loss_streak[key]
                    self._queue(OperationalIntent(
                        intent=CAP_GITHUB_CREATE_ISSUE,
                        objective=f"Recurring deserved losses on {sym} {d}",
                        params={
                            "title": f"[apex] recurring deserved losses: {sym} {d}",
                            "body": (f"{streak} consecutive campaigns on {sym} {d} closed as "
                                     f"'{VERDICT_DESERVED_LOSS}' (weak reasoning + loss). "
                                     "Investigate the evidence sources driving these entries."),
                            "labels": ["apex", "reasoning-quality"],
                        },
                        confidence=0.7, priority=2, evidence_ref=evidence_ref,
                    ), dedup_key=f"issue:{sym}:{d}")
                    self._loss_streak[key] = 0     # reset after raising
            else:
                # Any non-deserved-loss outcome breaks the streak.
                self._loss_streak[key] = 0
                if v == VERDICT_VALIDATED and self.notify_on_validated:
                    self._queue(OperationalIntent(
                        intent=CAP_OPERATOR_NOTIFY,
                        objective=f"Validated campaign on {sym} {d}",
                        params={"message": (f"Apex closed a VALIDATED campaign on {sym} {d} "
                                            f"(reasoning quality {float(reasoning_quality):.2f}, "
                                            f"pnl {float(realized_pnl):.2f}).")},
                        confidence=max(0.5, min(1.0, float(reasoning_quality) or 0.6)),
                        priority=4, evidence_ref=evidence_ref,
                    ), dedup_key=f"notify:{sym}:{d}")
        except Exception as exc:  # noqa: BLE001 — authoring must never raise
            logger.debug("[operations] observe_campaign_outcome fault: %s", exc)

    def _queue(self, intent: "OperationalIntent", *, dedup_key: str) -> None:
        intent.params.setdefault("_dedup_key", dedup_key)
        self._pending.append(intent)

    # ── Drain ──────────────────────────────────────────────────────────────

    def tick(self, *, now: Optional[float] = None,
             memory_status: Optional[dict] = None) -> list:
        """Return the operational objectives to submit now. Fail-safe.

        Emits the cadence-driven report (Article 9 — "today's campaigns require a
        report") when due, then drains queued outcome-driven objectives, honouring
        the per-key cooldown so a persistent condition emits once per window.
        """
        if not self.enabled:
            return []
        t = time.time() if now is None else float(now)
        out: list = []
        try:
            # Cadence report.
            if (t - self._last_report_epoch) >= self.report_period_seconds:
                if self._allow("report:cadence", t):
                    completed = int((memory_status or {}).get("completed_rows", 0) or 0)
                    out.append(OperationalIntent(
                        intent=CAP_REPORT_PUBLISH,
                        objective="Periodic operations report",
                        params={"summary": (f"Apex operations report: {completed} completed "
                                            "campaigns in institutional memory.")},
                        confidence=0.8, priority=4,
                    ))
                self._last_report_epoch = t
            # Queued outcome-driven objectives (dedup + cooldown).
            pending, self._pending = self._pending, []
            for intent in pending:
                key = str(intent.params.get("_dedup_key", intent.intent))
                if self._allow(key, t):
                    intent.params.pop("_dedup_key", None)
                    out.append(intent)
                else:
                    self._suppressed += 1
            self._authored += len(out)
        except Exception as exc:  # noqa: BLE001
            logger.debug("[operations] tick fault: %s", exc)
        return out

    def _allow(self, key: str, now: float) -> bool:
        """True when ``key`` has not emitted within the cooldown window.

        A never-seen key is always allowed (a missing entry must not be read as
        "emitted at epoch 0", which would wrongly suppress the first emission at
        ``now == 0``).
        """
        last = self._last_emit.get(key)
        if last is not None and (now - last) < self.cooldown_seconds:
            return False
        self._last_emit[key] = now
        return True

    def get_status(self) -> dict:
        return {
            "enabled": self.enabled,
            "authored": self._authored,
            "suppressed": self._suppressed,
            "pending": len(self._pending),
            "loss_streaks": {f"{s}:{d}": n for (s, d), n in self._loss_streak.items() if n},
        }


__all__ = ["OperationalIntent", "OperationsAuthor",
           "CAP_OPERATOR_NOTIFY", "CAP_GITHUB_CREATE_ISSUE", "CAP_REPORT_PUBLISH"]
