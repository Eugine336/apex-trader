"""Static guard against live broker-entry bypasses.

This audit is intentionally AST-only: it runs without broker SDKs, credentials,
or market connectivity. Low-level broker adapters and the simulated backtest
broker are allowed to implement order APIs; orchestration code must not invoke
them directly and bypass the shared admission/execution boundary.
"""
from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENTRY_MUTATORS = {
    "place_order",
    "place_pending_order",
    "execute_entry",
    "order_send",
    "buy",
    "sell",
}

# These are implementation boundaries, not orchestration routes:
# - platform adapters/gateway own the final broker protocol calls.
# - ActionExecutor is the sole shared intent-to-broker entry executor.
# - backtest uses a simulated broker, never a live connector.
_ALLOWED_MUTATION_FILES = {
    Path("execution/action_executor.py"),
    Path("platforms/platform_manager.py"),
}


def _entry_mutation_calls(path: Path) -> list[tuple[int, str]]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr in ENTRY_MUTATORS:
            found.append((node.lineno, func.attr))
    return found


def test_live_orchestration_has_no_direct_entry_mutation_calls() -> None:
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
        for line, method in _entry_mutation_calls(path):
            violations.append(f"{path.relative_to(ROOT)}:{line} calls {method}()")
    assert not violations, (
        "Direct exposure-increasing broker calls found outside the shared "
        "ActionExecutor/PlatformManager boundary:\n" + "\n".join(violations)
    )


def test_only_approved_gateway_files_call_live_entry_mutators() -> None:
    """Catch new direct-entry call sites anywhere in production Python code."""
    excluded_dirs = {
        ".git", ".venv", "venv", "node_modules", "__pycache__", "tests",
        "backtest", "platforms",
    }
    violations = []
    for path in ROOT.rglob("*.py"):
        relative = path.relative_to(ROOT)
        if any(part in excluded_dirs for part in relative.parts):
            continue
        if relative in _ALLOWED_MUTATION_FILES:
            continue
        try:
            calls = _entry_mutation_calls(path)
        except (SyntaxError, UnicodeDecodeError):
            # Parse errors are covered by the repository's compile/lint checks.
            continue
        violations.extend(
            f"{relative}:{line} calls {method}()" for line, method in calls
        )
    assert not violations, (
        "Unexpected direct entry mutation call sites found. Route new entry "
        "flows through CognitiveBrain → first-class opportunity/whole-book "
        "admission → shared RiskGate/ActionExecutor instead:\n"
        + "\n".join(violations)
    )
