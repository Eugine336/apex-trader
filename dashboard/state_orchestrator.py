"""
APEX TRADER — Dashboard Orchestrator + Outcome-Feedback Mixin

Two more output producers made visible:

  * the **Orchestrator** (round table) — for every entry attempt it folds each
    stage's evidence into one bounded graded size multiplier (a dimmer, not a
    kill switch). Each ORCHESTRATOR_PROPOSAL event carries the full evidence
    proposal, every dimension's contribution, and the applied multiplier.
  * the **Outcome Feedback** loop — links each placed trade's realised R back to
    the modules / opportunity that drove it, yielding per-module, per-horizon
    accuracy and confidence calibration.

This mixin only reads + aggregates; it never makes or mutates a decision. The
orchestrator proposals come from the append-only event store; the feedback
accuracy comes straight off the live loop's ``OutcomeFeedback`` journal (or a
read-only journal view when idle).
"""

from __future__ import annotations

import json
import time as _time
from datetime import datetime, timezone
from typing import Any

from loguru import logger

from persistence.domain_events import ORCHESTRATOR_PROPOSAL


def _ts_iso(ts_ms: int) -> str:
    if not ts_ms:
        return ""
    try:
        return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).isoformat()
    except Exception:
        return str(ts_ms)


class OrchestratorMixin:
    """get_orchestrator() + get_outcome_feedback() for the dashboard panels."""

    _ORCH_CACHE_TTL = 3.0

    def _get_event_store_safe(self):
        try:
            from persistence.event_store import get_event_store

            return get_event_store()
        except Exception as exc:
            logger.debug("[state_orchestrator] event store unavailable: {}", exc)
            return None

    # ── Orchestrator proposals ──────────────────────────────────────────────
    def get_orchestrator(self, limit: int = 100, symbol: str = "") -> dict:
        """Recent orchestrator proposals + aggregate size/dimension stats."""
        now = _time.monotonic()
        cache_key = f"{limit}:{symbol}"
        if (
            getattr(self, "_orch_cache_key", None) == cache_key
            and (now - getattr(self, "_orch_cache_ts", 0.0)) < self._ORCH_CACHE_TTL
        ):
            return self._orch_cache

        store = self._get_event_store_safe()
        proposals: list[dict] = []
        if store is not None:
            try:
                rows = store.query_events(
                    severity_min="DEBUG",
                    event_types=[ORCHESTRATOR_PROPOSAL],
                    symbol=symbol or None,
                    limit=max(1, min(limit, 500)),
                )
            except Exception as exc:
                logger.debug("[state_orchestrator] query failed: {}", exc)
                rows = []
            for row in rows:
                pj = row.get("payload_json")
                if not pj:
                    continue
                try:
                    payload = json.loads(pj) if isinstance(pj, str) else pj
                except Exception:
                    continue
                payload["_event_ts"] = _ts_iso(row.get("ts_utc_ms", 0))
                proposals.append(payload)

        result = {
            "proposals": proposals[:limit],
            "total": len(proposals),
            "stats": self._compute_orchestrator_stats(proposals),
            "config": self._orchestrator_config(),
            "source": "event_store",
        }
        self._orch_cache = result
        self._orch_cache_key = cache_key
        self._orch_cache_ts = now
        return result

    def _orchestrator_config(self) -> dict:
        ed = getattr(self, "_event_driven_system", None)
        cfg = getattr(getattr(ed, "_config", None), "orchestrator", None) if ed is not None else None
        return {
            "enabled": bool(getattr(cfg, "enabled", False)),
            "apply_sizing": bool(getattr(cfg, "apply_sizing", False)),
            "size_floor": float(getattr(cfg, "size_floor", 0.0) or 0.0),
        }

    def _compute_orchestrator_stats(self, proposals: list[dict]) -> dict:
        total = len(proposals)
        vetoed = 0
        applied = 0
        size_sum = 0.0
        size_cnt = 0
        # dimension → [sum, count] for the average per-dimension dim factor.
        dim_acc: dict[str, list] = {}
        for p in proposals:
            if p.get("vetoed"):
                vetoed += 1
            if p.get("applied"):
                applied += 1
            try:
                size_sum += float(p.get("size_multiplier", 0.0) or 0.0)
                size_cnt += 1
            except (TypeError, ValueError):
                pass
            for d in p.get("dimensions") or []:
                name = d.get("name", "")
                if not name:
                    continue
                acc = dim_acc.setdefault(name, [0.0, 0])
                try:
                    acc[0] += float(d.get("multiplier", 1.0) or 1.0)
                    acc[1] += 1
                except (TypeError, ValueError):
                    pass
        dimensions = [
            {"name": n, "avg_multiplier": round(s / c, 4) if c else 0.0, "count": c}
            for n, (s, c) in dim_acc.items()
        ]
        # Lowest average multiplier first — the dimensions dragging size down most.
        dimensions.sort(key=lambda d: d["avg_multiplier"])
        return {
            "total": total,
            "vetoed": vetoed,
            "applied": applied,
            "avg_size_multiplier": round(size_sum / size_cnt, 4) if size_cnt else 0.0,
            "dimensions": dimensions,
        }

    # ── Outcome feedback ─────────────────────────────────────────────────────
    def get_outcome_feedback(self) -> dict:
        """Per-module / per-horizon accuracy + calibration over closed trades."""
        fb = self._outcome_feedback_obj()
        if fb is None:
            return {
                "modules": [], "horizons": [], "total_trades": 0,
                "overall_win_rate": 0.0, "overall_avg_r": 0.0,
                "source": "idle",
            }
        try:
            acc = fb.module_accuracy()
        except Exception as exc:
            logger.debug("[state_orchestrator] module_accuracy failed: {}", exc)
            acc = {"modules": [], "horizons": [], "total_trades": 0,
                   "overall_win_rate": 0.0, "overall_avg_r": 0.0}
        acc["source"] = "live" if self.is_live else "journal"
        return acc

    def _outcome_feedback_obj(self):
        """The system's OutcomeFeedback, or a read-only journal view."""
        ctx = getattr(self, "_system_context", None)
        fb = getattr(ctx, "outcome_feedback", None) if ctx is not None else None
        if fb is not None:
            return fb
        # Idle / not attached — read the default journal so the panel still works.
        try:
            from brain.outcome_feedback import OutcomeFeedback

            return OutcomeFeedback(None)
        except Exception as exc:
            logger.debug("[state_orchestrator] outcome feedback view unavailable: {}", exc)
            return None
