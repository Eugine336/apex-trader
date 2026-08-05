"""Tests for the competing-decision-code audit (Constitution III.2 / XI / XIV).

The audit turns the Definition-of-Done "no competing decision code remains"
grep audit into a machine-checkable gate. These tests pin (a) that it detects
the documented legacy deciders, (b) the regression guard — no *new* market-
deciding code outside the Brain, (c) that it is precise (evidence polarity and
membership tests are not decisions), and (d) that it reports ``clean`` only once
every competing surface is gone (the cutover-complete signal).
"""

import os
import tempfile

from cognition.legacy_audit import (
    KNOWN_LEGACY_SURFACES,
    CompetingDecisionAudit,
    audit_competing_decision_code,
    scan_file,
)

_KNOWN = {s.module for s in KNOWN_LEGACY_SURFACES}


# ── Live-tree audit (the real gate) ──────────────────────────────────────────

def test_audit_detects_known_legacy_emitters():
    audit = audit_competing_decision_code()
    present = set(audit.known_present)
    # brain/thesis_engine.py has been physically deleted (Single Reasoner
    # cutover, Stage 2) — the audit now reports it as *removed*, not present.
    # The remaining documented emitters are still present until their cutover.
    assert "brain/directional_consensus.py" in present
    assert "event_driven_bootstrap.py" in present
    assert "brain/thesis_engine.py" in audit.removed
    hit_modules = {f.module for f in audit.findings}
    assert "brain/directional_consensus.py" in hit_modules
    assert "event_driven_bootstrap.py" in hit_modules
    assert "brain/thesis_engine.py" not in hit_modules  # gone


def test_no_unexpected_competing_decision_code():
    # Regression guard: no NEW market-deciding code outside the sanctioned Brain
    # path. If this fails, a module started emitting buy/sell/hold/close/reverse
    # or a directional vote outside cognition/ — a Single-Reasoner violation.
    audit = audit_competing_decision_code()
    assert audit.unexpected == [], f"unexpected competing deciders: {audit.unexpected}"
    assert audit.regression_free is True


def test_not_yet_clean_before_cutover():
    # Legacy deciders are runtime-superseded (Brain gate authoritative) but not
    # yet physically removed, so the audit is not clean. When the validated
    # cutover deletes them this flips True — the Part XIV gate.
    audit = audit_competing_decision_code()
    assert audit.clean is False
    assert audit.scanned_files > 0


def test_to_dict_shape():
    d = audit_competing_decision_code().to_dict()
    for key in ("clean", "regression_free", "scanned_files", "known_present",
                "removed", "unexpected", "findings", "summary"):
        assert key in d
    assert isinstance(d["findings"], list)


# ── Precision + lifecycle on a synthetic tree ────────────────────────────────

def _write(root, rel, text):
    path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    return path


def test_detectors_precise_on_synthetic_tree():
    with tempfile.TemporaryDirectory() as root:
        # (a) genuine verdict emitter → must be flagged (unexpected: not registered)
        _write(root, "legacy/decider.py", "def decide():\n    return ('HOLD', 'x')\n")
        # (b) directional vote construction → flagged
        _write(root, "legacy/votes.py", "def v():\n    return VoteResult('LONG', 0.5)\n")
        # (c) membership test — NOT a verdict, must NOT match
        _write(root, "safe/membership.py",
               "def is_long(self):\n    return self.direction in ('BUY', 'LONG')\n")
        # (d) evidence polarity — leaning LONG/SHORT is allowed Evidence, not a decision
        _write(root, "safe/polarity.py",
               "def lean(x):\n    return 'LONG' if x > 0 else 'SHORT'\n")
        # (e) sanctioned Brain path — even a real verdict here is legitimate
        _write(root, "cognition/brainy.py", "def r():\n    return ('CLOSE', 'ok')\n")

        audit = audit_competing_decision_code(root=root)
        flagged = {f.module for f in audit.findings}
        assert "legacy/decider.py" in flagged
        assert "legacy/votes.py" in flagged
        assert "safe/membership.py" not in flagged
        assert "safe/polarity.py" not in flagged
        assert "cognition/brainy.py" not in flagged  # sanctioned
        # None of the synthetic modules are registered → all unexpected.
        assert set(audit.unexpected) == {"legacy/decider.py", "legacy/votes.py"}
        assert audit.clean is False


def test_detector_names_are_correct():
    with tempfile.TemporaryDirectory() as root:
        p_verdict = _write(root, "a.py", "return ('REVERSE', 1)\n")
        p_vote = _write(root, "b.py", "x = VoteResult('SHORT', 0.9)\n")
        assert scan_file(p_verdict) == [("verdict", 1)]
        assert scan_file(p_vote) == [("vote", 1)]


def test_clean_when_no_competing_code():
    # An empty/clean tree: no findings, every known surface counts as removed,
    # audit is clean — the cutover-complete state.
    with tempfile.TemporaryDirectory() as root:
        _write(root, "pkg/pure.py", "def add(a, b):\n    return a + b\n")
        audit = audit_competing_decision_code(root=root)
        assert audit.findings == []
        assert audit.clean is True
        assert audit.regression_free is True
        assert set(audit.removed) == _KNOWN  # all known surfaces absent here


def test_report_is_dataclass_instance():
    assert isinstance(audit_competing_decision_code(), CompetingDecisionAudit)
