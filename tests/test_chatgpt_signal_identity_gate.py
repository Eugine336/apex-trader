"""Regression tests for the ChatGPT→Binance relay's CognitiveBrain boundary."""

import ast
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock

import pytest

from integrations.chatgpt_signals import executor as executor_module
from integrations.chatgpt_signals.binance_execution import (
    BinanceChatGPTExecutor,
    BinanceExecutionError,
    BinanceSignedClient,
)
from integrations.chatgpt_signals.executor import ChatGPTExecutor
from integrations.chatgpt_signals.schema import ChatGPTTradeSignal
from integrations.chatgpt_signals import runtime_health


ROOT = Path(__file__).resolve().parents[1]


def _signal(mode: str = "LIVE") -> ChatGPTTradeSignal:
    return ChatGPTTradeSignal(
        signal_id="identity-gate-test",
        symbol="BTCUSDT",
        side="BUY",
        order_type="MARKET",
        quantity=0.001,
        stop_loss=60000.0,
        take_profit=70000.0,
        expires_at=datetime(2099, 1, 1, tzinfo=timezone.utc),
        mode=mode,
    )


def test_live_configured_shared_executor_rejects_external_signal(monkeypatch):
    monkeypatch.setenv("BINANCE_EXECUTION_MODE", "LIVE")
    paper = Mock()
    monkeypatch.setattr(executor_module, "BinancePaperExecutor", lambda: paper)
    executor = ChatGPTExecutor()

    with pytest.raises(BinanceExecutionError, match="paper-only"):
        executor.execute(_signal("LIVE"))

    paper.execute.assert_not_called()


def test_paper_configuration_cannot_smuggle_live_signal(monkeypatch):
    monkeypatch.setenv("BINANCE_EXECUTION_MODE", "PAPER")
    paper = Mock()
    monkeypatch.setattr(executor_module, "BinancePaperExecutor", lambda: paper)
    executor = ChatGPTExecutor()

    with pytest.raises(BinanceExecutionError, match="paper-only"):
        executor.execute(_signal("LIVE"))

    paper.execute.assert_not_called()


def test_direct_live_adapter_is_fail_closed():
    live = object.__new__(BinanceChatGPTExecutor)
    with pytest.raises(BinanceExecutionError, match="LIVE execution is disabled"):
        live.execute(_signal("LIVE"))


def test_low_level_binance_order_endpoint_is_disabled():
    client = object.__new__(BinanceSignedClient)
    with pytest.raises(BinanceExecutionError, match="Direct Binance order placement is disabled"):
        client.place_order(symbol="BTCUSDT", side="BUY", type="MARKET", quantity="0.001")
    with pytest.raises(BinanceExecutionError, match="Mutating Binance REST requests are disabled"):
        client._request("POST", "/fapi/v1/order", {"symbol": "BTCUSDT"}, signed=True)


def test_runtime_health_never_declares_live_ready(monkeypatch, capsys):
    monkeypatch.setenv("BINANCE_EXECUTION_MODE", "LIVE")
    monkeypatch.setenv("BINANCE_LIVE_EXECUTION_ENABLED", "true")
    monkeypatch.setenv("CHATGPT_RELAY_TOKEN", "test-token")
    monkeypatch.setenv("BINANCE_MAX_NOTIONAL_USDT", "100")
    monkeypatch.setenv("CHATGPT_SIGNAL_TIMEOUT_SECONDS", "90")
    monkeypatch.setenv("MAX_SIMULTANEOUS_POSITIONS", "3")
    monkeypatch.setenv("MAX_ACCOUNT_RISK_PERCENT", "1.0")

    assert runtime_health.check() == 2
    output = capsys.readouterr().out
    assert "Live execution       : BLOCKED" in output
    assert "Status               : BLOCKED" in output
    assert "LIVE-READY" not in output


def test_relay_health_reports_effective_paper_only_mode():
    tree = ast.parse((ROOT / "integrations/chatgpt_signals/relay_server.py").read_text(encoding="utf-8"))
    function = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "health")
    returned = next(n for n in ast.walk(function) if isinstance(n, ast.Return) and isinstance(n.value, ast.Dict))
    health = {
        key.value: value.value
        for key, value in zip(returned.value.keys, returned.value.values)
        if isinstance(key, ast.Constant) and isinstance(value, ast.Constant)
    }
    assert health["execution_mode"] == "PAPER_ONLY"
    assert health["live_enabled"] is False
    assert health["binance_private_api"] is False


def test_relay_blocks_non_paper_mode_before_executor_call():
    tree = ast.parse((ROOT / "integrations/chatgpt_signals/relay_server.py").read_text(encoding="utf-8"))
    function = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "receive_signal")
    guards = [
        node for node in ast.walk(function)
        if isinstance(node, ast.If)
        and isinstance(node.test, ast.Compare)
        and isinstance(node.test.left, ast.Name)
        and node.test.left.id == "MODE"
        and any(isinstance(value, ast.Constant) and value.value == "PAPER" for value in node.test.comparators)
    ]
    assert guards, "relay route must explicitly fail closed outside PAPER mode"
    assert any(
        any(
            isinstance(call, ast.Call)
            and isinstance(call.func, ast.Name)
            and call.func.id == "HTTPException"
            for call in ast.walk(guard)
        )
        for guard in guards
    ), "non-PAPER route must raise HTTP 503"
