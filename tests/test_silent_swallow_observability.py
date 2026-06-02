"""Regression test: assert zero silent exception handlers remain in the source tree.

A "silent handler" is an except clause whose body contains only
pass / continue / return <sentinel> with NO logging and NO re-raise.

This test prevents regression after M3 Phase A added observability
logging to all 57 formerly-silent handlers.
"""

import ast
import os
import pathlib


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


def _collect_silent_handlers():
    """Walk the non-test source tree and yield (filepath, lineno) for each
    silent exception handler found."""
    for root, dirs, files in os.walk(_REPO_ROOT):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
        for fname in files:
            if not fname.endswith(".py"):
                continue
            fpath = os.path.join(root, fname)
            try:
                with open(fpath) as fh:
                    tree = ast.parse(fh.read())
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.ExceptHandler) and _is_silent_handler(node):
                    rel = os.path.relpath(fpath, _REPO_ROOT)
                    yield rel, node.lineno


def test_no_silent_exception_handlers():
    """Every except handler in the source tree must contain either a log call
    or a re-raise.  Silent swallowing of exceptions is a regression."""
    silent = list(_collect_silent_handlers())
    if silent:
        detail = "\n".join(f"  {f}:{ln}" for f, ln in silent)
        raise AssertionError(
            f"{len(silent)} silent exception handler(s) found — "
            f"add logging or re-raise:\n{detail}"
        )
