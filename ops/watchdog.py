"""Process watchdog for APEX TRADER (P1, Part 1C).

A lightweight, internal heartbeat + stall detector — NOT the Datadog Watchdog.
It complements the existing in-process :class:`platforms.health_watchdog.HealthWatchdog`
(which tracks scan / trade-check freshness) by adding the *process-boundary*
signals an external supervisor (systemd ``WatchdogSec``, a Docker ``HEALTHCHECK``,
or k8s liveness probe) needs:

* a **heartbeat file** whose mtime + contents advance every tick, so an external
  process can tell a live loop from a hung one without touching the API, and
* a **stall check** that flags when the gap since the last completed tick exceeds
  ``max_tick_duration_seconds`` and logs it CRITICAL once per stall.

It deliberately does nothing destructive (no ``kill``, no restart) — detection
and reporting only. Acting on a stall is the operator's / supervisor's job.

Everything is fail-safe: a heartbeat write error is logged and swallowed; the
watchdog must never be able to take down the trading loop.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from loguru import logger


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class ProcessWatchdog:
    """Heartbeat writer + tick-stall detector (detect & report only)."""

    def __init__(self, ops_config: Any) -> None:
        self._cfg = ops_config
        self._heartbeat_path = Path(getattr(ops_config, "heartbeat_file", "data/.heartbeat"))
        self._interval = float(getattr(ops_config, "heartbeat_interval_seconds", 10))
        self._max_tick = float(getattr(ops_config, "max_tick_duration_seconds", 60))
        self._last_beat_mono: float = 0.0
        self._last_tick_mono: float = time.monotonic()
        self._stall_logged: bool = False
        self._beats: int = 0
        try:
            self._heartbeat_path.parent.mkdir(parents=True, exist_ok=True)
        except Exception as exc:  # noqa: BLE001
            logger.debug("[ops] heartbeat parent mkdir failed: {}", exc)

    # ── Heartbeat ─────────────────────────────────────────────────────────
    def beat(self, force: bool = False) -> None:
        """Advance the heartbeat file if the interval has elapsed.

        Called once per tick; the interval throttle keeps disk writes cheap on
        a fast (5s) scan cadence. ``force=True`` writes unconditionally.
        """
        now = time.monotonic()
        if not force and (now - self._last_beat_mono) < self._interval:
            return
        self._last_beat_mono = now
        self._beats += 1
        payload = {
            "ts": _utc_iso(),
            "pid": os.getpid(),
            "beats": self._beats,
            "seconds_since_tick": round(now - self._last_tick_mono, 1),
        }
        try:
            tmp = self._heartbeat_path.with_suffix(self._heartbeat_path.suffix + ".tmp")
            tmp.write_text(json.dumps(payload))
            os.replace(tmp, self._heartbeat_path)  # atomic
        except Exception as exc:  # noqa: BLE001
            logger.debug("[ops] heartbeat write failed: {}", exc)

    # ── Tick tracking ─────────────────────────────────────────────────────
    def record_tick(self) -> None:
        """Mark a completed tick — resets the stall timer."""
        self._last_tick_mono = time.monotonic()
        if self._stall_logged:
            logger.bind(component="ops", event="tick_recovered").info(
                "Tick recovered — loop is progressing again",
            )
        self._stall_logged = False

    def check_stall(self) -> Optional[float]:
        """Return the stall age (s) if the loop has exceeded the tick budget.

        Logs CRITICAL once per stall episode; returns ``None`` when healthy.
        """
        age = time.monotonic() - self._last_tick_mono
        if age > self._max_tick:
            if not self._stall_logged:
                logger.bind(component="ops", event="tick_stall").critical(
                    "🚨 TICK STALL — no completed tick for {:.0f}s (budget {:.0f}s)",
                    age, self._max_tick,
                )
                self._stall_logged = True
            return round(age, 1)
        return None

    def get_state(self) -> dict:
        return {
            "heartbeat_file": str(self._heartbeat_path),
            "beats": self._beats,
            "interval_seconds": self._interval,
            "max_tick_duration_seconds": self._max_tick,
            "seconds_since_tick": round(time.monotonic() - self._last_tick_mono, 1),
            "stalled": self._stall_logged,
        }


__all__ = ["ProcessWatchdog"]
