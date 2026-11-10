"""Typed, explicit environment overrides for operator-owned runtime settings.

Only operational controls are exposed here. Analytical definitions, learned
thresholds, broker-derived facts, OwnerLimits and constitutional safety gates
are intentionally excluded from generic environment mutation.
"""
from __future__ import annotations

import json
import math
import os
from dataclasses import fields, is_dataclass
from typing import Any


# section -> fields that are operational/deployment controls, not analytical
# cutoffs or capital/risk limits. Keep this allowlist reviewable.
ENV_CONFIG_FIELDS: dict[str, tuple[str, ...]] = {
    "performance": (
        "candle_cache_enabled", "candle_cache_ttl", "candle_cache_default_ttl",
        "m1_cache_ttl", "scan_interval_active", "scan_interval_with_positions",
        "parallel_scan_enabled", "parallel_scan_max_workers",
        "account_info_cache_enabled",
    ),
    "ops": (
        "enabled", "shutdown_timeout_seconds", "crash_marker_path",
        "heartbeat_interval_seconds", "heartbeat_file",
        "max_tick_duration_seconds", "tick_profiling_enabled",
        "slow_tick_threshold_ms", "profiling_window_size", "log_format",
        "log_level", "log_max_size_mb", "log_max_files", "main_log",
        "trade_audit_log", "risk_audit_log", "reconnect_max_retries",
        "reconnect_base_delay_seconds",
    ),
    "data_backup": (
        "enabled", "interval_hours", "max_file_size_mb", "exclude_patterns",
        "auto_sync_data_repo", "sync_branch", "data_repo_url",
        "sync_orphan_branch",
    ),
    "backtest": (
        "enabled", "default_spread_pips", "default_slippage_pips",
        "use_memory_db", "progress_interval", "synthetic_seed",
        "starting_balance",
    ),
    "decision_trace": ("enabled",),
    "outcome_feedback": (
        "enabled", "journal_path", "accuracy_lookback", "gold_specialist",
    ),
    "signal_ledger": (
        "signal_ledger_enabled", "signal_grading_enabled",
        "signal_grading_delay_minutes", "signal_grading_check_intervals",
        "emitter_feedback_enabled", "signal_ledger_db_path",
        "accuracy_lookback",
    ),
    "post_close_tracker": (
        "enabled", "db_path", "check_intervals_minutes", "max_retries",
    ),
    "tuner_agent": (
        "enabled", "max_tune_duration_seconds", "max_consecutive_failures",
        "audit_db_path", "log_all_skips",
    ),
    "counterfactual": (
        "counterfactual_enabled", "attribution_lookback",
        "attribution_interval", "min_trades_for_attribution",
        "counterfactual_db_path",
    ),
    "interaction": (
        "interaction_discovery_enabled", "interaction_lookback",
        "interaction_interval", "exhaustive_search_max_modules",
        "interaction_db_path",
    ),
    "calibration": ("enabled", "state_path", "spread_sample_tf", "persist"),
    "developing_analysis": (
        "enabled", "max_workers", "refresh_m5", "refresh_m15",
        "refresh_h1", "refresh_h4", "refresh_d1",
        "management_advisory_enabled",
    ),
    "campaign": (
        "enabled", "dormant_after_seconds", "invalidate_after_seconds",
        "history_limit", "postmortem_enabled", "evidence_full_refreshes",
    ),
    "adaptive_tuner": (
        "enabled", "periodic_interval_seconds", "scan_interval_seconds",
    ),
    "cross_instrument": (
        "queue_enabled", "queue_window_ms", "ranking_enabled",
        "quality_sizing_enabled", "displacement_enabled",
        "proactive_scan_enabled", "proactive_scan_interval_seconds",
    ),
}


