"""Permanent identity source/document conformance checks (initial scaffold)."""

from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace

from cognition.brain import BrainOutput
from cognition.contracts import DecisionType
from cognition.identity import IDENTITY_STATEMENT


_REPO_ROOT = Path(__file__).resolve().parents[1]
_EXCLUDED_DIRS = {".git", ".venv", "__pycache__", "build", "dist", "node_modules"}


def test_identity_statement_has_one_python_definition():
    definitions: list[Path] = []
    for path in _REPO_ROOT.rglob("*.py"):
        relative = path.relative_to(_REPO_ROOT)
        if "data" in relative.parts or any(part in _EXCLUDED_DIRS for part in relative.parts):
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (OSError, UnicodeDecodeError, SyntaxError):
            continue
        for node in ast.walk(tree):
            targets = []
            if isinstance(node, ast.Assign):
                targets = node.targets
            elif isinstance(node, ast.AnnAssign):
                targets = [node.target]
            if any(
                isinstance(target, ast.Name) and target.id == "IDENTITY_STATEMENT"
                for target in targets
            ):
                definitions.append(relative)

    assert definitions == [Path("cognition/identity.py")]


def test_canonical_identity_is_copied_verbatim_into_public_identity_docs():
    docs_dir = _REPO_ROOT / "docs"
    documents = [
        _REPO_ROOT / "README.md",
        docs_dir / "APEX_IDENTITY.md",
        *sorted(docs_dir.glob("APEX_Constitution*.md")),
    ]
    assert documents[0].is_file()
    assert documents[1].is_file()
    assert any(path.is_file() for path in documents[2:])

    for path in documents:
        content = path.read_text(encoding="utf-8")
        assert content.count(IDENTITY_STATEMENT) == 1, (
            f"{path.relative_to(_REPO_ROOT)} must contain the canonical identity "
            "exactly once, byte-for-byte"
        )



def test_repository_wide_identity_audit():
    """Keep the repo-wide identity/provenance/order-boundary guard executable in CI."""
    from scripts.identity_audit import audit_repository

    violations = audit_repository()
    assert not violations, "Identity conformance violations:\n" + "\n".join(violations)



def test_provider_status_is_independent_of_market_verdict():
    """A native campaign can coexist with an unavailable external advisor."""
    decision = SimpleNamespace(
        decision_type=DecisionType.OPEN_CAMPAIGN,
        questions_answered={
            "reasoning_mode": "native_only",
            "external_advisor_status": "unavailable",
        },
    )
    output = BrainOutput(decision=decision, direction="LONG")
    assert output.decision_type == DecisionType.OPEN_CAMPAIGN.value
    assert output.provider_status == "unavailable"
    assert output.provider_unavailable is True
