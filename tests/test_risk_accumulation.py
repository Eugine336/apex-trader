"""Unit tests for the accumulated risk-scoring core (risk/risk_accumulation.py).

Pure-maths module — these tests prove the first-breach kill is replaced by a
graded, bounded multiplier that measures EVERY dimension.
"""

from risk import risk_accumulation as ra


# ── proximity ──────────────────────────────────────────────────────────────


def test_proximity_higher_is_riskier():
    assert ra.proximity(0.9, 1.8) == 0.5
    assert ra.proximity(1.8, 1.8) == 1.0
    assert ra.proximity(2.7, 1.8) == 1.5


def test_proximity_lower_is_riskier():
    # R:R of 2.0 vs a 1.0 minimum → well clear (proximity 0.5).
    assert ra.proximity(2.0, 1.0, lower_is_riskier=True) == 0.5
    # At the floor → proximity 1.0.
    assert ra.proximity(1.0, 1.0, lower_is_riskier=True) == 1.0
    # Below the floor → breached (>1).
    assert ra.proximity(0.5, 1.0, lower_is_riskier=True) == 2.0


def test_proximity_invalid_denominator_is_safe():
    assert ra.proximity(5.0, 0.0) == 0.0
    assert ra.proximity(0.0, 1.0, lower_is_riskier=True) == 2.0


# ── dimension / breach classification ────────────────────────────────────────


def test_dimension_breach_and_near_flags():
    clear = ra.dimension("heat", 1.0, 1.8)
    near = ra.dimension("heat", 1.5, 1.8)       # 0.83 → near
    over = ra.dimension("heat", 2.0, 1.8)       # 1.11 → breached
    assert not clear.near_breach and not clear.breached
    assert near.near_breach and not near.breached
    assert over.breached and not over.near_breach


# ── accumulate ───────────────────────────────────────────────────────────────


def test_accumulate_empty_is_neutral():
    score = ra.accumulate([])
    assert score.multiplier == 1.0
    assert score.score == 0.0
    assert score.near_breaches == [] and score.breaches == []


def test_accumulate_all_clear_is_neutral():
    dims = [ra.dimension("a", 0.5, 1.0), ra.dimension("b", 0.2, 1.0)]
    score = ra.accumulate(dims)
    assert score.multiplier == 1.0
    assert score.score == 0.5


def test_accumulate_does_not_stop_at_first_breach():
    # Two breached dimensions must BOTH be reported — the whole point of the
    # refactor (legacy first-breach would have hidden the second).
    dims = [
        ra.dimension("currency", 4, 3),       # breached
        ra.dimension("correlated", 3, 2),     # breached
    ]
    score = ra.accumulate(dims)
    assert set(score.breaches) == {"currency", "correlated"}
    assert score.multiplier < 1.0


def test_accumulate_two_near_dims_dim_more_than_one():
    one = ra.accumulate([ra.dimension("a", 1.7, 1.8)])
    two = ra.accumulate([
        ra.dimension("a", 1.7, 1.8),
        ra.dimension("b", 1.7, 1.8),
    ])
    assert two.multiplier < one.multiplier < 1.0


def test_accumulate_bounded_by_floor():
    dims = [ra.dimension("a", 100, 1), ra.dimension("b", 100, 1)]
    score = ra.accumulate(dims, floor=0.2)
    assert score.multiplier >= 0.2
    assert score.multiplier <= 1.0


def test_accumulate_score_is_nearest_limit():
    dims = [ra.dimension("a", 0.5, 1.0), ra.dimension("b", 0.95, 1.0)]
    score = ra.accumulate(dims)
    assert score.score == 0.95  # the closest limit drives the headline score


def test_accumulate_never_zero_multiplier():
    score = ra.accumulate([ra.dimension("a", 1e9, 1.0)], floor=0.15)
    assert score.multiplier == 0.15


def test_risk_score_to_dict_round_trip():
    score = ra.accumulate([ra.dimension("heat", 1.5, 1.8)])
    d = score.to_dict()
    assert d["near_breaches"] == ["heat"]
    assert 0.0 <= d["multiplier"] <= 1.0
    assert d["dimensions"][0]["name"] == "heat"