def _parse_value(raw: str, current: Any, key: str) -> Any:
    value = raw.strip()
    if isinstance(current, bool):
        lowered = value.lower()
        if lowered in {"1", "true", "yes", "on"}:
            return True
        if lowered in {"0", "false", "no", "off"}:
            return False
        raise ValueError(f"{key} must be a boolean")
    if isinstance(current, int) and not isinstance(current, bool):
        try:
            return int(value)
        except ValueError as exc:
            raise ValueError(f"{key} must be an integer") from exc
    if isinstance(current, float):
        try:
            parsed = float(value)
        except ValueError as exc:
            raise ValueError(f"{key} must be numeric") from exc
        if not math.isfinite(parsed):
            raise ValueError(f"{key} must be finite")
        return parsed
    if isinstance(current, (list, dict, tuple)):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{key} must contain valid JSON") from exc
        if isinstance(current, list) and not isinstance(parsed, list):
            raise ValueError(f"{key} must be a JSON array")
        if isinstance(current, dict) and not isinstance(parsed, dict):
            raise ValueError(f"{key} must be a JSON object")
        if isinstance(current, tuple) and not isinstance(parsed, list):
            raise ValueError(f"{key} must be a JSON array")
        return tuple(parsed) if isinstance(current, tuple) else parsed
    if isinstance(current, str):
        # JSON string syntax allows an explicit empty string; otherwise retain
        # the literal value, including URL/path punctuation.
        if value.startswith('"') and value.endswith('"'):
            try:
                parsed = json.loads(value)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{key} has invalid quoted string syntax") from exc
            if not isinstance(parsed, str):
                raise ValueError(f"{key} must be a string")
            return parsed
        return value
    raise TypeError(f"{key} cannot override unsupported value type {type(current).__name__}")


def apply_environment_config(config: Any) -> list[str]:
    """Apply allowlisted APEX_<SECTION>_<FIELD> variables to AppConfig.

    Empty template assignments mean 'use the typed default'. Invalid values
    fail startup rather than silently falling back to a different runtime.
    """
    applied: list[str] = []
    for section, names in ENV_CONFIG_FIELDS.items():
        target = getattr(config, section, None)
        if target is None or not is_dataclass(target):
            raise TypeError(f"AppConfig.{section} must be a dataclass")
        available = {item.name for item in fields(target)}
        for name in names:
            if name not in available:
                raise AttributeError(f"unknown {section} configuration field: {name}")
            key = f"APEX_{section}_{name}".upper()
            raw = os.getenv(key)
            if raw is None or raw == "":
                continue
            current = getattr(target, name)
            parsed = _parse_value(raw, current, key)
            setattr(target, name, parsed)
            applied.append(key)
    _validate_operational_config(config)
    return applied


def _validate_operational_config(config: Any) -> None:
    performance = config.performance
    for name in (
        "candle_cache_ttl", "candle_cache_default_ttl", "m1_cache_ttl",
        "scan_interval_active", "scan_interval_with_positions",
    ):
        value = getattr(performance, name)
        if float(value) <= 0:
            raise ValueError(f"APEX_PERFORMANCE_{name.upper()} must be positive")
    if int(performance.parallel_scan_max_workers) < 1:
        raise ValueError("APEX_PERFORMANCE_PARALLEL_SCAN_MAX_WORKERS must be >= 1")

    ops = config.ops
    for name in (
        "shutdown_timeout_seconds", "heartbeat_interval_seconds",
        "max_tick_duration_seconds", "slow_tick_threshold_ms",
        "profiling_window_size", "log_max_size_mb", "log_max_files",
        "reconnect_base_delay_seconds",
    ):
        if float(getattr(ops, name)) <= 0:
            raise ValueError(f"APEX_OPS_{name.upper()} must be positive")
    if int(ops.reconnect_max_retries) < 0:
        raise ValueError("APEX_OPS_RECONNECT_MAX_RETRIES cannot be negative")

    backup = config.data_backup
    if float(backup.interval_hours) <= 0 or float(backup.max_file_size_mb) <= 0:
        raise ValueError("APEX_DATA_BACKUP interval and max file size must be positive")

    backtest = config.backtest
    if float(backtest.default_spread_pips) < 0 or float(backtest.default_slippage_pips) < 0:
        raise ValueError("APEX_BACKTEST spread/slippage cannot be negative")
    if float(backtest.starting_balance) <= 0:
        raise ValueError("APEX_BACKTEST_STARTING_BALANCE must be positive")

    if int(config.post_close_tracker.max_retries) < 1:
        raise ValueError("APEX_POST_CLOSE_TRACKER_MAX_RETRIES must be >= 1")
    if int(config.developing_analysis.max_workers) < 1:
        raise ValueError("APEX_DEVELOPING_ANALYSIS_MAX_WORKERS must be >= 1")
    if int(config.tuner_agent.max_consecutive_failures) < 1:
        raise ValueError("APEX_TUNER_AGENT_MAX_CONSECUTIVE_FAILURES must be >= 1")
