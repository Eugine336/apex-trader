"""APEX TRADER — Competing-decision-code audit (Constitution III.2, XI, XIV).

The Constitution's Single Reasoner Principle (Part I Art 4; Part II) says only the
AI Cognitive Brain may reason about the market: *no other module may emit
buy/sell/hold/close/reverse* (Part III Art 2), and by the Definition of Done
(Part XIV) "no competing decision code remains" — verified by a **grep audit**.

Phase K's remaining tail (the *validated legacy-decider cutover*) is what makes
that literally true: the legacy directional/lifecycle deciders are physically
removed once a symbol group holds ``authoritative`` cleanly on the demo (Part XV
— no deletion until superseded **and** validated). Until then the Brain gate
already *runtime-supersedes* them (Steps C/D authoritative by default), so this
module does not delete anything; it turns the mandated grep audit into a
concrete, repeatable, offline-verifiable artifact that:

* **inventories** the known competing-decision surfaces slated for the cutover
  (:data:`KNOWN_LEGACY_SURFACES`) and reports which are still present vs already
  removed — a machine-checkable cutover checklist;
* **guards against regressions** — any *new* market-deciding code that appears
  outside the sanctioned Brain path (``cognition/``) is reported as
  ``unexpected`` so it fails CI before it can dilute the single authority;
* **closes the loop** — when the cutover is complete the audit reports
  ``clean == True``, which is exactly the Part XIV "no competing decision code
  remains" gate.

Two conservative, line-based detectors keep the audit precise (they target the
*decision vocabulary*, never the evidence *polarity* that Part III Art 3/4
explicitly require — a module may lean LONG/SHORT as Evidence; it may not return
a BUY/SELL/HOLD/CLOSE/REVERSE *verdict*):

* ``verdict`` — a lifecycle-decision literal returned as the first element of a
  ``return`` (e.g. ``return ("HOLD", ...)``); membership tests such as
  ``return self.direction in ("BUY", "LONG")`` and passthrough normalisations
  (``return "LONG" if ... else "SHORT"``) are *not* verdicts and do not match.
* ``vote`` — construction of a directional ``VoteResult(...)`` (legacy consensus).

Pure standard library; fail-safe — an unreadable file is skipped, never raised.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Optional

# ── Sanctioned paths ──────────────────────────────────────────────────────────
# Where decision vocabulary is legitimate: the Brain IS the decider (its
# ``DecisionType`` enum owns HOLD/OPEN/CLOSE/REVERSE); tests fixture it; docs
# describe it; the dashboard only displays it. Everything else that emits a
# decision verdict is competing authority.
SANCTIONED_PREFIXES = (
    "cognition/",
    "tests/",
    "docs/",
    "dashboard/",
)

# Sanctioned Evidence producers (Constitution III.4). These modules construct
# directional-lean ``VoteResult`` objects (LONG/SHORT/NEUTRAL + confidence +
# evidence), which are *Evidence* — NOT the III.2 lifecycle decisions
# (buy/sell/hold/close/reverse) this audit forbids. They emit no verdict and
# make no trade; they feed the Brain / analysis as observations only.
SANCTIONED_EVIDENCE_FILES = frozenset({
    "brain/vote_evidence.py",
})

# Directories never worth scanning.
_SKIP_DIRS = {
    ".git",
    "__pycache__",
    ".venv",
    "venv",
    "env",
    "node_modules",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "build",
    "dist",
}


@dataclass(frozen=True)
class LegacySurface:
    """A documented competing-decision surface scheduled for the cutover."""

    module: str  # repo-relative path
    authority: str  # the decision it emits today
    article: str  # the Constitution article it violates
    note: str  # how the cutover supersedes it


# The known legacy decision authorities (grounded in the current tree — every
# entry is a module that a detector actually matches today). As the validated
# cutover removes each one, it flips from ``present`` to ``removed`` in the audit
# and eventually the whole list is gone (``clean``).
KNOWN_LEGACY_SURFACES = (
    LegacySurface(
        module="brain/directional_consensus.py",
        authority="directional vote (VoteResult LONG/SHORT via consensus argmax)",
        article="III.2 (module emits direction) / I.4 (single authority)",
        note="RESOLVED — the vote-Evidence production (types + vote_from_* extractors) "
        "moved to the sanctioned Evidence module brain/vote_evidence.py (III.4); the "
        "remaining consensus decide/form_thesis are runtime-severed from trading by the "
        "single-path cutover (the Brain is sole decider). No VoteResult construction or "
        "lifecycle verdict remains here → reports as 'removed'.",
    ),
    LegacySurface(
        module="brain/thesis_engine.py",
        authority="thesis HOLD / exit verdicts",
        article="III.2 / VI.5 (management judgment)",
        note="RETIRED — physically deleted in the Single Reasoner cutover; the "
        "Brain owns entry/exit judgment and the thesis reads flow in as Evidence. "
        "Kept in the registry so the audit reports it as 'removed' (cutover progress)",
    ),
    LegacySurface(
        module="event_driven_bootstrap.py",
        authority="legacy management CLOSE/HOLD verdicts on the god-file eval path",
        article="III.2 / VI.4 (renewed authorization) / XI (god-file)",
        note="RETIRED — the candidate-scoped thesis exit verdict is neutralised "
        "(returns None); the Brain owns exit judgment, mechanical stops own safety. "
        "Reports as 'removed'. God-file decomposition of the coupled cores remains.",
    ),
)

_KNOWN_MODULES = frozenset(s.module for s in KNOWN_LEGACY_SURFACES)

# ── Detectors ───────────────────────────────────────────────────────────────
# A lifecycle-decision literal as the FIRST returned element (verdict), not a
# membership test or a passthrough normalisation.
_DETECTOR_VERDICT = re.compile(r"""return\s*\(?\s*['"](BUY|SELL|HOLD|CLOSE|REVERSE)['"]""")
# A directional vote construction (legacy consensus authority).
_DETECTOR_VOTE = re.compile(r"\bVoteResult\s*\(")

_DETECTORS = (("verdict", _DETECTOR_VERDICT), ("vote", _DETECTOR_VOTE))


@dataclass
class DecisionFinding:
    """One module found to emit a market decision, with the matching lines."""

    module: str
    detector: str
    count: int
    lines: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "module": self.module,
            "detector": self.detector,
            "count": self.count,
            "lines": list(self.lines),
        }


