"""Tests for the Single Reasoner path predicate (Constitution I.4 / III.2).

Pins the exact rule the live entry funnel uses to sever legacy entry authority:
under single-path, only the AI Brain (source == "ai_brain") may open a trade;
every legacy-sourced entry is suppressed; with single-path off nothing changes.
"""

from cognition.single_path import BRAIN_SOURCE, legacy_entry_suppressed
from cognition import legacy_entry_suppressed as exported  # lazy re-export works


def test_single_path_off_never_suppresses():
    for src in ("consensus", "zone", "reversal", "ai_brain", None, ""):
        assert legacy_entry_suppressed(src, False) is False


def test_single_path_suppresses_every_legacy_source():
    for src in ("consensus", "zone", "reversal", "trigger", "opportunity"):
        assert legacy_entry_suppressed(src, True) is True


def test_single_path_never_suppresses_the_brain():
    assert legacy_entry_suppressed("ai_brain", True) is False
    assert legacy_entry_suppressed("AI_BRAIN", True) is False   # case-insensitive
    assert legacy_entry_suppressed("  ai_brain  ", True) is False  # whitespace-tolerant


def test_none_and_odd_sources_treated_as_legacy_under_single_path():
    assert legacy_entry_suppressed(None, True) is True
    assert legacy_entry_suppressed("", True) is True
    assert legacy_entry_suppressed(123, True) is True


def test_brain_source_constant():
    assert BRAIN_SOURCE == "ai_brain"


def test_lazy_export_is_same_callable():
    assert exported is legacy_entry_suppressed
