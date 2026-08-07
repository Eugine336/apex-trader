"""APEX TRADER — Dashboard Operations Mixin (P3 — ops control room).

Assembles a single unified snapshot of the live system for the Operations page:
health, drawdown, equity curve, open positions, risk events, regime map,
exposure breakdown, per-layer pulse, governor actions, watchdog, and the
per-component tick-latency profile (P4).

It is purely a *composer* — it reads from the same read-only accessors the
existing panels already use (``get_ops_health``, the risk manager / regime
detector ``get_state()``, the open-trades mixin, the module-governor mixin, and
the tick profiler) and never mutates the loop. Every section is individually
guarded so one missing component never breaks the whole endpoint, and a stable
shape is returned when the loop is detached so the frontend never breaks.
"""

from __future__ import annotations

from typing import Any

from loguru import logger


def _r(x: Any, n: int = 2) -> float:
    try:
        return round(float(x), n)
    except (TypeError, ValueError):
        return 0.0


class OperationsMixin:
    """get_operations() — the unified ops control-room snapshot."""

    # ── Component handles (all optional, all read-only) ──────────────────────
    def _ops_loop(self) -> Any:
        return None

    def _ops_risk_manager(self) -> Any:
        ctx = getattr(self, "_system_context", None)
        if ctx is not None and getattr(ctx, "risk_engine", None) is not None:
            return ctx.risk_engine
        return getattr(self._ops_loop(), "_risk_manager", None)

    def _ops_regime_detector(self) -> Any:
        ctx = getattr(self, "_system_context", None)
        if ctx is not None and getattr(ctx, "regime_detector", None) is not None:
            return ctx.regime_detector
        return getattr(self._ops_loop(), "_regime_detector", None)

    # ── Aggregate ────────────────────────────────────────────────────────────
    def get_operations(self) -> dict:
        """Every operational signal the control room needs, in one payload."""
        if not self.is_live:
            return {
                "source": "idle",
                "health": {"status": "ok", "ops_enabled": False, "attached": False},
                "drawdown": {},
                "equity_curve": [],
                "open_positions": [],
                "risk_events": [],
                "regime_map": [],
                "exposure": {},
                "layer_status": {},
                "governor_actions": [],
                "watchdog": {},
                "performance": {"enabled": False, "tick": {}, "components": [], "slow_ticks": [], "recommendations": []},
            }
        return {
            "source": "live",
            "health": self._safe_ops(self._ops_health_snapshot),
            "drawdown": self._safe_ops(self._ops_drawdown),
            "equity_curve": self._safe_ops(self._ops_equity_curve, default=[]),
            "open_positions": self._safe_ops(self._ops_open_positions, default=[]),
            "risk_events": self._safe_ops(self._ops_risk_events, default=[]),
            "regime_map": self._safe_ops(self._ops_regime_map, default=[]),
            "exposure": self._safe_ops(self._ops_exposure),
            "layer_status": self._safe_ops(self._ops_layer_status),
            "governor_actions": self._safe_ops(self._ops_governor_actions, default=[]),
            "watchdog": self._safe_ops(self._ops_watchdog_state),
            "performance": self._safe_ops(self._ops_performance),
        }

    @staticmethod
    def _safe_ops(fn, default: Any = None):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001
            logger.debug("[state_operations] {} failed: {}", getattr(fn, "__name__", fn), exc)
            return {} if default is None else default

    # ── Health ───────────────────────────────────────────────────────────────
    def _ops_health_snapshot(self) -> dict:
        loop = self._ops_loop()
        getter = getattr(loop, "get_ops_health", None)
        if callable(getter):
            return getter() or {}
        # Event-driven mode (loop detached): reuse the ED/ctx-aware health
        # snapshot so the ops page sources its health from the event-driven
        # system instead of going dark.
        health_getter = getattr(self, "get_health", None)
        if callable(health_getter):
            try:
                return health_getter() or {}
            except Exception as exc:  # noqa: BLE001
                logger.debug("[state_operations] ED health read failed: {}", exc)
        return {"status": "ok", "ops_enabled": False, "attached": True}

    # ── Drawdown ───────────────────────────────────────────────────────────────
    def _ops_drawdown(self) -> dict:
        rm = self._ops_risk_manager()
        if rm is None or not hasattr(rm, "get_state"):
            return {"source": "none"}
        st = rm.get_state() or {}
        limits = st.get("limits", {}) or {}
        return {
            "source": "risk_manager",
            "enabled": bool(st.get("enabled", False)),
            "state": str(st.get("state", "")),
            "current_daily_pct": _r(st.get("daily_drawdown_pct", 0.0), 3),
            "current_rolling_pct": _r(st.get("rolling_drawdown_pct", 0.0), 3),
            "daily_limit": _r(limits.get("daily_drawdown_limit_pct", 0.0), 3),
            "rolling_limit": _r(limits.get("rolling_drawdown_limit_pct", 0.0), 3),
            "hard_stop_limit": _r(limits.get("hard_stop_drawdown_pct", 0.0), 3),
            "peak_equity": _r(st.get("peak_equity", 0.0), 2),
            "current_equity": _r(st.get("current_equity", 0.0), 2),
            "sizing_factor": _r(st.get("sizing_factor", 1.0), 4),
            "should_flatten": bool(st.get("should_flatten", False)),
            "cooldown_until": st.get("cooldown_until"),
        }

    def _ops_equity_curve(self) -> list:
        rm = self._ops_risk_manager()
        if rm is None or not hasattr(rm, "get_state"):
            return []
        st = rm.get_state() or {}
        curve = []
        for pt in st.get("equity_curve", []) or []:
            curve.append({
                "equity": _r(pt.get("equity", 0.0), 2),
                "pnl": _r(pt.get("pnl", 0.0), 2),
                "ts": pt.get("ts"),
            })
        return curve

    def _ops_risk_events(self) -> list:
        rm = self._ops_risk_manager()
        if rm is None or not hasattr(rm, "get_state"):
            return []
        st = rm.get_state() or {}
        events = []
        for e in st.get("risk_events", []) or []:
            events.append({
                "pair": str(e.get("pair", "")),
                "rule": str(e.get("rule", "")),
                "reason": str(e.get("reason", "")),
                "ts": e.get("ts"),
            })
        return events

    # ── Open positions (reuse the trades mixin) ───────────────────────────────
    def _ops_open_positions(self) -> list:
        getter = getattr(self, "get_open_trades", None)
        if not callable(getter):
            return []
        result = getter() or {}
        return result.get("trades", []) or []

    # ── Regime map ─────────────────────────────────────────────────────────────
    def _ops_regime_map(self) -> list:
        det = self._ops_regime_detector()
        if det is None or not hasattr(det, "get_state"):
            return []
        st = det.get_state() or {}
        pairs = []
        for p in st.get("pairs", []) or []:
            pairs.append({
                "pair": str(p.get("pair", "")),
                "regime": str(p.get("regime", "")),
                "confidence": _r(p.get("confidence", 0.0), 4),
                "duration_sec": _r(p.get("duration_sec", 0.0), 1),
            })
        pairs.sort(key=lambda x: x["pair"])
        return pairs

    # ── Exposure breakdown (derived from open positions + risk limits) ─────────
    def _ops_exposure(self) -> dict:
        positions = self._ops_open_positions()
        per_pair: dict[str, int] = {}
        per_direction: dict[str, int] = {"LONG": 0, "SHORT": 0}
        for t in positions:
            pair = str(t.get("instrument", "")) or "?"
            per_pair[pair] = per_pair.get(pair, 0) + 1
            direction = str(t.get("direction", "")).upper()
            if direction in per_direction:
                per_direction[direction] += 1

        # Map positions to their current regime for a per-regime breakdown.
        regime_by_pair = {r["pair"]: r["regime"] for r in self._ops_regime_map()}
        per_regime: dict[str, int] = {}
        for t in positions:
            reg = regime_by_pair.get(str(t.get("instrument", "")), "UNKNOWN") or "UNKNOWN"
            per_regime[reg] = per_regime.get(reg, 0) + 1

        total = len(positions)
        longs = per_direction["LONG"]
        shorts = per_direction["SHORT"]
        directional_pct = (abs(longs - shorts) / total * 100.0) if total else 0.0

        max_positions = 0
        rm = self._ops_risk_manager()
        if rm is not None and hasattr(rm, "get_state"):
            try:
                limits = (rm.get_state() or {}).get("limits", {}) or {}
                max_positions = int(limits.get("max_simultaneous_positions", 0) or 0)
            except Exception:  # noqa: BLE001
                max_positions = 0

        return {
            "total_positions": total,
            "max_positions": max_positions,
            "long_positions": longs,
            "short_positions": shorts,
            "directional_pct": _r(directional_pct, 1),
            "per_pair": per_pair,
            "per_direction": per_direction,
            "per_regime": per_regime,
        }

    # ── Per-layer pulse ────────────────────────────────────────────────────────
    def _ops_layer_status(self) -> dict:
        health = self._ops_health_snapshot()
        layers = health.get("adaptive_layers", {}) or {}
        detail = health.get("adaptive_layer_detail", {}) or {}
        store_sizes = health.get("store_sizes_mb", {}) or {}
        out: dict[str, dict] = {}
        for name, state in layers.items():
            info = detail.get(name, {}) or {}
            out[name] = {
                "state": str(state),
                "store_mb": _r(store_sizes.get(name, 0.0), 3),
                "reason": str(info.get("reason", "")),
                "enabled": info.get("enabled"),
            }
        return out

    # ── Governor actions (reuse the module-governor mixin transitions) ─────────
    def _ops_governor_actions(self) -> list:
        getter = getattr(self, "get_module_governor", None)
        if not callable(getter):
            return []
        data = getter(limit=50) or {}
        transitions = data.get("transitions", []) or []
        out = []
        for t in transitions[:30]:
            out.append({
                "module": str(t.get("module", t.get("name", ""))),
                "from_state": str(t.get("from_state", t.get("old_state", ""))),
                "to_state": str(t.get("to_state", t.get("new_state", ""))),
                "reason": str(t.get("reason", "")),
                "ts": t.get("ts") or t.get("timestamp"),
            })
        return out

    # ── Watchdog ─────────────────────────────────────────────────────────────
    def _ops_watchdog_state(self) -> dict:
        health = self._ops_health_snapshot()
        wd = health.get("watchdog", {}) or {}
        return {
            "heartbeat_file": wd.get("heartbeat_file"),
            "beats": int(wd.get("beats", 0) or 0),
            "interval_seconds": _r(wd.get("interval_seconds", 0.0), 1),
            "max_tick_duration_seconds": _r(wd.get("max_tick_duration_seconds", 0.0), 1),
            "seconds_since_tick": _r(wd.get("seconds_since_tick", 0.0), 1),
            "stalled": bool(wd.get("stalled", False)),
            "last_tick_age_seconds": health.get("last_tick_age_seconds"),
            "running": bool(health.get("running", False)),
            "memory_mb": health.get("memory_mb"),
        }

    # ── Tick-latency profile (P4) ──────────────────────────────────────────────
    def _ops_performance(self) -> dict:
        loop = self._ops_loop()
        getter = getattr(loop, "get_tick_profile", None)
        if callable(getter):
            return getter() or {}
        # Event-driven mode: read the position-eval latency profile the ED
        # system now exposes.
        ed = getattr(self, "_event_driven_system", None)
        ed_getter = getattr(ed, "get_tick_profile", None) if ed is not None else None
        if callable(ed_getter):
            try:
                return ed_getter() or {}
            except Exception as exc:  # noqa: BLE001
                logger.debug("[state_operations] ED tick profile read failed: {}", exc)
        return {"enabled": False, "tick": {}, "components": [], "slow_ticks": [], "recommendations": []}
