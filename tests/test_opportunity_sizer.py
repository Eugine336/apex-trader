"""APEX TRADER — Opportunity Quality Sizer tests (GAP 3).

Covers: disabled = identity 1.0, boost for strong EV, cut for weak EV, neutral at
mid quality, [min_cut, max_boost] clamping, rank-aware blending, and crash safety.
"""

from brain.opportunity_sizer import OpportunityQualitySizer


def test_disabled_is_identity():
    s = OpportunityQualitySizer(enabled=False)
    assert s.multiplier(ev=5.0, confidence=1.0) == 1.0
    assert s.multiplier(ev=-5.0, confidence=0.0) == 1.0


def test_strong_ev_boosts():
    s = OpportunityQualitySizer(enabled=True, max_boost=1.3, ev_ref=1.0)
    m = s.multiplier(ev=1.0, confidence=0.9)
    assert m > 1.0
    assert m <= 1.3


def test_weak_ev_cuts():
    s = OpportunityQualitySizer(enabled=True, min_cut=0.7, ev_ref=1.0)
    m = s.multiplier(ev=-1.0, confidence=0.1)
    assert m < 1.0
    assert m >= 0.7


def test_neutral_quality_is_near_one():
    s = OpportunityQualitySizer(enabled=True, ev_ref=1.0)
    # ev=0 → ev_q=0.5, confidence=0.5 → quality 0.5 → ~1.0
    m = s.multiplier(ev=0.0, confidence=0.5)
    assert abs(m - 1.0) < 1e-6


def test_clamped_to_max_boost():
    s = OpportunityQualitySizer(enabled=True, max_boost=1.2, ev_ref=0.5)
    m = s.multiplier(ev=10.0, confidence=1.0)
    assert m == 1.2


def test_clamped_to_min_cut():
    s = OpportunityQualitySizer(enabled=True, min_cut=0.6, ev_ref=0.5)
    m = s.multiplier(ev=-10.0, confidence=0.0)
    assert m == 0.6


def test_rank_aware_top_beats_tail():
    s = OpportunityQualitySizer(enabled=True, ev_ref=1.0)
    top = s.multiplier(ev=0.5, confidence=0.5, rank=0, rank_total=4)
    tail = s.multiplier(ev=0.5, confidence=0.5, rank=3, rank_total=4)
    assert top > tail


def test_rank_total_one_ignored():
    s = OpportunityQualitySizer(enabled=True, ev_ref=1.0)
    # rank_total<=1 must not divide-by-zero; falls back to ev/conf blend.
    m = s.multiplier(ev=0.5, confidence=0.5, rank=0, rank_total=1)
    assert 0.7 <= m <= 1.3


def test_status_shape():
    s = OpportunityQualitySizer(enabled=True, max_boost=1.4, min_cut=0.5)
    st = s.status()
    assert st["enabled"] is True
    assert st["max_boost"] == 1.4
    assert st["min_cut"] == 0.5


def test_non_finite_inputs_safe():
    s = OpportunityQualitySizer(enabled=True)
    # NaN/inf must never crash; returns a value within bounds.
    m = s.multiplier(ev=float("nan"), confidence=float("inf"))
    assert 0.01 <= m <= s._max_boost
