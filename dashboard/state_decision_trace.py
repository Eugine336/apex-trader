"""
APEX TRADER — Dashboard Decision Trace Mixin (Phase 4 — pipeline awareness)
Reads DECISION_TRACE events from the append-only event store and exposes the
full pipeline-awareness picture to the dashboard: per-setup traces, the stage
funnel (where opportunities survive / die), rejection breakdown by gate, the
challenge feed (downstream disagreeing with upstream), and per-stage confidence.

Every trace is the complete chain of stage verdicts the trading loop stamped as
one opportunity flowed scan → entry, plus any challenges raised. This mixin only
reads + aggregates; it never makes a decision.
"""

from __future__ import annotations

import json
import time as _time
from datetime import datetime, timezone
from typing import Any, Optional

from loguru import logger

from brain.decision_trace import PIPELINE_STAGES, OUTCOME_TRADE_PLACED
from persistence.domain_events import DECISION_TRACE


def _ts_iso(ts_ms: int) -> str:
    if not ts_ms:
        return ""
    try:
        return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).isoformat()
    except Exception:
        return str(ts_ms)


class DecisionTraceMixin:
    """Reads + aggregates DECISION_TRACE events for the dashboard panels."""

    _DTRACE_CACHE_TTL = 3.0

    def _get_event_store_safe(self):
        try:
            from persistence.event_store import get_event_store

            return get_event_store()
        except Exception as exc:
            logger.debug("[state_decision_trace] event store unavailable: {}", exc)
            return None

    def _read_traces(self, limit: int = 500, symbol: str = "") -> list[dict]:
        """Pull the most recent DECISION_TRACE payloads (newest-first)."""
        store = self._get_event_store_safe()
        if store is None:
            return []
        try:
            rows = store.query_events(
                severity_min="DEBUG",
                event_types=[DECISION_TRACE],
                symbol=symbol or None,
                limit=max(1, min(limit, 500)),
            )
        except Exception as exc:
            logger.debug("[state_decision_trace] query failed: {}", exc)
            return []

        traces: list[dict] = []
        for row in rows:
            pj = row.get("payload_json")
            if not pj:
                continue
            try:
                payload = json.loads(pj) if isinstance(pj, str) else pj
            except Exception:
                continue
            payload["_event_ts"] = _ts_iso(row.get("ts_utc_ms", 0))
            payload.setdefault("correlation_id", row.get("correlation_id"))
            traces.append(payload)
        return traces

    def get_decision_traces(self, limit: int = 100, symbol: str = "") -> dict:
        """Recent decision traces (full stage chains) for the feed / per-pair view."""
        now = _time.monotonic()
        cache_key = f"{limit}:{symbol}"
        if (
            getattr(self, "_dtrace_cache_key", None) == cache_key
            and (now - getattr(self, "_dtrace_cache_ts", 0.0)) < self._DTRACE_CACHE_TTL
        ):
            return self._dtrace_cache

        traces = self._read_traces(limit=max(limit, 200), symbol=symbol)
        result = {
            "traces": traces[:limit],
            "total": len(traces),
            "stats": self._compute_trace_stats(traces),
            "source": "event_store",
        }
        self._dtrace_cache = result
        self._dtrace_cache_key = cache_key
        self._dtrace_cache_ts = now
        return result

    def get_decision_trace_stats(self) -> dict:
        """Aggregated stats only (funnel, rejections, challenges, confidence)."""
        return self._compute_trace_stats(self._read_traces(limit=500))

    def _compute_trace_stats(self, traces: list[dict]) -> dict:
        total = len(traces)
        # Funnel: how many traces reached (stamped) each pipeline stage.
        funnel = {stage: 0 for stage in PIPELINE_STAGES}
        # Rejection breakdown: count + sample reasons per rejecting stage.
        rejections: dict[str, dict] = {}
        outcomes: dict[str, int] = {}
        challenges: list[dict] = []
        # Per-stage confidence accumulation.
        conf_sum: dict[str, float] = {s: 0.0 for s in PIPELINE_STAGES}
        conf_cnt: dict[str, int] = {s: 0 for s in PIPELINE_STAGES}
        placed = 0

        for tr in traces:
            stages = tr.get("stages") or []
            seen_stages = set()
            for sv in stages:
                stage = sv.get("stage", "")
                if stage in funnel:
                    if stage not in seen_stages:
                        funnel[stage] += 1
                    seen_stages.add(stage)
                    try:
                        conf_sum[stage] += float(sv.get("confidence", 0.0) or 0.0)
                        conf_cnt[stage] += 1
                    except (TypeError, ValueError):
                        pass

            outcome = tr.get("final_outcome", "") or "UNKNOWN"
            outcomes[outcome] = outcomes.get(outcome, 0) + 1
            if outcome == OUTCOME_TRADE_PLACED:
                placed += 1

            rej_stage = tr.get("rejected_at")
            if rej_stage:
                bucket = rejections.setdefault(rej_stage, {"count": 0, "reasons": []})
                bucket["count"] += 1
                reason = tr.get("final_reason", "")
                if reason and len(bucket["reasons"]) < 5:
                    bucket["reasons"].append(reason)

            for ch in tr.get("challenges") or []:
                challenges.append({
                    "pair": tr.get("pair", ""),
                    "trace_id": tr.get("trace_id", ""),
                    "challenger": ch.get("challenger", ""),
                    "target_stage": ch.get("target_stage", ""),
                    "reason": ch.get("reason", ""),
                    "timestamp": tr.get("_event_ts", ""),
                })

        confidence = {
            s: round(conf_sum[s] / conf_cnt[s], 3)
            for s in PIPELINE_STAGES
            if conf_cnt[s] > 0
        }

        funnel_list = [{"stage": s, "count": funnel[s]} for s in PIPELINE_STAGES]
        rejection_list = sorted(
            ({"stage": k, **v} for k, v in rejections.items()),
            key=lambda r: r["count"],
            reverse=True,
        )

        return {
            "total_traces": total,
            "trades_placed": placed,
            "funnel": funnel_list,
            "outcomes": outcomes,
            "rejections": rejection_list,
            "challenges": challenges[:50],
            "challenge_count": len(challenges),
            "confidence": confidence,
        }
