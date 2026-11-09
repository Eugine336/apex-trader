from __future__ import annotations

from pathlib import Path

from config import OpsConfig
from ops.logging_config import configure_structured_logging
from loguru import logger


def test_default_structured_logs_follow_runtime_log_directory(tmp_path, monkeypatch):
    log_root = tmp_path / "isolated" / "logs"
    monkeypatch.setenv("APEX_LOG_DIR", str(log_root))
    summary = configure_structured_logging(OpsConfig())
    sink_ids = summary.get("sink_ids", [])
    try:
        assert summary["main_log"] == str(log_root / "apex.log")
        assert summary["trade_audit"] == str(log_root / "trade_audit.log")
        assert summary["risk_audit"] == str(log_root / "risk_audit.log")
        assert all(Path(summary[key]).parent == log_root for key in (
            "main_log", "trade_audit", "risk_audit",
        ))
    finally:
        for sink_id in sink_ids:
            logger.remove(sink_id)
