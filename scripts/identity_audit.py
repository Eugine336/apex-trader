"""Static conformance audit for the canonical Apex identity contract.

The audit is read-only: it scans Python source and the designated identity
documents only. It never opens .env, runtime databases, broker connectors, or
trading state.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

from cognition.identity import IDENTITY_STATEMENT

ROOT = Path(__file__).resolve().parents[1]
EXCLUDED = {".git", ".venv", "__pycache__", "build", "dist", "node_modules"}
FORBIDDEN_IDENTITY = re.compile(
    r"\b(?:apex(?:\s+trader)?|the\s+system|this\s+trading\s+system)\s+"
    r"(?:is|operates\s+as|is\s+configured\s+as)\s+"
    r"(?:an?\s+)?(?:scalper|swing\s+trader|day\s+trader|ict[- ]only|gold[- ]only)\b",
    re.IGNORECASE,
)
RETIRED_ENTRY_SYMBOLS = (
    "_on_entry_decision",
    "_register_entry_zone_critical_levels",
    "entry_orchestrator",
)
BROKER_MUTATION_NAMES = {
    "execute_entry",
    "modify_trade",
    "close_trade",
    "place_order",
    "modify_order",
    "order_send",
    "close_position",
}


def _python_files() -> list[Path]:
    files: list[Path] = []
    for path in ROOT.rglob("*.py"):
        relative = path.relative_to(ROOT)
        if any(part in EXCLUDED for part in relative.parts):
            continue
        if "data" in relative.parts or "tests" in relative.parts:
            continue
        if path.is_file():
            files.append(path)
    return sorted(files)


def _production_python_files() -> list[Path]:
    return [
        path for path in _python_files()
        if path.relative_to(ROOT).parts[0] not in {"scripts", "docs", "test_support"}
    ]


def _single_assignment(path: Path, name: str) -> bool:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, UnicodeDecodeError, SyntaxError):
        return False
    definitions = []
    for node in ast.walk(tree):
        targets = []
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        if any(isinstance(target, ast.Name) and target.id == name for target in targets):
            definitions.append(path.relative_to(ROOT))
    return definitions == [Path("cognition/identity.py")]


def _contains_name(node: ast.AST, name: str) -> bool:
    return any(isinstance(child, ast.Name) and child.id == name for child in ast.walk(node))


def audit_repository() -> list[str]:
    """Return all known static conformance violations (empty means clean)."""
    violations: list[str] = []
    identity_path = ROOT / "cognition" / "identity.py"
    if not _single_assignment(identity_path, "IDENTITY_STATEMENT"):
        violations.append("IDENTITY_STATEMENT must have exactly one definition in cognition/identity.py")

    documents = [
        ROOT / "README.md",
        ROOT / "docs" / "APEX_IDENTITY.md",
        *sorted((ROOT / "docs").glob("APEX_Constitution*.md")),
    ]
    if not documents[0].is_file() or not documents[1].is_file():
        violations.append("README.md and docs/APEX_IDENTITY.md are required")
    if not any(path.is_file() for path in documents[2:]):
        violations.append("a canonical APEX constitution document is required")
    for path in documents:
        if path.is_file():
            try:
                content = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                violations.append(f"{path.relative_to(ROOT)} is not readable as UTF-8")
                continue
            if content.count(IDENTITY_STATEMENT) != 1:
                violations.append(
                    f"{path.relative_to(ROOT)} must contain the canonical identity exactly once"
                )

    for path in _production_python_files():
        try:
            content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            violations.append(f"{path.relative_to(ROOT)} is not readable as UTF-8")
            continue
        if FORBIDDEN_IDENTITY.search(content):
            violations.append(
                f"{path.relative_to(ROOT)} appears to define Apex as a fixed trading style"
            )
        for symbol in RETIRED_ENTRY_SYMBOLS:
            if symbol in content:
                violations.append(
                    f"{path.relative_to(ROOT)} references retired entry symbol {symbol}"
                )
        try:
            tree = ast.parse(content, filename=str(path))
        except SyntaxError:
            violations.append(f"{path.relative_to(ROOT)} has a syntax error")
            continue

        relative = path.relative_to(ROOT).as_posix()
        # Broker-mutating adapters are confined to the canonical execution sink
        # and platform connector boundary. Simulation-only trade managers are
        # excluded because their methods mutate in-memory replay state, not live
        # broker accounts. Do not use receiver-name heuristics here: self._pm,
        # a custom wrapper, or an alias can still perform a real order mutation.
        simulation_only = (
            relative.startswith("platforms/")
            or relative.startswith("backtest/")
            or relative.startswith("rl/")
            or relative.startswith("management/")
            or relative.startswith("test_support/")
        )
        if relative != "execution/action_executor.py" and not simulation_only:
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                    continue
                if node.func.attr not in BROKER_MUTATION_NAMES:
                    continue
                violations.append(
                    f"{relative}:{node.lineno} calls broker mutation "
                    f"{node.func.attr} outside the ActionExecutor/platform boundary"
                )

    intents_path = ROOT / "execution" / "intents.py"
    try:
        intents_tree = ast.parse(intents_path.read_text(encoding="utf-8"))
        intent_class = next(
            node for node in intents_tree.body
            if isinstance(node, ast.ClassDef) and node.name == "Intent"
        )
        if not any(
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == "identity_version"
            for node in intent_class.body
        ):
            violations.append("Intent must carry identity_version")
        for method_name in ("open", "close", "modify_sl", "modify_tp", "partial_close"):
            method = next(
                (
                    node for node in intent_class.body
                    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and node.name == method_name
                ),
                None,
            )
            if method is None or not _contains_name(method, "identity_version"):
                violations.append(f"Intent.{method_name} must preserve identity_version")
    except (OSError, UnicodeDecodeError, SyntaxError, StopIteration):
        violations.append("execution/intents.py could not be audited")

    prompt_path = ROOT / "llm" / "reasoner.py"
    try:
        prompt_content = prompt_path.read_text(encoding="utf-8")
        prompt_tree = ast.parse(prompt_content, filename=str(prompt_path))
        imports_identity = any(
            isinstance(node, ast.ImportFrom)
            and node.module == "cognition.identity"
            and any(alias.name == "IDENTITY_STATEMENT" for alias in node.names)
            for node in ast.walk(prompt_tree)
        )
        if not imports_identity:
            violations.append("llm/reasoner.py must import IDENTITY_STATEMENT")
        assignments = {
            target.id: node.value
            for node in ast.walk(prompt_tree)
            if isinstance(node, (ast.Assign, ast.AnnAssign))
            for target in (
                node.targets if isinstance(node, ast.Assign) else [node.target]
            )
            if isinstance(target, ast.Name)
        }
        for prompt_name in ("_SYSTEM_PROMPT", "_MANAGEMENT_SYSTEM_PROMPT"):
            prompt_expr = assignments.get(prompt_name)
            if prompt_expr is None or not _contains_name(prompt_expr, "IDENTITY_STATEMENT"):
                violations.append(f"{prompt_name} must compose the canonical identity")
        # "Opportunity harvester" is an analytical role description. Only a
        # sentence that installs it as Apex's permanent identity conflicts with
        # the canonical statement.
        if re.search(
            r"\bApex\s+(?:is|acts as|operates as)\s+an?\s+OPPORTUNITY HARVESTER\b",
            prompt_content,
            re.IGNORECASE,
        ):
            violations.append(
                "the LLM prompt must not replace Apex's canonical identity with a permanent role"
            )
    except (OSError, UnicodeDecodeError, SyntaxError):
        violations.append("llm/reasoner.py could not be audited")

    return sorted(set(violations))


__all__ = ["audit_repository"]
