"""Tests for the concept direction-flip in ``brain.decision_core.blend_concepts``.

By default the blend only nudges the bias *score* and keeps the structural
*direction* authoritative. When the absolute net concept vote clears
``concept_flip_threshold`` AND opposes structure, the blended direction is
allowed to flip so overwhelming convergent market evidence is not discarded.
"""

from __future__ import annotations

from types import SimpleNamespace

from brain.decision_core import blend_concepts


def _sig(name: str, direction: str, strength: float):
    return SimpleNamespace(
        name=name, direction=direction, strength=strength, is_directional=True
    )


def _bias(direction: str = "LONG", score: int = 60) -> dict:
    return {"direction": direction, "score": score, "tradeable": True}


class TestConceptFlipDisabled:
    def test_no_flip_when_threshold_zero(self):
        # Strong opposing concepts, but flipping disabled (default).
        concepts = {"M5": [_sig("a", "SHORT", 1.0), _sig("b", "SHORT", 1.0),
                           _sig("c", "SHORT", 1.0)]}
        out = blend_concepts(_bias("LONG"), concepts, {})
        assert out["direction"] == "LONG"  # direction preserved
        assert out.get("concept_flip") is False

    def test_score_only_nudge_below_threshold(self):
        concepts = {"M5": [_sig("a", "SHORT", 0.5)]}  # net = -0.5
        out = blend_concepts(_bias("LONG", 60), concepts, {},
                             concept_flip_threshold=3.0)
        assert out["direction"] == "LONG"
        assert out["score"] < 60  # opposing concept shaved the score
        assert out.get("concept_flip") is False


class TestConceptFlipEnabled:
    def test_flip_when_concepts_overwhelm_structure(self):
        # net = -3.0 (three full-strength SHORT concepts) clears the threshold.
        concepts = {"M5": [_sig("a", "SHORT", 1.0), _sig("b", "SHORT", 1.0),
                           _sig("c", "SHORT", 1.0)]}
        out = blend_concepts(_bias("LONG"), concepts, {},
                             concept_flip_threshold=3.0)
        assert out["direction"] == "SHORT"
        assert out["concept_flip"] is True
        assert out["concept_flip_from"] == "LONG"

    def test_no_flip_when_concepts_agree_with_structure(self):
        concepts = {"M5": [_sig("a", "LONG", 1.0), _sig("b", "LONG", 1.0),
                           _sig("c", "LONG", 1.0)]}
        out = blend_concepts(_bias("LONG"), concepts, {},
                             concept_flip_threshold=3.0)
        assert out["direction"] == "LONG"
        assert out.get("concept_flip") is False

    def test_no_flip_when_structure_has_no_direction(self):
        concepts = {"M5": [_sig("a", "SHORT", 1.0), _sig("b", "SHORT", 1.0),
                           _sig("c", "SHORT", 1.0)]}
        out = blend_concepts({"direction": "", "score": 0}, concepts, {},
                             concept_flip_threshold=3.0)
        assert out["direction"] == ""  # nothing to flip against
        assert out.get("concept_flip") is False