@dataclass
class CompetingDecisionAudit:
    """Result of the grep audit for competing decision authority."""

    findings: list = field(default_factory=list)  # list[DecisionFinding]
    known_present: list = field(default_factory=list)  # registered + still emitting
    removed: list = field(default_factory=list)  # registered but no longer emitting
    unexpected: list = field(default_factory=list)  # emitting + not registered + not sanctioned
    scanned_files: int = 0

    @property
    def clean(self) -> bool:
        """True iff NO competing decision code remains anywhere (Part XIV gate)."""
        return not self.findings

    @property
    def regression_free(self) -> bool:
        """True iff no *new* (unexpected) competing decision code exists."""
        return not self.unexpected

    @property
    def summary(self) -> str:
        return (
            f"scanned {self.scanned_files} files; "
            f"{len(self.known_present)} known legacy present, "
            f"{len(self.removed)} removed, "
            f"{len(self.unexpected)} unexpected; "
            f"clean={self.clean}"
        )

    def to_dict(self) -> dict:
        return {
            "clean": self.clean,
            "regression_free": self.regression_free,
            "scanned_files": self.scanned_files,
            "known_present": list(self.known_present),
            "removed": list(self.removed),
            "unexpected": list(self.unexpected),
            "findings": [f.to_dict() for f in self.findings],
            "summary": self.summary,
        }


def _repo_root() -> str:
    """The repository root (the parent of the ``cognition`` package)."""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _rel(path: str, root: str) -> str:
    return os.path.relpath(path, root).replace(os.sep, "/")


def _is_sanctioned(rel_path: str) -> bool:
    return rel_path in SANCTIONED_EVIDENCE_FILES or any(
        rel_path.startswith(p) for p in SANCTIONED_PREFIXES
    )


def _iter_py_files(root: str):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        for name in filenames:
            if name.endswith(".py"):
                yield os.path.join(dirpath, name)


def scan_file(path: str) -> list:
    """Return ``[(detector, lineno), ...]`` for one file. Fail-safe (skips on error)."""
    hits: list = []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for lineno, line in enumerate(fh, start=1):
                for detector_name, pattern in _DETECTORS:
                    if pattern.search(line):
                        hits.append((detector_name, lineno))
    except OSError:
        return []
    return hits


def audit_competing_decision_code(root: Optional[str] = None) -> CompetingDecisionAudit:
    """Grep-audit the repo for competing decision authority (Part XIV gate).

    Scans every ``.py`` file outside the sanctioned Brain path for the decision
    vocabulary; buckets the hits into ``known_present`` (documented cutover
    scope), ``removed`` (registered surfaces that no longer emit — cutover
    done for them), and ``unexpected`` (new competing code — a regression).
    """
    base = root or _repo_root()
    findings: list = []
    hit_modules: set = set()
    scanned = 0

    for path in _iter_py_files(base):
        rel = _rel(path, base)
        scanned += 1
        if _is_sanctioned(rel):
            continue
        # Skip this audit module itself (its regex literals are not decisions).
        if rel == "cognition/legacy_audit.py":
            continue
        hits = scan_file(path)
        if not hits:
            continue
        hit_modules.add(rel)
        by_detector: dict = {}
        for detector_name, lineno in hits:
            by_detector.setdefault(detector_name, []).append(lineno)
        for detector_name, lines in sorted(by_detector.items()):
            findings.append(
                DecisionFinding(
                    module=rel,
                    detector=detector_name,
                    count=len(lines),
                    lines=sorted(lines),
                )
            )

    known_present = sorted(m for m in hit_modules if m in _KNOWN_MODULES)
    removed = sorted(m for m in _KNOWN_MODULES if m not in hit_modules)
    unexpected = sorted(m for m in hit_modules if m not in _KNOWN_MODULES)

    return CompetingDecisionAudit(
        findings=findings,
        known_present=known_present,
        removed=removed,
        unexpected=unexpected,
        scanned_files=scanned,
    )
