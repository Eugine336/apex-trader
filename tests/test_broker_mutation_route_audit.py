"""Static guard against live broker-entry bypasses.

This audit is intentionally AST-only: it runs without broker SDKs, credentials,
or market connectivity. Broker protocol adapters and the simulated backtest
broker may implement order APIs; orchestration code must not invoke them
directly and bypass the shared admission/execution boundary.
"""
from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BROKER_MUTATORS = {
    # Exposure-increasing entry operations.
    "place_order",
    "place_pending_order",
    "execute_entry",
    "execute_pending_entry",
    "order_send",
    "buy",
    "sell",
    # Existing-position and pending-order mutations.
    "modify_order",
    "close_order",
    "cancel_pending_order",
    "modify_trade",
    "close_trade",
    "cancel_order",
    "modify_position",
    "close_position",
    "order_modify",
    "order_close",
    "order_delete",
}

# Explicit implementation boundaries, not orchestration routes. Keep this list
# narrow and path-specific: excluding the whole platforms/ tree would silently
# exempt any newly added runtime coordinator from the audit.
_ALLOWED_MUTATION_FILES = {
    Path("execution/action_executor.py"),
    Path("platforms/platform_manager.py"),
    Path("platforms/mt5/mt5_connector.py"),
    Path("platforms/deriv/deriv_connector.py"),
    Path("backtest/broker.py"),
    Path("backtest/runner.py"),
}


def _broker_mutation_calls(path: Path) -> list[tuple[int, str]]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr in BROKER_MUTATORS:
            # TradeManager.close_trade is an in-memory legacy exit decision,
            # not a broker mutation. Calls through a broker/connector attribute
            # are still detected and must remain inside the approved gateway.
            if (
                func.attr in {"close_trade", "modify_trade"}
                and isinstance(func.value, ast.Name)
                and func.value.id == "self"
            ):
                continue
            found.append((node.lineno, func.attr))
    return found


def test_live_orchestration_has_no_direct_broker_mutations() -> None:
    """No runtime coordinator may bypass the shared Brain/admission path."""
    roots = (
        ROOT / "event_driven_bootstrap.py",
        ROOT / "main.py",
        ROOT / "risk" / "spread_bootstrap.py",
        ROOT / "integrations" / "chatgpt_signals" / "binance_execution.py",
    )
    violations = []
    for path in roots:
        if not path.exists():
            continue
        for line, method in _broker_mutation_calls(path):
            violations.append(f"{path.relative_to(ROOT)}:{line} calls {method}()")
    assert not violations, (
        "Direct broker mutations found outside the shared "
        "ActionExecutor/PlatformManager boundary:\n" + "\n".join(violations)
    )


def test_only_approved_gateway_files_call_broker_mutators() -> None:
    """Catch new entry, close, modify and cancel call sites anywhere in production Python code."""
    excluded_dirs = {
        ".git", ".venv", "venv", "node_modules", "__pycache__", "tests",
    }
    violations = []
    for path in ROOT.rglob("*.py"):
        relative = path.relative_to(ROOT)
        if any(part in excluded_dirs for part in relative.parts) or relative.name == "conftest.py":
            continue
        if relative in _ALLOWED_MUTATION_FILES:
            continue
        try:
            calls = _broker_mutation_calls(path)
        except (SyntaxError, UnicodeDecodeError):
            # Parse errors are covered by the repository's compile/lint checks.
            continue
        violations.extend(
            f"{relative}:{line} calls {method}()" for line, method in calls
        )
    assert not violations, (
        "Unexpected broker mutation call sites found. Route entry, close, modify and cancel "
        "flows through CognitiveBrain → first-class opportunity/whole-book "
        "admission → shared RiskGate/ActionExecutor instead:\n"
        + "\n".join(violations)
    )
