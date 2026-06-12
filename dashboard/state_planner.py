"""
APEX TRADER — Dashboard Trade Planner Mixin
Reads the plan journal (data/plan_journal.jsonl), joins plans to their
realised outcomes, and exposes recent plans plus aggregate stats
(action / entry-mode / SL / TP strategy distributions and their win rates).
"""

from __future__ import annotations

import json
import time as _time
from pathlib import Path

from loguru import logger


class PlannerMixin:
    """Reads the Trade Planner journal for dashboard display."""

    _PLAN_JOURNAL = Path("data/plan_journal.jsonl")
    _PLANNER_CACHE_TTL = 3.0

    def get_planner(self, limit: int = 50, symbol: str = "") -> dict:
        """Recent completed/open plans with optional symbol filter."""
        now = _time.monotonic()
        cache_key = f"{limit}:{symbol}"
        cached = getattr(self, "_planner_cache", None)
        cached_key = getattr(self, "_planner_cache_key", "")
        cached_ts = getattr(self, "_planner_cache_ts", 0.0)
        if cached and cached_key == cache_key and (now - cached_ts) < self._PLANNER_CACHE_TTL:
            return cached

        plans, outcomes = self._read_plan_journal()
        joined = self._join(plans, outcomes)

        if symbol:
            sym = symbol.upper()
            joined = [j for j in joined if j.get("symbol", "").upper() == sym]

        joined = joined[-limit:][::-1]  # newest first
        result = {
            "plans": joined,
            "stats": self._compute_stats(plans, outcomes),
            "total": len(joined),
        }
        self._planner_cache = result
        self._planner_cache_key = cache_key
        self._planner_cache_ts = now
        return result

    def get_planner_stats(self) -> dict:
        plans, outcomes = self._read_plan_journal()
        return self._compute_stats(plans, outcomes)

    # ── Internal ─────────────────────────────────────────────────────────

    def _read_plan_journal(self, max_records: int = 1000) -> tuple[dict, dict]:
        plans: dict[str, dict] = {}
        outcomes: dict[str, dict] = {}
        if not self._PLAN_JOURNAL.exists():
            return plans, outcomes
        try:
            lines = self._PLAN_JOURNAL.read_text(encoding="utf-8").strip().split("\n")
        except Exception as exc:
            logger.debug("[dashboard] plan journal read failed: {}", exc)
            return plans, outcomes

        for line in lines[-max_records:]:
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
                plans[pid] = rec
            elif rec.get("type") == "outcome":
                outcomes[pid] = rec
        return plans, outcomes

    @staticmethod
    def _join(plans: dict, outcomes: dict) -> list[dict]:
        rows: list[dict] = []
        for pid, prec in plans.items():
            plan = prec.get("plan", {})
            oc = outcomes.get(pid, {}).get("outcome", {})
            rows.append(
                {
                    "plan_id": pid,
                    "timestamp": prec.get("timestamp", ""),
                    "symbol": prec.get("symbol", ""),
                    "action": plan.get("action", ""),
                    "direction": plan.get("direction", ""),
                    "entry_mode": plan.get("entry_mode", ""),
                    "sl_strategy": plan.get("sl_strategy", ""),
                    "tp_strategy": plan.get("tp_strategy", ""),
                    "risk_pct": plan.get("risk_pct", 0.0),
                    "confidence": plan.get("confidence", 0.0),
                    "advisor_agreement": plan.get("advisor_agreement", 0.0),
                    "reasoning": plan.get("reasoning", ""),
                    "outcome": oc,
                }
            )
        return rows

    @staticmethod
    def _compute_stats(plans: dict, outcomes: dict) -> dict:
        def _bucket(field: str) -> dict:
            counts: dict[str, dict] = {}
            for pid, prec in plans.items():
                key = prec.get("plan", {}).get(field, "?")
                b = counts.setdefault(key, {"count": 0, "wins": 0, "completed": 0})
                b["count"] += 1
                oc = outcomes.get(pid, {}).get("outcome")
                if oc is not None:
                    b["completed"] += 1
                    try:
                        if float(oc.get("pnl_r", 0.0) or 0.0) > 0:
                            b["wins"] += 1
                    except (TypeError, ValueError):
                        pass
            for b in counts.values():
                b["win_rate"] = round(b["wins"] / b["completed"], 3) if b["completed"] else 0.0
            return counts

        total_plans = len(plans)
        total_completed = sum(1 for pid in plans if pid in outcomes)
        return {
            "total_plans": total_plans,
            "total_completed": total_completed,
            "action_counts": _bucket("action"),
            "entry_mode_stats": _bucket("entry_mode"),
            "sl_strategy_stats": _bucket("sl_strategy"),
            "tp_strategy_stats": _bucket("tp_strategy"),
        }
