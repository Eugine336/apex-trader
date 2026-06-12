"""
Outcome Logger — append-only journal that links every trade plan to its
realised outcome, producing the dataset the `Calibrator` learns from.

On entry it records the full `TradePlan` + `TradePlanContext`.  On close it
links the realised R-multiple, P&L and exit reason back to the plan by id.
Storage is JSONL (one record per line) for cheap streaming and crash safety.

Leaf module — standard library + loguru only.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from loguru import logger

from planning.models import TradePlan, TradePlanContext


class OutcomeLogger:
    """Persists plan→outcome pairs to ``data/plan_journal.jsonl``."""

    def __init__(self, journal_path: str = "data/plan_journal.jsonl") -> None:
        self._path = Path(journal_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)

    # ── Writing ──────────────────────────────────────────────────────────

    def log_plan(self, plan: TradePlan, context: TradePlanContext) -> None:
        """Record a plan that resulted in an ENTER decision."""
        record = {
            "type": "plan",
            "plan_id": plan.plan_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "symbol": context.symbol,
            "plan": plan.to_dict(),
            "context": context.to_dict(),
        }
        self._append(record)

    def log_outcome(self, plan_id: str, outcome: dict) -> None:
        """Link a realised outcome to a previously-logged plan."""
        if not plan_id:
            return
        record = {
            "type": "outcome",
            "plan_id": plan_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "outcome": outcome,
        }
        self._append(record)

    def _append(self, record: dict) -> None:
        try:
            with self._path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, default=str) + "\n")
        except Exception as exc:
            logger.warning("[OutcomeLogger] write failed: {}", exc)

    # ── Reading ──────────────────────────────────────────────────────────

    def get_completed_trades(self, lookback: int = 200) -> list[dict]:
        """Return the most recent completed plan+outcome pairs.

        Joins ``plan`` and ``outcome`` records by ``plan_id`` and returns only
        those with both halves present, newest last.
        """
        plans: dict[str, dict] = {}
        outcomes: dict[str, dict] = {}
        order: list[str] = []

        if not self._path.exists():
            return []

        try:
            lines = self._path.read_text(encoding="utf-8").strip().split("\n")
        except Exception as exc:
            logger.warning("[OutcomeLogger] read failed: {}", exc)
            return []

        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            pid = rec.get("plan_id")
            if not pid:
                continue
            if rec.get("type") == "plan":
                if pid not in plans:
                    order.append(pid)
                plans[pid] = rec
            elif rec.get("type") == "outcome":
                outcomes[pid] = rec

        completed: list[dict] = []
        for pid in order:
            if pid in plans and pid in outcomes:
                completed.append(
                    {
                        "plan_id": pid,
                        "plan": plans[pid].get("plan", {}),
                        "context": plans[pid].get("context", {}),
                        "outcome": outcomes[pid].get("outcome", {}),
                    }
                )

        if lookback > 0:
            completed = completed[-lookback:]
        return completed

    def completed_count(self) -> int:
        return len(self.get_completed_trades(lookback=0))
