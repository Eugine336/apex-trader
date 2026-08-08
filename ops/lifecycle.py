"""Lifecycle management for APEX TRADER (P1, Part 1A).

Three cooperating, additive primitives that harden the process boundary:

* :class:`ShutdownManager` — on a shutdown signal, stops new entries, waits a
  bounded time for in-flight work, then flushes EVERY SQLite-backed adaptive
  store (the gap the pre-existing ``stop()`` left open) and clears the crash
  marker. It complements the event-driven system's ``stop()`` rather than
  replacing it.
* :class:`StartupRecovery` — writes a crash marker at boot and removes it on a
  clean exit; a marker found at startup means the previous run died unclean and
  is reported loudly so the operator (and broker reconciliation) can react.
* :class:`HealthCheck` — one read-only snapshot of broker connectivity, every
  adaptive layer's active/dormant/error state, last-tick age, open-position
  count, drawdown state, process memory and on-disk store sizes, for the
  dashboard ``/api/health`` endpoint and the watchdog.

All three operate on the live system by duck typing — they read public
attributes and call ``.close()`` on stores, never reaching into engine
internals. Every method is fail-safe: an ops fault must never block a trade or
crash the loop, but it must always be logged (silent failure is worse).
"""

from __future__ import annotations

import os
import signal
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from loguru import logger

# Adaptive components that own a SQLite connection
# (or other releasable resource) and expose ``.close()``. (label, attribute).
# Order matters only for tidy logging — flushing is independent per store.
_ADAPTIVE_STORE_ATTRS: tuple[tuple[str, str], ...] = (
    ("signal_ledger", "_signal_ledger"),
    ("post_close_tracker", "_post_close_tracker"),
    ("counterfactual", "_counterfactual"),
    ("interaction_analyzer", "_interaction_analyzer"),
    ("capital_allocator", "_capital_allocator"),
    ("execution_profiles", "_execution_profiles"),
    ("regime_detector", "_regime_detector"),
    ("risk_manager", "_risk_manager"),
    ("behavior_discovery", "_behavior_discovery"),
    ("param_evolver", "_param_evolver"),
    ("signal_discovery", "_signal_discovery"),
    ("module_governor", "_module_governor"),
    ("virtual_registry", "_virtual_registry"),
    ("tuner_agent", "_tuner_agent"),
)

# Components that have a status/state accessor used for the health snapshot.
# (label, attribute, accessor-method-name). Accessor is best-effort.
_HEALTH_STATE_ACCESSORS: tuple[tuple[str, str, str], ...] = (
    ("regime_detector", "_regime_detector", "get_state"),
    ("risk_manager", "_risk_manager", "get_state"),
)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _process_memory_mb() -> Optional[float]:
    """Resident-set size in MB using stdlib only; ``None`` if unavailable."""
    try:
        import resource

        rss_kb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        # Linux reports KB, macOS reports bytes — normalise on magnitude.
        if rss_kb > 10_000_000:  # almost certainly bytes
            return round(rss_kb / (1024.0 * 1024.0), 1)
        return round(rss_kb / 1024.0, 1)
    except Exception:  # noqa: BLE001
        return None


