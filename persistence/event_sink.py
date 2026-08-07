"""
APEX TRADER — Loguru → EventStore Tee Sink + stdlib Intercept
Captures every loguru record as a structured LOG event in the EventStore.
Also bridges stdlib-logging outliers (rl/bridge, brain/regime_detector) into
loguru so their output is captured identically.

Non-blocking: events are enqueued, never written inline.
Fail-safe: if the store is unavailable the trading system keeps running.
"""

from __future__ import annotations

import logging
from typing import Optional

from loguru import logger

from persistence.event_store import get_event_store

# ── Severity mapping (loguru level name → canonical) ──────────────────────────

_LEVEL_MAP = {
    "TRACE": "DEBUG",
    "DEBUG": "DEBUG",
    "INFO": "INFO",
    "SUCCESS": "INFO",
    "WARNING": "WARNING",
    "ERROR": "ERROR",
    "CRITICAL": "CRITICAL",
}


# ── Loguru sink ───────────────────────────────────────────────────────────────

_sink_error_count = 0


def event_store_sink(message) -> None:
    """Loguru sink that tees every record into the EventStore as a LOG event.

    Bound extras (``correlation_id``, ``symbol``, ``source_module``) propagate
    into the event row when present.  The full formatted message and any extra
    bindings are stored in payload_json.
    """
    global _sink_error_count
    record = message.record
    extra = record.get("extra", {})

    severity = _LEVEL_MAP.get(record["level"].name, "INFO")

    payload = {"message": record["message"]}
    if extra:
        safe = {}
        for k, v in extra.items():
            try:
                safe[k] = v
            except Exception:
                safe[k] = str(v)
        payload["extra"] = safe

    if record["exception"] is not None:
        exc_info = record["exception"]
        if exc_info.type is not None:
            payload["exception"] = f"{exc_info.type.__name__}: {exc_info.value}"

    try:
        store = get_event_store()
        store.emit(
            event_type="LOG",
            severity=severity,
            symbol=extra.get("symbol"),
            correlation_id=extra.get("correlation_id"),
            source_module=extra.get("source_module") or record["name"],
            payload=payload,
        )
    except Exception:
        _sink_error_count += 1


def get_sink_error_count() -> int:
    return _sink_error_count


# ── stdlib → loguru intercept handler ─────────────────────────────────────────


class _LoguruInterceptHandler(logging.Handler):
    """Route stdlib logging records into loguru so they hit the tee sink.

    Targets the two known outliers: ``rl/bridge.py`` (``apex.rl.bridge``)
    and ``brain/regime_detector.py`` (``apex`` root logger at DEBUG).
    """

    def emit(self, record: logging.LogRecord) -> None:
        try:
            level = logger.level(record.levelname).name
        except ValueError:
            level = record.levelno

        frame = logging.currentframe()
        depth = 2
        while frame and frame.f_code.co_filename == logging.__file__:
            frame = frame.f_back
            depth += 1

        logger.opt(depth=depth, exception=record.exc_info).log(level, "{}", record.getMessage())


def install_stdlib_intercept(logger_names: Optional[list] = None) -> None:
    """Attach the intercept handler to the given stdlib loggers.

    Defaults to the two known outliers if *logger_names* is not specified.
    """
    if logger_names is None:
        logger_names = ["apex.rl.bridge", "apex"]

    handler = _LoguruInterceptHandler()
    for name in logger_names:
        stdlib_logger = logging.getLogger(name)
        stdlib_logger.handlers = [handler]
        stdlib_logger.setLevel(logging.DEBUG)
        stdlib_logger.propagate = False
