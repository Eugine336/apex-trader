"""Structured, component-tagged logging for APEX TRADER (P1, Part 1B).

Standardises the loguru output into machine-parseable JSON lines with a
consistent field set, adds size-based rotation, and splits two high-value audit
streams (trade open/close, risk-gate decisions) into their own files so they can
be retained and queried independently of the noisy main log.

Design rules:

* **Additive** — this never removes the event-store sink (the dashboard /
  persistence pipeline) and never touches the stdlib intercept. It only manages
  the console + file sinks.
* **Backward compatible** — when structured logging is disabled the caller
  simply does not invoke :func:`configure_structured_logging`; the pre-existing
  loguru console sink is untouched.
* **No new dependencies** — pure loguru + stdlib ``json``.

A log line in JSON mode looks like::

    {"ts": "2026-06-18T04:00:00.000Z", "level": "INFO",
     "component": "risk_manager", "event": "trade_blocked",
     "message": "...", "pair": "EURUSD", "reason": "daily_drawdown_exceeded"}

``component`` / ``event`` and any extra key/values are pulled from loguru's
``record["extra"]`` — i.e. ``logger.bind(component="risk_manager",
event="trade_blocked", pair="EURUSD").info(...)``. Call sites that don't bind
anything still get valid JSON (``component`` falls back to the module name).
"""

from __future__ import annotations

import json
from typing import Any, Optional

from loguru import logger

# Keys that loguru injects into ``extra`` for its own bookkeeping / our event
# store correlation — excluded from the flattened JSON tail so the audit line
# stays clean.
_RESERVED_EXTRA = {"correlation_id", "cycle_id", "setup_id"}

# Filter tags used to route records to the dedicated audit sinks. A call site
# opts in with ``logger.bind(audit="trade")`` / ``logger.bind(audit="risk")``.
_AUDIT_TRADE = "trade"
_AUDIT_RISK = "risk"


def _iso_utc(record_time) -> str:
    """Render loguru's record time as an ISO-8601 UTC string (millisecond)."""
    try:
        return record_time.astimezone().isoformat(timespec="milliseconds")
    except Exception:  # noqa: BLE001
        return str(record_time)


def _json_sink_formatter(record) -> str:
    """Serialise one loguru record to a single JSON line.

    Used as a loguru ``format`` callable. Returns a template string ending in a
    newline; loguru appends nothing else when ``format`` returns the full line.
    The actual JSON is stashed in ``record["extra"]["_json"]`` so the returned
    template can reference it without re-escaping braces.
    """
    extra = dict(record["extra"] or {})
    component = extra.pop("component", None) or record["name"] or "app"
    event = extra.pop("event", None)
    payload: dict[str, Any] = {
        "ts": _iso_utc(record["time"]),
        "level": record["level"].name,
        "component": component,
        "message": record["message"],
    }
    if event is not None:
        payload["event"] = event
    # Flatten any remaining user-bound key/values (skip loguru/internal keys).
    for key, value in extra.items():
        if key in _RESERVED_EXTRA or key.startswith("_"):
            continue
        try:
            json.dumps(value)  # ensure serialisable
            payload[key] = value
        except (TypeError, ValueError):
            payload[key] = str(value)
    if record["exception"] is not None:
        payload["exception"] = repr(record["exception"])
    line = json.dumps(payload, default=str, ensure_ascii=False)
    # Escape braces so loguru does not treat the JSON as a format template.
    record["extra"]["_json"] = line
    return "{extra[_json]}\n"


def _make_filter(audit_tag: Optional[str]):
    """Build a loguru filter that keeps only records with the given audit tag.

    ``audit_tag=None`` keeps records that are NOT audit-tagged (the main log),
    so a single trade/risk line is not duplicated into the main stream.
    """

    def _filter(record) -> bool:
        tag = (record["extra"] or {}).get("audit")
        if audit_tag is None:
            return tag is None
        return tag == audit_tag

    return _filter