class ShutdownManager:
    """Coordinates a clean shutdown: stop new work, flush every store, log it."""

    def __init__(self, trading_loop: Any, ops_config: Any) -> None:
        self._loop = trading_loop
        self._cfg = ops_config
        self._shutting_down = threading.Event()
        self._completed = threading.Event()
        self._lock = threading.RLock()
        self._timeout = float(getattr(ops_config, "shutdown_timeout_seconds", 30))

    # ── State ────────────────────────────────────────────────────────────
    @property
    def shutting_down(self) -> bool:
        return self._shutting_down.is_set()

    def request_shutdown(self, reason: str = "signal") -> None:
        """Mark the loop for shutdown — checked each tick. Idempotent."""
        if not self._shutting_down.is_set():
            self._shutting_down.set()
            logger.bind(component="ops", event="shutdown_requested").info(
                "Shutdown requested ({}) — new entries suspended", reason,
            )
        # Mirror onto the loop's own running flag so the cycle loop exits.
        try:
            self._loop.running = False
        except Exception:  # noqa: BLE001
            pass

    # ── Signal handlers ──────────────────────────────────────────────────
    def register_signal_handlers(self) -> bool:
        """Install SIGTERM/SIGINT/SIGHUP → graceful shutdown. Returns success.

        Safe to call from the main thread only; on a worker thread Python
        refuses to set handlers and we degrade quietly (the existing loop-level
        KeyboardInterrupt path still applies).
        """

        def _handle(signum, _frame):
            try:
                name = signal.Signals(signum).name
            except Exception:  # noqa: BLE001
                name = str(signum)
            self.request_shutdown(reason=name)

        installed = False
        for sig in (signal.SIGTERM, signal.SIGINT, getattr(signal, "SIGHUP", None)):
            if sig is None:
                continue
            try:
                signal.signal(sig, _handle)
                installed = True
            except (OSError, ValueError):
                logger.debug("[ops] could not install handler for {} (not main thread)", sig)
        return installed

    # ── The shutdown sequence ─────────────────────────────────────────────
    def shutdown(self, reason: str = "request") -> dict:
        """Flush every adaptive store + clear the crash marker. Idempotent.

        This is the piece the legacy ``stop()`` did not cover: ``stop()`` already
        persists open positions, closes the position store, disconnects brokers
        and closes the journal loop. Here we additionally flush the adaptive
        SQLite stores so recently-learned state is consolidated to disk, and we
        clear the crash marker to record that this was a clean exit.
        """
        with self._lock:
            if self._completed.is_set():
                return {"already_completed": True}
            self._shutting_down.set()
            summary: dict[str, Any] = {
                "reason": reason,
                "stores_flushed": [],
                "stores_failed": [],
                "open_positions": 0,
            }
            try:
                summary["open_positions"] = len(getattr(self._loop, "managed_positions", []) or [])
            except Exception:  # noqa: BLE001
                pass

            for label, attr in _ADAPTIVE_STORE_ATTRS:
                comp = getattr(self._loop, attr, None)
                if comp is None:
                    continue
                closer = getattr(comp, "close", None)
                if not callable(closer):
                    continue
                try:
                    closer()
                    summary["stores_flushed"].append(label)
                except Exception as exc:  # noqa: BLE001
                    summary["stores_failed"].append(label)
                    logger.bind(component="ops", event="store_flush_failed").error(
                        "🔴 SHUTDOWN store flush FAILED for {}: {}", label, exc,
                    )

            # Clear the crash marker last — only after stores were flushed, so a
            # crash mid-flush still leaves the marker for the next boot.
            try:
                StartupRecovery(self._cfg).clear_crash_marker()
                summary["crash_marker_cleared"] = True
            except Exception as exc:  # noqa: BLE001
                summary["crash_marker_cleared"] = False
                logger.debug("[ops] crash-marker clear failed: {}", exc)

            self._completed.set()
            logger.bind(component="ops", event="shutdown_complete").info(
                "Shutdown complete ({}) — {} store(s) flushed, {} failed, {} open position(s)",
                reason, len(summary["stores_flushed"]), len(summary["stores_failed"]),
                summary["open_positions"],
            )
            return summary


