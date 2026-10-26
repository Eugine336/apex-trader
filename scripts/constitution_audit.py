#!/usr/bin/env python3
"""Static constitutional live-path guard for APEX TRADER.

Standard-library only (no third-party imports) so it runs anywhere, including a
minimal sandbox. It scans the *production* source for constructs the APEX
Constitution forbids on the live trading path, so a regression cannot silently
re-introduce a retired decider, a module-level directional vote, or a premature
LONG/SHORT/FLAT collapse.

Each finding names the constitution area it violates and the redesign PR that is
scheduled to remove it. This is a BASELINE tool: it is expected to report
violations until the constitutional redesign lands. Phase H wires it as a
required pre-merge gate — ``exit 0`` then means the live path is constitutional.

Usage::

    python scripts/constitution_audit.py           # human report; exit 1 if any
    python scripts/constitution_audit.py --json     # machine-readable report
    python scripts/constitution_audit.py --quiet     # summary line only

Scoping: pattern checks target specific production files (never ``tests/`` and
never this script), so test fixtures referencing retired names do not register
as live-path violations, and this guard never flags its own token list.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


@dataclass
class Check:
    """One constitutional invariant and how to detect its violation statically."""

    id: str
    clause: str
    pr: str
    summary: str
    # Production files whose mere existence is a violation (retired modules).
    exists: list[str] = field(default_factory=list)
    # Production files that must exist because they enforce a constitutional invariant.
    required_files: list[str] = field(default_factory=list)
    # (relative_path, compiled-regex) pairs; each match line is a violation.
    patterns: list[tuple[str, "re.Pattern[str]"]] = field(default_factory=list)
    # (relative_path, compiled-regex) pairs that must match at least once.
    required_patterns: list[tuple[str, "re.Pattern[str]"]] = field(default_factory=list)
    # Method-call regexes that must not appear in ANY production file — a live
    # call site for a retired decider. Scanned tree-wide (see ``_scan_tree``),
    # excluding tests, this script, and the retired-cluster files listed in
    # ``scan_excludes`` (where the deciders are DEFINED and may legitimately
    # cross-call one another). This catches a regression that re-wires a retired
    # directional decider back onto the live path from anywhere in the codebase.
    banned_calls: list["re.Pattern[str]"] = field(default_factory=list)
    scan_excludes: list[str] = field(default_factory=list)


def _p(path: str, pattern: str) -> "tuple[str, re.Pattern[str]]":
    return (path, re.compile(pattern))


# The invariants, grounded in the live-path forensic audit. As each redesign PR
# deletes/rewires the offending code, the corresponding violations disappear and
# the guard moves toward a clean (exit 0) live path.
CHECKS: list[Check] = [

    Check(
        id="provider_resource_accounting",
        clause="§21-§37: providers are finite, failure-prone cognitive resources; capacity, health, cooldowns and local/cloud diversity are first-class",
        pr="Provider-constitution guard",
        summary="Provider accounting/router primitives required by the APEX resource constitution are missing from the live codebase.",
        required_files=[
            "llm/provider_budget.py",
            "llm/health.py",
            "llm/model_manager.py",
            "llm/provider_tiers.py",
        ],
        required_patterns=[
            _p("llm/provider_budget.py", r"class\s+ProviderBudget\b"),
            _p("llm/provider_budget.py", r"def\s+estimate_tokens\b"),
            _p("llm/provider_budget.py", r"def\s+blocked_until\b"),
            _p("llm/model_manager.py", r"class\s+ModelManager\b"),
            _p("llm/model_manager.py", r"def\s+_ordered\b"),
            _p("llm/model_manager.py", r"def\s+describe\b"),
            _p("llm/health.py", r"class\s+CircuitBreaker\b"),
            _p("llm/health.py", r"def\s+bench\b"),
            _p("llm/provider_tiers.py", r"TIER_3"),
        ],
    ),
    Check(
        id="live_code_audit_mandate",
        clause="Audit mandate: documentation is not compliance; raw reachable code is the authority",
        pr="Constitutional audit mandate",
        summary="The executable audit mandate must be available so operators can launch live-code audits without relying on README prose.",
        required_files=["scripts/apex_live_code_audit_mandate.py"],
        required_patterns=[
            _p("scripts/apex_live_code_audit_mandate.py", r"AUDIT THE LIVE RAW CODE"),
            _p("scripts/apex_live_code_audit_mandate.py", r"VIOLATION REGISTER FORMAT"),
            _p("scripts/apex_live_code_audit_mandate.py", r"Provider constraints are operating reality"),
        ],
    ),
    Check(
        id="retired_entry_pipeline",
        clause="§XXX (a capability that is not reachable from the live path is not implemented) / §XXIX complete-cycle",
        pr="PR-A1",
        summary="Retired zone-touch / DecisionEngine ENTRY pipeline still present in source.",
        # Only the retired entry DECIDERS are asserted gone. entry/models.py
        # (EntryConfig / EntryZone data + config) and entry/flip_sequence_tracker.py
        # (+ entry/m1_confirmation.py) are retained SHARED, NON-DECIDER utilities
        # used by live analysis, so they are intentionally not listed here.
        exists=[
            "entry/zone_watcher.py",
            "entry/entry_gate.py",
            "entry/entry_orchestrator.py",
            "entry/flip_confirmer.py",
            "entry/tick_delta_analyzer.py",
            "trigger/entry_validator.py",
        ],
        patterns=[
            _p("event_driven_bootstrap.py", r"\b_RetiredEntryPipeline\b"),
            _p("event_driven_bootstrap.py", r"def\s+_evaluate_consensus_entry\b"),
            _p("event_driven_bootstrap.py", r"def\s+_on_entry_decision\b"),
            _p("scanner/candle_close_handler.py", r"\bextract_entry_zones\b"),
            _p("scanner/candle_close_handler.py", r"\bentry_quality_(long|short)\b"),
        ],
    ),
    Check(
        id="vote_consensus_engine",
        clause="§XXIX: analytical modules produce observations rather than trading votes",
        pr="PR-A2 / PR-B",
        summary="Directional vote/consensus engine and WorldModel vote panel still present.",
        exists=[
            "brain/directional_consensus.py",
            "brain/vote_evidence.py",
            "adaptive/vote_calibrator.py",
        ],
        patterns=[
            _p("brain/world_model.py", r"\bdef\s+votes_list\b"),
            _p("event_driven_bootstrap.py", r"\bdef\s+_cognition_vote_panel\b"),
        ],
    ),
    Check(
        id="module_directional_output",
        clause="§XXIX: analytical modules produce observations rather than trading votes; §IV Q5/Q6",
        pr="PR-B1",
        summary="Analytical modules still emit a directional opinion (LONG/SHORT/bias/expected_direction).",
        patterns=[
            _p("brain/wyckoff_engine.py", r"\bexpected_direction\s*[:=]"),
            _p("brain/inducement_detector.py", r"\bexpected_direction\s*[:=]"),
            _p("brain/volume_analyzer.py", r"\bconfirmation_bias\s*[:=]"),
            _p("brain/concept_modules.py", r"\bdirection\s*[:=]"),
            _p("brain/structure_engine.py", r"def\s+get_bias\b"),
        ],
    ),
    Check(
        id="brain_output_collapse",
        clause="§XXIX: information is not prematurely collapsed into LONG/SHORT/FLAT; §VI Q16/Q17; expected value is evaluated (Q13)",
        pr="PR-C2 / PR-C3",
        summary="Brain collapses cognition to direction+confidence and uses expected_value=confidence proxy.",
        patterns=[
            _p("cognition/brain.py", r"expected_value\s*=\s*confidence"),
        ],
    ),
    Check(
        id="directional_wake_trigger",
        clause="§XXIV event-driven integrity (Q101-103); §VII Q25 opportunity discovery not gated by a precomputed direction",
        pr="PR-E1",
        summary="Cognition is woken by a precomputed directional 'bias' rather than a raw market change.",
        patterns=[
            _p("event_driven_bootstrap.py", r'bias\.get\(\s*["\']direction["\']'),
            _p("event_driven_bootstrap.py", r"maybe_reason_on_change\([^)]*,\s*direction"),
            _p("cognition/loop.py", r"def maybe_reason_on_change\(self, symbol: str, direction"),
        ],
    ),
    Check(
        id="fail_open_ev_gate",
        clause="§IX Q35/Q36 execution conditions evaluated; §XXV silent-failure audit (Q104)",
        pr="PR-C2",
        summary="Net-EV execution gate fails OPEN (trades anyway) when cost/price cannot be estimated.",
        patterns=[
            _p("event_driven_bootstrap.py", r"return True\s*#\s*cannot qualify"),
        ],
    ),
    Check(
        id="legacy_single_path_flags",
        clause="§XXX (only one live decider; dead scaffolding is not the implementation)",
        pr="PR-H1",
        summary="Legacy single-path / shadow / legacy-audit scaffolding still present (only one path should remain).",
        exists=[
            "cognition/single_path.py",
            "cognition/legacy_audit.py",
        ],
        patterns=[
            _p("event_driven_bootstrap.py", r"def\s+_single_reasoner_path_active\b"),
        ],
    ),
    Check(
        id="retired_directional_deciders_removed",
        clause="§I/§III no analytical module casts a LONG/SHORT vote and no directional consensus substitutes for cognition; §IV higher timeframe is context, not command; §XXX a retired decider must not exist on the live path",
        pr="PR-V05/V06 (guard) + retired-decider removal",
        summary="A retired directional decider (DecisionEngine/SituationEngine/RiskGovernor/DecisionJournal vote+consensus pipeline, or the EntryEngine.calculate_entry H4 direction / range-edge gate) is present or invoked on the live path.",
        # The decider cluster has been physically REMOVED; these files must stay
        # gone and calculate_entry must not reappear. The banned_calls also fail
        # if any live-path file calls a retired decider (defence in depth). The
        # remaining cluster files are excluded so a re-added decider cannot hide
        # by cross-calling within the cluster.
        exists=[
            "decision/engine.py",
            "decision/situation.py",
            "decision/governor.py",
            "decision/journal.py",
            "decision/context.py",
        ],
        patterns=[
            _p("trigger/entry_engine.py", r"def\s+calculate_entry\b"),
        ],
        banned_calls=[
            re.compile(r"\.decide_entry\("),
            re.compile(r"\.decide_management\("),
            re.compile(r"\.calculate_entry\("),
            re.compile(r"\.assess_entry\("),
            re.compile(r"\.assess_open_trade\("),
        ],
        scan_excludes=[
            "decision/",                  # (now only actions.py) — no deciders
            "trigger/entry_engine.py",    # live zone/stop/target/sizing utilities
            "planning/trade_planner.py",  # retired plan_trade may cross-call assess_*
        ],
    ),
]


@dataclass
class Violation:
    check_id: str
    kind: str          # "file" | "pattern"
    path: str
    line: int
    snippet: str


def _scan_exists(check: Check) -> list[Violation]:
    out: list[Violation] = []
    for rel in check.exists:
        if (REPO_ROOT / rel).exists():
            out.append(Violation(check.id, "file", rel, 0, "retired module still present"))
    return out


def _scan_required(check: Check) -> list[Violation]:
    out: list[Violation] = []
    for rel in check.required_files:
        if not (REPO_ROOT / rel).exists():
            out.append(Violation(check.id, "missing-file", rel, 0, "required constitutional guard missing"))
    for rel, rx in check.required_patterns:
        fp = REPO_ROOT / rel
        if not fp.exists():
            # The missing file is already reported above when declared there.
            continue
        try:
            text = fp.read_text(encoding="utf-8", errors="replace")
        except OSError:
            out.append(Violation(check.id, "unreadable-file", rel, 0, "required constitutional guard unreadable"))
            continue
        if not rx.search(text):
            out.append(Violation(check.id, "missing-pattern", rel, 0, f"required pattern absent: {rx.pattern}"))
    return out


def _scan_patterns(check: Check) -> list[Violation]:
    out: list[Violation] = []
    for rel, rx in check.patterns:
        fp = REPO_ROOT / rel
        if not fp.exists():
            continue
        try:
            text = fp.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for i, line in enumerate(text.splitlines(), start=1):
            if rx.search(line):
                out.append(Violation(check.id, "pattern", rel, i, line.strip()[:160]))
    return out


# Directories never scanned by the tree walk (VCS / caches / vendored / non-py).
_SKIP_DIRS = frozenset({
    ".git", "__pycache__", "node_modules", ".venv", "venv", "build", "dist",
    ".mypy_cache", ".ruff_cache", ".pytest_cache", "frontend",
})
_THIS_FILE = Path(__file__).resolve()


def _scan_tree(check: Check) -> list[Violation]:
    """Flag any production line that CALLS a retired decider (``banned_calls``).

    Walks every ``*.py`` under the repo, skipping tests, this script, vendored /
    cache dirs, and the retired-cluster files in ``check.scan_excludes`` (a
    retired module cross-calling its own retired methods is not a live-path
    violation — only a call from OUTSIDE that cluster is). Empty ``banned_calls``
    ⇒ no-op."""
    if not check.banned_calls:
        return []
    excludes = tuple(check.scan_excludes)
    out: list[Violation] = []
    for fp in sorted(REPO_ROOT.rglob("*.py")):
        parts = fp.parts
        if any(d in _SKIP_DIRS for d in parts):
            continue
        if "tests" in parts or fp.name.startswith("test_"):
            continue
        if fp.resolve() == _THIS_FILE:
            continue
        rel = fp.relative_to(REPO_ROOT).as_posix()
        if any(rel == e or rel.startswith(e) for e in excludes):
            continue
        try:
            text = fp.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for i, line in enumerate(text.splitlines(), start=1):
            if any(rx.search(line) for rx in check.banned_calls):
                out.append(Violation(check.id, "call", rel, i, line.strip()[:160]))
    return out


def run() -> "list[tuple[Check, list[Violation]]]":
    results: list[tuple[Check, list[Violation]]] = []
    for check in CHECKS:
        violations = _scan_exists(check) + _scan_required(check) + _scan_patterns(check) + _scan_tree(check)
        results.append((check, violations))
    return results


def _print_human(results, quiet: bool) -> None:
    total = sum(len(v) for _, v in results)
    offending = [c for c, v in results if v]
    if not quiet:
        print("=" * 78)
        print("APEX TRADER — Constitutional live-path audit")
        print("=" * 78)
        for check, violations in results:
            mark = "FAIL" if violations else "ok"
            print(f"[{mark:>4}] {check.id}  ({check.pr})")
            if violations:
                print(f"        clause : {check.clause}")
                print(f"        detail : {check.summary}")
                for v in violations:
                    loc = v.path if v.kind == "file" else f"{v.path}:{v.line}"
                    print(f"          - {loc}  {v.snippet}")
        print("-" * 78)
    verdict = "CONSTITUTIONAL (clean live path)" if total == 0 else "NON-CONSTITUTIONAL"
    print(
        f"{verdict}: {total} violation(s) across {len(offending)}/{len(results)} invariants."
    )


def _print_json(results) -> None:
    payload = {
        "total_violations": sum(len(v) for _, v in results),
        "invariants": [
            {
                "id": c.id,
                "pr": c.pr,
                "clause": c.clause,
                "summary": c.summary,
                "violations": [
                    {"path": v.path, "line": v.line, "kind": v.kind, "snippet": v.snippet}
                    for v in vs
                ],
            }
            for c, vs in results
        ],
    }
    print(json.dumps(payload, indent=2))


def main(argv: "list[str] | None" = None) -> int:
    ap = argparse.ArgumentParser(description="APEX constitutional live-path guard")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--quiet", action="store_true", help="print only the summary line")
    args = ap.parse_args(argv)

    results = run()
    if args.json:
        _print_json(results)
    else:
        _print_human(results, quiet=args.quiet)

    total = sum(len(v) for _, v in results)
    return 1 if total else 0


if __name__ == "__main__":
    raise SystemExit(main())
