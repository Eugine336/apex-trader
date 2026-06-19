"""
APEX TRADER — Dashboard Health Mixin (P1 — ops / production hardening)

Exposes the aggregate health snapshot produced by the ops layer
(:class:`ops.lifecycle.HealthCheck`) for the ``/api/health`` endpoint: broker
connectivity, every adaptive layer's active/dormant state, last-tick age, open
positions, drawdown state, process memory, on-disk store sizes, and the process
watchdog heartbeat.

Read-only — it never mutates the loop. Returns a stable shape even when the loop
is detached or the ops layer is disabled so the endpoint never 500s.
"""

from __future__ import annotations

from typing import Any

from loguru import logger


# (health label, SystemContext attribute) — mirrors ops.lifecycle adaptive layers.
_ED_ADAPTIVE_LAYERS: tuple[tuple[str, str], ...] = (
    ("signal_ledger", "signal_ledger"),
    ("post_close_tracker", "post_close_tracker"),
    ("counterfactual", "counterfactual_engine"),
    ("interaction_analyzer", "interaction_analyzer"),
    ("capital_allocator", "capital_allocator"),
    ("execution_profiles", "execution_profiles"),
    ("regime_detector", "regime_detector"),
    ("risk_engine", "risk_engine"),
    ("behavior_discovery", "behavior_discovery"),
    ("signal_discovery", "signal_discovery"),
    ("module_governor", "module_governor"),
    ("virtual_registry", "virtual_module_registry"),
    ("tuner_agent", "tuner_agent"),
    ("vote_calibrator", "vote_calibrator"),
    ("gate_tuner", "gate_tuner"),
    ("shadow_store", "shadow_store"),
    ("ml_adapter", "ml_adapter"),
)

# (label, config section, db-path key) — mirrors ops.lifecycle store sizes.
_ED_STORE_PATHS: tuple[tuple[str, str, str], ...] = (
    ("signal_ledger", "signal_ledger", "signal_ledger_db_path"),
    ("counterfactual", "counterfactual", "counterfactual_db_path"),
    ("capital_allocation", "capital_allocation", "capital_allocation_db_path"),
    ("execution_profiles", "execution_profiles", "execution_profiles_db_path"),
    ("regime_detection", "regime_detection", "regime_detection_db_path"),
    ("risk_management", "risk_management", "risk_management_db_path"),
    ("behavior_discovery", "behavior_discovery", "behavior_discovery_db_path"),
    ("param_evolution", "param_evolution", "param_evolution_db_path"),
    ("signal_discovery", "signal_discovery", "signal_discovery_db_path"),
    ("interaction", "interaction", "interaction_db_path"),
)


def _ed_adaptive_layers(ctx: Any) -> dict:
    """active/dormant per adaptive component, read from the SystemContext."""
    layers: dict[str, str] = {}
    if ctx is None:
        return layers
    for label, attr in _ED_ADAPTIVE_LAYERS:
        layers[label] = "active" if getattr(ctx, attr, None) is not None else "dormant"
    return layers


def _ed_store_sizes_mb(cfg: Any) -> dict:
    """On-disk size (MB) of each known SQLite store from config paths."""
    from pathlib import Path

    sizes: dict[str, float] = {}
    if cfg is None:
        return sizes
    for label, section, key in _ED_STORE_PATHS:
        try:
            sub = getattr(cfg, section, None)
            path = getattr(sub, key, None) if sub is not None else None
            if path and Path(path).exists():
                sizes[label] = round(Path(path).stat().st_size / (1024.0 * 1024.0), 3)
        except Exception:  # noqa: BLE001
            continue
    try:
        p = Path("data/post_close_tracker.db")
        if p.exists():
            sizes["post_close_tracker"] = round(p.stat().st_size / (1024.0 * 1024.0), 3)
    except Exception:  # noqa: BLE001
        pass
    return sizes


class HealthMixin:
    """get_health() — aggregate system health for /api/health."""

    def get_health(self) -> dict:
        ed = getattr(self, "_event_driven_system", None)
        if ed is not None:
            return self._ed_health()

        loop = getattr(self, "_trading_loop", None)
        if loop is None:
            return {
                "status": "ok",
                "ops_enabled": False,
                "attached": False,
                "running": False,
            }
        getter = getattr(loop, "get_ops_health", None)
        if not callable(getter):
            return {"status": "ok", "ops_enabled": False, "attached": True}
        try:
            snap = getter()
            snap.setdefault("attached", True)
            return snap
        except Exception as exc:  # noqa: BLE001
            logger.debug("[state_health] health read failed: {}", exc)
            return {"status": "error", "ops_enabled": True, "attached": True, "error": str(exc)}

    def _ed_health(self) -> dict:
        """Health snapshot for event-driven mode."""
        from datetime import datetime, timezone

        ed = self._event_driven_system
        ctx = getattr(self, "_system_context", None)
        pm = getattr(self, "_platform_manager", None)

        broker = {"any_connected": False, "mt5": False, "deriv": False}
        if pm is not None:
            try:
                broker["any_connected"] = bool(getattr(pm, "any_connected", False))
                flags = getattr(pm, "_mt5_connected_flags", []) or []
                broker["mt5"] = any(bool(f) for f in flags)
                deriv = getattr(pm, "deriv", None)
                broker["deriv"] = bool(deriv is not None and getattr(deriv, "is_connected", lambda: False)())
            except Exception:
                pass

        open_positions = 0
        try:
            positions = pm.get_all_open_positions() if pm else []
            open_positions = len(positions) if positions else 0
        except Exception:
            pass

        warnings: list[str] = []
        if not broker["any_connected"]:
            warnings.append("no broker connected")

        watchdog_state = None
        if ctx is not None and ctx.process_watchdog is not None:
            try:
                watchdog_state = ctx.process_watchdog.get_state()
                if watchdog_state.get("stalled"):
                    warnings.append("tick stall detected")
            except Exception:
                pass

        drawdown = {"source": "none"}
        if ctx is not None and ctx.drawdown_guard is not None:
            try:
                dd = ctx.drawdown_guard.get_status(datetime.now(timezone.utc))
                drawdown = {"source": "drawdown_guard", "mode": str(getattr(dd, "mode", "NORMAL"))}
            except Exception:
                pass

        status = "ok" if not warnings else "degraded"
        stats = ed.stats() if hasattr(ed, "stats") else {}
        cfg = getattr(ed, "_config", None)

        return {
            "status": status,
            "ts": datetime.now(timezone.utc).isoformat(),
            "running": bool(getattr(ed, "is_running", False)),
            "mode": "event_driven",
            "broker": broker,
            "open_positions": open_positions,
            "drawdown": drawdown,
            "watchdog": watchdog_state,
            "ed_stats": stats,
            "adaptive_layers": _ed_adaptive_layers(ctx),
            "store_sizes_mb": _ed_store_sizes_mb(cfg),
            "last_tick_age_seconds": (watchdog_state or {}).get("seconds_since_tick"),
            "warnings": warnings,
            "attached": True,
            "ops_enabled": True,
        }