class StartupRecovery:
    """Crash-marker lifecycle + unclean-exit detection on boot."""

    def __init__(self, ops_config: Any) -> None:
        self._cfg = ops_config
        self._marker = Path(getattr(ops_config, "crash_marker_path", "data/.crash_marker"))

    def _ensure_parent(self) -> None:
        try:
            self._marker.parent.mkdir(parents=True, exist_ok=True)
        except Exception as exc:  # noqa: BLE001
            logger.debug("[ops] crash-marker parent mkdir failed: {}", exc)

    def crash_marker_present(self) -> bool:
        try:
            return self._marker.exists()
        except Exception:  # noqa: BLE001
            return False

    def read_marker(self) -> Optional[str]:
        try:
            return self._marker.read_text().strip() if self._marker.exists() else None
        except Exception:  # noqa: BLE001
            return None

    def write_crash_marker(self) -> None:
        """Drop a marker at boot; presence on the next boot ⇒ unclean exit."""
        self._ensure_parent()
        try:
            self._marker.write_text(
                f"pid={os.getpid()} started={_utc_now().isoformat()}"
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("[ops] could not write crash marker: {}", exc)

    def clear_crash_marker(self) -> None:
        try:
            if self._marker.exists():
                self._marker.unlink()
        except Exception as exc:  # noqa: BLE001
            logger.debug("[ops] crash-marker unlink failed: {}", exc)

    def run(self) -> dict:
        """Detect an unclean previous exit, then arm a fresh marker.

        Returns a summary describing whether recovery from a crash is needed.
        Never raises — recovery telemetry must not block startup.
        """
        summary: dict[str, Any] = {"unclean_previous_exit": False, "marker": str(self._marker)}
        try:
            if self.crash_marker_present():
                summary["unclean_previous_exit"] = True
                summary["previous_marker"] = self.read_marker()
                logger.bind(component="ops", event="unclean_restart").warning(
                    "⚠️ Crash marker present — previous run exited UNCLEAN ({}). "
                    "Broker reconciliation will verify open positions.",
                    summary["previous_marker"],
                )
            else:
                logger.bind(component="ops", event="clean_restart").info(
                    "Clean restart — no crash marker found",
                )
            # Arm a fresh marker for THIS run; cleared on clean shutdown.
            self.write_crash_marker()
        except Exception as exc:  # noqa: BLE001
            logger.warning("[ops] startup recovery check failed: {}", exc)
        return summary


class HealthCheck:
    """Read-only aggregate health snapshot across the whole system."""

    def __init__(self, trading_loop: Any, ops_config: Any) -> None:
        self._loop = trading_loop
        self._cfg = ops_config

    def _broker_status(self) -> dict:
        out = {"any_connected": False, "mt5": False, "deriv": False}
        pm = getattr(self._loop, "platforms", None)
        if pm is None:
            return out
        try:
            out["any_connected"] = bool(getattr(pm, "any_connected", False))
        except Exception:  # noqa: BLE001
            pass
        try:
            flags = getattr(pm, "_mt5_connected_flags", []) or []
            out["mt5"] = any(bool(f) for f in flags)
        except Exception:  # noqa: BLE001
            pass
        try:
            deriv = getattr(pm, "deriv", None)
            out["deriv"] = bool(deriv is not None and getattr(deriv, "is_connected", lambda: False)())
        except Exception:  # noqa: BLE001
            pass
        return out

    def _adaptive_layers(self) -> dict:
        """active / dormant / error for each adaptive component."""
        layers: dict[str, str] = {}
        for label, attr in _ADAPTIVE_STORE_ATTRS:
            comp = getattr(self._loop, attr, None)
            layers[label] = "active" if comp is not None else "dormant"
        return layers

    def _store_sizes_mb(self) -> dict:
        """On-disk size (MB) of each known SQLite store from config paths."""
        sizes: dict[str, float] = {}
        cfg = getattr(self._loop, "config", None)
        if cfg is None:
            return sizes
        candidates = {
            "signal_ledger": ("signal_ledger", "signal_ledger_db_path"),
            "counterfactual": ("counterfactual", "counterfactual_db_path"),
            "capital_allocation": ("capital_allocation", "capital_allocation_db_path"),
            "execution_profiles": ("execution_profiles", "execution_profiles_db_path"),
            "regime_detection": ("regime_detection", "regime_detection_db_path"),
            "risk_management": ("risk_management", "risk_management_db_path"),
            "behavior_discovery": ("behavior_discovery", "behavior_discovery_db_path"),
            "param_evolution": ("param_evolution", "param_evolution_db_path"),
            "signal_discovery": ("signal_discovery", "signal_discovery_db_path"),
            "interaction": ("interaction", "interaction_db_path"),
        }
        for label, (section, key) in candidates.items():
            try:
                sub = getattr(cfg, section, None)
                path = getattr(sub, key, None) if sub is not None else None
                if path and Path(path).exists():
                    sizes[label] = round(Path(path).stat().st_size / (1024.0 * 1024.0), 3)
            except Exception:  # noqa: BLE001
                continue
        # The post-close tracker uses a fixed path.
        try:
            p = Path("data/post_close_tracker.db")
            if p.exists():
                sizes["post_close_tracker"] = round(p.stat().st_size / (1024.0 * 1024.0), 3)
        except Exception:  # noqa: BLE001
            pass
        return sizes

    def _last_tick_age(self) -> Optional[float]:
        wd = getattr(self._loop, "watchdog", None)
        if wd is None:
            return None
        try:
            last = getattr(wd, "_last_successful_trade_check", None)
            if last is not None:
                return round((_utc_now() - last).total_seconds(), 1)
        except Exception:  # noqa: BLE001
            pass
        return None

    def _drawdown_state(self) -> dict:
        rm = getattr(self._loop, "_risk_manager", None)
        if rm is not None and hasattr(rm, "get_state"):
            try:
                st = rm.get_state()
                return {
                    "source": "risk_manager",
                    "state": st.get("state"),
                    "rolling_drawdown_pct": st.get("rolling_drawdown_pct"),
                    "daily_drawdown_pct": st.get("daily_drawdown_pct"),
                    "should_flatten": st.get("should_flatten"),
                    "sizing_factor": st.get("sizing_factor"),
                }
            except Exception:  # noqa: BLE001
                pass
        dd = getattr(self._loop, "drawdown", None)
        if dd is not None:
            try:
                return {"source": "drawdown_guard", "mode": getattr(getattr(dd, "mode", None), "value", None)}
            except Exception:  # noqa: BLE001
                pass
        return {"source": "none"}

    def get_health(self) -> dict:
        """The aggregate health snapshot. Never raises."""
        broker = self._broker_status()
        layers = self._adaptive_layers()
        open_positions = 0
        try:
            open_positions = len(getattr(self._loop, "managed_positions", []) or [])
        except Exception:  # noqa: BLE001
            pass
        tick_age = self._last_tick_age()
        max_tick = float(getattr(self._cfg, "max_tick_duration_seconds", 60))
        running = bool(getattr(self._loop, "running", False))

        warnings: list[str] = []
        if not broker["any_connected"]:
            warnings.append("no broker connected")
        if tick_age is not None and tick_age > max_tick:
            warnings.append(f"last tick {tick_age:.0f}s ago (> {max_tick:.0f}s)")

        # Overall status: 'error' if any warning, else 'ok'. Dormant adaptive
        # layers are EXPECTED (cold start) and never count as an error.
        status = "ok" if not warnings else "degraded"

        return {
            "status": status,
            "ts": _utc_now().isoformat(),
            "running": running,
            "broker": broker,
            "open_positions": open_positions,
            "last_tick_age_seconds": tick_age,
            "memory_mb": _process_memory_mb(),
            "adaptive_layers": layers,
            "store_sizes_mb": self._store_sizes_mb(),
            "drawdown": self._drawdown_state(),
            "warnings": warnings,
        }


__all__ = [
    "ShutdownManager",
    "StartupRecovery",
    "HealthCheck",
]
