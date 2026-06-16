"""
APEX TRADER — Dashboard Position Health Mixin

The orchestrator's live-management round table (Phase 8) grades every OPEN
position into a continuous health score and a bounded management action each
cycle — replacing the old argmax management collapse. Every evaluation emits a
``POSITION_HEALTH`` event carrying the re-evaluated evidence, the per-dimension
health multipliers, the overall score, the graded action and how it changed
since entry.

This mixin only reads + aggregates those events from the append-only event
store; it never makes or mutates a management decision. It powers the Position
Health page: the open-position health table, per-position health-over-time, the
dimension breakdown and the management action log.
"""

from __future__ import annotations

import json
import time as _time
from datetime import datetime, timezone

from loguru import logger

from persistence.domain_events import POSITION_HEALTH


def _ts_iso(ts_ms: int) -> str:
    if not ts_ms:
        return ""
    try:
        return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).isoformat()
    except Exception:
        return str(ts_ms)


class PositionHealthMixin:
    """get_position_health() for the dashboard's live-management panel."""

    _PH_CACHE_TTL = 3.0

    def _get_event_store_safe(self):
        # Reused by sibling mixins; defined defensively so this mixin also works
        # standalone in tests.
        try:
            from persistence.event_store import get_event_store

            return get_event_store()
        except Exception as exc:
            logger.debug("[state_position_health] event store unavailable: {}", exc)
            return None

    def get_position_health(self, limit: int = 200, symbol: str = "") -> dict:
        """Recent position-health evaluations + per-position series and stats."""
        now = _time.monotonic()
        cache_key = f"{limit}:{symbol}"
        if (
            getattr(self, "_ph_cache_key", None) == cache_key
            and (now - getattr(self, "_ph_cache_ts", 0.0)) < self._PH_CACHE_TTL
        ):
            return self._ph_cache

        store = self._get_event_store_safe()
        reports: list[dict] = []
        if store is not None:
            try:
                rows = store.query_events(
                    severity_min="DEBUG",
                    event_types=[POSITION_HEALTH],
                    symbol=symbol or None,
                    limit=max(1, min(limit, 1000)),
                )
            except Exception as exc:
                logger.debug("[state_position_health] query failed: {}", exc)
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
                reports.append(payload)

        result = {
            "reports": reports[:limit],
            "total": len(reports),
            "positions": self._group_by_position(reports),
            "stats": self._compute_health_stats(reports),
            "config": self._position_health_config(),
            "source": "event_store",
        }
        self._ph_cache = result
        self._ph_cache_key = cache_key
        self._ph_cache_ts = now
        return result

    def _position_health_config(self) -> dict:
        cfg = (
            getattr(getattr(self._trading_loop, "config", None), "orchestrator", None)
            if self.is_live else None
        )
        thresholds = getattr(cfg, "health_thresholds", None)
        return {
            "manage_open_positions": bool(getattr(cfg, "manage_open_positions", False)),
            "allow_scale_up": bool(getattr(cfg, "allow_scale_up", False)),
            "thresholds": thresholds if isinstance(thresholds, dict) else {},
        }

    def _group_by_position(self, reports: list[dict]) -> list[dict]:
        """Latest health per open position + its health-over-time series."""
        by_oid: dict[str, dict] = {}
        for r in reports:
            oid = str(r.get("order_id") or r.get("pair") or "")
            if not oid:
                continue
            grp = by_oid.setdefault(oid, {
                "order_id": r.get("order_id", ""),
                "pair": r.get("pair", ""),
                "direction": r.get("direction", ""),
                "horizon": r.get("horizon", ""),
                "series": [],
            })
            grp["series"].append({
                "ts": r.get("_event_ts", ""),
                "health_score": r.get("health_score", 0.0),
                "action": r.get("action", ""),
                "profit_r": r.get("profit_r", 0.0),
            })
        positions = []
        for grp in by_oid.values():
            series = grp["series"]
            latest = series[-1] if series else {}
            positions.append({
                **{k: grp[k] for k in ("order_id", "pair", "direction", "horizon")},
                "health_score": latest.get("health_score", 0.0),
                "action": latest.get("action", ""),
                "profit_r": latest.get("profit_r", 0.0),
                "evaluations": len(series),
                "series": series[-60:],
            })
        positions.sort(key=lambda p: p.get("health_score", 0.0))
        return positions

    def _compute_health_stats(self, reports: list[dict]) -> dict:
        total = len(reports)
        action_counts: dict[str, int] = {}
        health_sum = 0.0
        health_cnt = 0
        dim_acc: dict[str, list] = {}
        for r in reports:
            action = str(r.get("action", "") or "")
            if action:
                action_counts[action] = action_counts.get(action, 0) + 1
            try:
                health_sum += float(r.get("health_score", 0.0) or 0.0)
                health_cnt += 1
            except (TypeError, ValueError):
                pass
            for name, mult in (r.get("dimension_scores") or {}).items():
                acc = dim_acc.setdefault(name, [0.0, 0])
                try:
                    acc[0] += float(mult)
                    acc[1] += 1
                except (TypeError, ValueError):
                    pass
        dimensions = [
            {"name": n, "avg_multiplier": round(s / c, 4) if c else 0.0, "count": c}
            for n, (s, c) in dim_acc.items()
        ]
        dimensions.sort(key=lambda d: d["avg_multiplier"])
        return {
            "total": total,
            "avg_health": round(health_sum / health_cnt, 4) if health_cnt else 0.0,
            "action_counts": action_counts,
            "dimensions": dimensions,
        }
