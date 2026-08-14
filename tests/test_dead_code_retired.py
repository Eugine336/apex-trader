"""V16 — the retired DecisionEngine management path carries no dead code.

Pure standard library: parses the bootstrap source with ``ast`` so it needs no
heavy runtime imports (broker/ML deps), and asserts the retired management
method is a clean stub with nothing after its unconditional return.
"""

import ast
import pathlib

_SRC = pathlib.Path(__file__).resolve().parent.parent / "event_driven_bootstrap.py"


def _methods() -> dict:
    tree = ast.parse(_SRC.read_text())
    return {
        n.name: n
        for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef)
    }


def test_run_decision_engine_management_is_clean_stub():
    node = _methods().get("_run_decision_engine_management")
    assert node is not None, "the retired stub must still exist"
    body = node.body
    # docstring first, an unconditional return as the final statement …
    assert isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant)
    assert isinstance(body[-1], ast.Return)
    # … and NOTHING after the return (no unreachable dead code).
    return_idx = next(i for i, s in enumerate(body) if isinstance(s, ast.Return))
    assert return_idx == len(body) - 1
    # The whole method is tiny now — guard against dead code re-accreting.
    assert (node.end_lineno - node.lineno) < 20


def test_retired_candidate_management_helpers_removed():
    names = set(_methods())
    for gone in (
        "_check_candidate_thesis",
        "_check_evidence_exit",
        "_scope_votes_to_candidate",
        "_management_micro_context",
        "_sl_move_too_close",
        "_record_candidate_position",
        "_scale_in_position",
        "_partial_close_position",
    ):
        assert gone not in names, f"{gone} should have been removed as dead code"