def configure_structured_logging(
    ops_config: Any,
    *,
    console_sink: Any = None,
    extra_sinks: Optional[list] = None,
) -> dict:
    """Install JSON/text file sinks + audit streams per ``ops_config``.

    Returns a summary dict describing which sinks were added. Never raises — a
    misconfigured path logs a warning and degrades to whatever could be set up,
    because logging must not be able to take down the trading loop.

    The caller is responsible for the console + event-store sinks (those live in
    ``main.py``). This function only manages the file/audit sinks so it composes
    with the existing setup rather than replacing it.
    """
    summary: dict[str, Any] = {"json": False, "main_log": None, "trade_audit": None, "risk_audit": None}
    if ops_config is None or not getattr(ops_config, "enabled", False):
        return summary

    fmt = "json" if str(getattr(ops_config, "log_format", "json")).lower() == "json" else "text"
    level = str(getattr(ops_config, "log_level", "INFO")).upper()
    rotation = f"{int(getattr(ops_config, 'log_max_size_mb', 50))} MB"
    retention = int(getattr(ops_config, "log_max_files", 10))

    formatter = _json_sink_formatter if fmt == "json" else (
        "{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <8} | "
        "{extra[component]} | {message}"
    )

    main_path = getattr(ops_config, "main_log", "logs/apex.log")
    trade_path = getattr(ops_config, "trade_audit_log", "logs/trade_audit.log")
    risk_path = getattr(ops_config, "risk_audit_log", "logs/risk_audit.log")

    # Ensure parent directories exist up front — loguru creates them lazily, but
    # being explicit makes the sink add deterministic regardless of cwd.
    import os as _os

    for _p in (main_path, trade_path, risk_path):
        try:
            parent = _os.path.dirname(str(_p))
            if parent:
                _os.makedirs(parent, exist_ok=True)
        except Exception:  # noqa: BLE001
            pass

    added: list[int] = []
    try:
        sink_id = logger.add(
            main_path,
            level=level,
            rotation=rotation,
            retention=retention,
            filter=_make_filter(None),
            format=formatter,
            enqueue=False,
            backtrace=False,
            catch=True,
        )
        added.append(sink_id)
        summary["main_log"] = main_path
        summary["json"] = fmt == "json"
    except Exception as exc:  # noqa: BLE001
        logger.warning("[ops] structured main-log sink failed ({}): {}", main_path, exc)

    for tag, path, key in (
        (_AUDIT_TRADE, trade_path, "trade_audit"),
        (_AUDIT_RISK, risk_path, "risk_audit"),
    ):
        try:
            sink_id = logger.add(
                path,
                level="DEBUG",
                rotation=rotation,
                retention=retention,
                filter=_make_filter(tag),
                format=formatter,
                enqueue=False,
                backtrace=False,
                catch=True,
            )
            added.append(sink_id)
            summary[key] = path
        except Exception as exc:  # noqa: BLE001
            logger.warning("[ops] structured {}-audit sink failed ({}): {}", tag, path, exc)

    summary["sink_ids"] = added
    logger.bind(component="ops", event="logging_configured").info(
        "Structured logging configured — format={}, main={}, trade_audit={}, risk_audit={}",
        fmt, summary["main_log"], summary["trade_audit"], summary["risk_audit"],
    )
    return summary


def audit_trade(message: str, **fields: Any) -> None:
    """Emit one trade-audit line (open/close) to the dedicated trade stream."""
    logger.bind(component="trade_audit", audit=_AUDIT_TRADE, **fields).info(message)


def audit_risk(message: str, **fields: Any) -> None:
    """Emit one risk-audit line (gate decision) to the dedicated risk stream."""
    logger.bind(component="risk_audit", audit=_AUDIT_RISK, **fields).info(message)


__all__ = [
    "configure_structured_logging",
    "audit_trade",
    "audit_risk",
]
