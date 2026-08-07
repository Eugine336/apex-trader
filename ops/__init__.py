"""APEX TRADER — Operations & Production Hardening (P1).

A thin, additive operations layer that makes the live system safe to run and
observe in production WITHOUT changing any trading logic:

* :class:`~ops.lifecycle.ShutdownManager` — flushes every SQLite-backed adaptive
  store and persists in-memory state on a shutdown signal, so a SIGTERM/SIGINT
  never loses learning state mid-flight.
* :class:`~ops.lifecycle.StartupRecovery` — detects an unclean previous exit via
  a crash-marker file and reports it loudly on the next boot.
* :class:`~ops.lifecycle.HealthCheck` — aggregates the status of every component
  (broker, adaptive layers, drawdown state, store sizes) into one snapshot for
  the dashboard ``/api/health`` endpoint.
* :class:`~ops.watchdog.ProcessWatchdog` — heartbeat file + stall detection so an
  external supervisor (systemd/Docker) can tell a hung tick from a healthy one.
* :func:`~ops.logging_config.configure_structured_logging` — optional JSON,
  component-tagged logging with rotation and separate trade / risk audit logs.

Everything here is gated behind :class:`~config.OpsConfig`. When ``ops.enabled``
is ``False`` the layer is inert and the live startup / shutdown path is
byte-for-byte the pre-existing behaviour. Silent failure is treated as worse
than loud failure — every ops action logs what it did and what it could not do.
"""

from ops.lifecycle import (  # noqa: F401
    HealthCheck,
    ShutdownManager,
    StartupRecovery,
)
from ops.watchdog import ProcessWatchdog  # noqa: F401
from ops.tick_profiler import TickProfiler  # noqa: F401
from ops.logging_config import configure_structured_logging  # noqa: F401

__all__ = [
    "HealthCheck",
    "ShutdownManager",
    "StartupRecovery",
    "ProcessWatchdog",
    "TickProfiler",
    "configure_structured_logging",
]
