"""Audit silent exception handlers and require explicit classification.

A handler that only returns a sentinel can be a valid fail-closed boundary, but
an unexplained ``pass``/``return None`` can also hide a dangerous failure. This
test therefore accepts only handlers that log/re-raise or carry an explicit,
local safe-fallback rationale. Unclassified handlers remain a CI failure; this
avoids blanket re-raises or meaningless logging on every exception path.
"""

import ast
import os
import pathlib
import re


_REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent

_SKIP_DIRS = {"tests", "node_modules", "__pycache__", ".git", ".ddagent"}

_SENTINEL_VALUES = {None, False, 0, True, "", 0.0}


def _is_silent_handler(handler: ast.ExceptHandler) -> bool:
    """Return True if the handler body has no logging, no raise, and only
    pass / continue / return-of-a-sentinel constant."""
    for stmt in handler.body:
        if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call):
            func = stmt.value.func
            if isinstance(func, ast.Attribute):
                if isinstance(func.value, ast.Name) and func.value.id in (
                    "logger",
                    "logging",
                    "log",
                ):
                    return False
                if func.attr in (
                    "debug",
                    "info",
                    "warning",
                    "error",
                    "critical",
                    "exception",
                    "warn",
                ):
                    return False
            if isinstance(func, ast.Name) and func.id == "print":
                return False
        if isinstance(stmt, ast.Raise):
            return False

    for stmt in handler.body:
        if isinstance(stmt, ast.Pass):
            continue
        if isinstance(stmt, ast.Continue):
            continue
        if isinstance(stmt, ast.Return):
            if stmt.value is None:
                continue
            if (
                isinstance(stmt.value, ast.Constant)
                and stmt.value.value in _SENTINEL_VALUES
            ):
                continue
            if isinstance(stmt.value, ast.List) and len(stmt.value.elts) == 0:
                continue
            if isinstance(stmt.value, ast.Dict) and len(stmt.value.keys) == 0:
                continue
            return False
        if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant):
            continue
        return False
    return True


_SAFE_FALLBACK_RATIONALE = re.compile(
    r"(?:safe[- ]fallback|fail[- ]safe|fail[- ]closed|never break|must never|"
    r"must not authorize|must not allow|best[- ]effort|non[- ]fatal|"
    r"optional .*?(?:probe|reader|adapter|integration|sink)|diagnostic only)",
    re.IGNORECASE,
)


def _has_explicit_fallback_rationale(source_lines: list[str], lineno: int) -> bool:
    """Require a nearby comment that explains why a sentinel fallback is safe."""
    # Inspect the except line and the immediately preceding comment line only;
    # distant file-level comments must not blanket-classify unrelated handlers.
    candidates = source_lines[max(0, lineno - 2):lineno]
    for line in candidates:
        comment = line.partition("#")[2].strip()
        if not comment:
            continue
        if "safe-fallback:" in comment.lower():
            return True
        if "NOQA: BLE001" in comment.upper() and _SAFE_FALLBACK_RATIONALE.search(comment):
            return True
        if line.lstrip().startswith("#") and _SAFE_FALLBACK_RATIONALE.search(comment):
            return True
    return False


def _collect_silent_handlers():
    """Yield unclassified silent handlers as (filepath, lineno)."""
    for root, dirs, files in os.walk(_REPO_ROOT):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
        for fname in files:
            if not fname.endswith(".py"):
                continue
            fpath = os.path.join(root, fname)
            try:
                with open(fpath, encoding="utf-8") as fh:
                    source = fh.read()
                source_lines = source.splitlines()
                tree = ast.parse(source, filename=fpath)
            except (OSError, UnicodeDecodeError, SyntaxError):
                continue
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.ExceptHandler)
                    and _is_silent_handler(node)
                    and not _has_explicit_fallback_rationale(source_lines, node.lineno)
                ):
                    rel = os.path.relpath(fpath, _REPO_ROOT)
                    yield rel, node.lineno


def test_no_silent_exception_handlers():
    """Every silent handler must be logged, re-raised, or explicitly classified."""
    silent = list(_collect_silent_handlers())
    if silent:
        detail = "\n".join(f"  {f}:{ln}" for f, ln in silent)
        raise AssertionError(
            f"{len(silent)} unclassified silent exception handler(s) found — "
            f"add logging, re-raise, or document the safe fallback locally:\n{detail}"
        )
