"""
Tests for the Capital Allocation Engine (adaptive/capital_allocator.py, L5.5a)
and its TunerAgent adapter (adaptive.tunable_adapters.CapitalAllocatorTunable).

Covers fingerprint derivation, three-horizon weighted scoring, Bayesian
shrinkage on thin samples, the allocation split (sums to 1.0), the min-allocation
floor, rate-limited rebalancing, anti-thrashing under whipsaw, SQLite
persistence round-trips, the cold-start no-op (1.0 multiplier identical to the
pre-allocator pipeline), the RiskEngine sizing-chain integration, Tunable
protocol compliance, rebalance-interval enforcement, and multi-fingerprint
profiles.

Deterministic: feeds fixed R-multiple streams and uses temp SQLite DBs so
nothing touches the real data/ dir.
"""

import os
import tempfile

import pytest

from config import CapitalAllocationConfig
from adaptive.capital_allocator import (
    CapitalAllocator,
    FingerprintStats,
    compute_fingerprint,
)
from adaptive.tunable import Tunable, TuneContext, TuneFrequency
from adaptive.tunable_adapters import CapitalAllocatorTunable


# ── Fixtures / helpers ──────────────────────────────────────────────────────


def _close(a, b, eps=1e-6):
    """Tolerant float compare.

    Used instead of ``pytest.approx`` because a sibling test module installs a
    stubbed ``numpy`` into ``sys.modules`` at import time, which breaks
    ``pytest.approx`` (it probes ``np.bool_``) for any test collected after it.
    A plain stdlib comparison keeps this suite order-independent.
    """
    return abs(float(a) - float(b)) <= eps


@pytest.fixture()
def db_path():
    d = tempfile.mkdtemp()
    return os.path.join(d, "capital_allocation.db")


def _make(db_path, **overrides):
    kwargs = dict(
        db_path=db_path,
        enabled=True,
        rebalance_interval_trades=10,
        min_trades_for_scoring=50,
        min_allocation=0.05,
        max_allocation_shift=0.10,
    )
    kwargs.update(overrides)
    return CapitalAllocator(**kwargs)


def _feed(alloc, fingerprint, r, n):
    for _ in range(n):
        alloc.record_outcome(fingerprint, r)


# ── 1. Fingerprint computation ──────────────────────────────────────────────


def test_fingerprint_from_mode_and_horizon():
    assert compute_fingerprint("MARKET", "SCALP") == "MARKET|SCALP"
    assert compute_fingerprint("pending", "swing") == "PENDING|SWING"


def test_fingerprint_normalises_empty_parts_to_any():
    assert compute_fingerprint("MARKET", "") == "MARKET|ANY"
    assert compute_fingerprint("", "SWING") == "ANY|SWING"


def test_fingerprint_all_empty_is_unknown_sentinel():
    assert compute_fingerprint("", "") == "unknown"
    assert compute_fingerprint(None, None) == "unknown"


def test_fingerprint_optional_extra_axis():
    assert compute_fingerprint("MARKET", "SCALP", extra="choch") == "MARKET|SCALP|CHOCH"
    # empty extra is omitted entirely
    assert compute_fingerprint("MARKET", "SCALP", extra="") == "MARKET|SCALP"


# ── 2. Three-horizon weighted scoring ───────────────────────────────────────


def test_three_horizon_weighting_long_dominates():
    """A fingerprint that was strong long-term but weak recently should still
    score positive because the long horizon carries 0.5 weight."""
    d = tempfile.mkdtemp()
    alloc = _make(os.path.join(d, "c.db"), min_trades_for_scoring=1)
    # 5000-trade history: mostly +1.0, with the most-recent 50 at -1.0.
    fp = "MARKET|SWING"
    _feed(alloc, fp, 1.0, 600)   # older winners
    _feed(alloc, fp, -1.0, 50)   # recent losers (newest)
    e_s, e_m, e_l, blended, n = alloc._blended_score(fp)
    assert _close(e_s, -1.0, 1e-6)       # last 50 all losers
    assert e_l > 0.0                                   # long horizon still net +
    # blend = 0.2*(-1) + 0.3*e_m + 0.5*e_l ; long dominance keeps it from -1
    assert blended > e_s


def test_blended_uses_available_windows_when_thin():
    d = tempfile.mkdtemp()
    alloc = _make(os.path.join(d, "c.db"), min_trades_for_scoring=1)
    fp = "MARKET|SCALP"
    _feed(alloc, fp, 0.5, 5)   # only 5 trades — all three windows see the same 5
    e_s, e_m, e_l, blended, n = alloc._blended_score(fp)
    assert n == 5
    assert _close(blended, 0.5, 1e-6)


# ── 3. Bayesian shrinkage with few trades ────────────────────────────────────


def test_bayesian_shrinkage_curbs_thin_outlier():
    """A tiny lucky sample (+3R over 3 trades) must not run away with the book.
    Strong shrinkage (large prior) pulls it toward the book average, so its
    allocation is far smaller than with negligible shrinkage."""
    d = tempfile.mkdtemp()

    strong = _make(
        os.path.join(d, "strong.db"), min_trades_for_scoring=1, bayesian_prior_trades=100,
    )
    _feed(strong, "PENDING|SWING", 1.0, 400)
    _feed(strong, "MARKET|SCALP", 3.0, 3)
    strong.rebalance(force=True)
    scalp_strong = strong.get_allocation("MARKET|SCALP")

    weak = _make(
        os.path.join(d, "weak.db"), min_trades_for_scoring=1, bayesian_prior_trades=1,
    )
    _feed(weak, "PENDING|SWING", 1.0, 400)
    _feed(weak, "MARKET|SCALP", 3.0, 3)
    weak.rebalance(force=True)
    scalp_weak = weak.get_allocation("MARKET|SCALP")

    # With minimal shrinkage the 3-trade outlier dominates; strong shrinkage
    # materially curbs it.
    assert scalp_weak > 0.8
    assert scalp_strong < scalp_weak - 0.2


# ── 4. Allocations sum to 1.0 ────────────────────────────────────────────────


def test_allocations_sum_to_one(db_path):
    alloc = _make(db_path, min_trades_for_scoring=1)
    _feed(alloc, "MARKET|SCALP", 0.3, 40)
    _feed(alloc, "PENDING|SWING", 0.9, 40)
    _feed(alloc, "MARKET|SWING", -0.2, 40)
    result = alloc.rebalance(force=True)
    assert result
    assert _close(sum(result.values()), 1.0, 1e-6)


# ── 5. Rate-limited rebalancing (max shift per cycle) ─────────────────────────


def test_rate_limit_caps_allocation_shift_per_rebalance():
    d = tempfile.mkdtemp()
    alloc = _make(
        os.path.join(d, "c.db"), min_trades_for_scoring=1, max_allocation_shift=0.10,
    )
    # First book: two roughly even styles.
    _feed(alloc, "A|X", 0.5, 50)
    _feed(alloc, "B|Y", 0.5, 50)
    first = alloc.rebalance(force=True)
    # Now make A wildly better and rebalance — A's allocation can't jump > 0.10.
    _feed(alloc, "A|X", 5.0, 50)
    second = alloc.rebalance(force=True)
    delta = abs(second["A|X"] - first["A|X"])
    assert delta <= 0.10 + 1e-6


# ── 6. Minimum allocation floor ──────────────────────────────────────────────


def test_min_allocation_floor_keeps_weak_style_alive():
    d = tempfile.mkdtemp()
    alloc = _make(
        os.path.join(d, "c.db"), min_trades_for_scoring=1, min_allocation=0.05,
        max_allocation_shift=1.0,
    )
    _feed(alloc, "STRONG|SWING", 2.0, 200)
    _feed(alloc, "WEAK|SCALP", -2.0, 200)
    result = alloc.rebalance(force=True)
    assert result["WEAK|SCALP"] >= 0.05 - 1e-9
    assert _close(sum(result.values()), 1.0, 1e-6)


def test_infeasible_floor_falls_back_to_equal_split():
    d = tempfile.mkdtemp()
    # floor 0.5 with 3 fingerprints is infeasible (1.5 > 1) → equal split.
    alloc = _make(
        os.path.join(d, "c.db"), min_trades_for_scoring=1, min_allocation=0.5,
        max_allocation_shift=1.0,
    )
    for fp in ("A|X", "B|Y", "C|Z"):
        _feed(alloc, fp, 1.0, 20)
    result = alloc.rebalance(force=True)
    for v in result.values():
        assert _close(v, 1.0 / 3.0, 1e-6)


# ── 7. Anti-thrashing under whipsaw ──────────────────────────────────────────


def test_anti_thrashing_no_whiplash_on_rapid_swings():
    d = tempfile.mkdtemp()
    alloc = _make(
        os.path.join(d, "c.db"), min_trades_for_scoring=1, max_allocation_shift=0.10,
    )
    _feed(alloc, "A|X", 1.0, 500)
    _feed(alloc, "B|Y", 1.0, 500)
    alloc.rebalance(force=True)
    moves = []
    prev = dict(alloc._allocations)
    # Alternate which style "wins" each cycle; allocation must crawl, not flip.
    for cycle in range(6):
        winner = "A|X" if cycle % 2 == 0 else "B|Y"
        _feed(alloc, winner, 4.0, 30)
        cur = alloc.rebalance(force=True)
        moves.append(abs(cur["A|X"] - prev["A|X"]))
        prev = dict(cur)
    assert max(moves) <= 0.10 + 1e-6  # long-horizon + rate limit prevent flips


# ── 8. SQLite persistence round-trip ─────────────────────────────────────────


def test_persistence_round_trip(db_path):
    alloc = _make(db_path, min_trades_for_scoring=1)
    _feed(alloc, "MARKET|SCALP", 0.4, 30)
    _feed(alloc, "PENDING|SWING", 1.1, 30)
    saved = alloc.rebalance(force=True)
    alloc.close()

    reopened = _make(db_path, min_trades_for_scoring=1)
    for fp, a in saved.items():
        assert _close(reopened.get_allocation(fp), a, 1e-6)
    assert reopened.total_trades() == 60


# ── 9. Cold-start no-op (multiplier 1.0 == pre-allocator behaviour) ──────────


def test_cold_start_sizing_multiplier_is_noop(db_path):
    alloc = _make(db_path, min_trades_for_scoring=50)
    # No trades at all.
    assert alloc.get_sizing_multiplier("MARKET|SCALP") == 1.0
    # Below the scoring floor — still a no-op.
    _feed(alloc, "MARKET|SCALP", 1.0, 10)
    alloc.rebalance(force=True)
    assert alloc.get_sizing_multiplier("MARKET|SCALP") == 1.0


def test_disabled_allocator_is_noop(db_path):
    alloc = _make(db_path, enabled=False, min_trades_for_scoring=1)
    _feed(alloc, "MARKET|SCALP", 1.0, 100)
    assert alloc.rebalance(force=True) is None
    assert alloc.get_sizing_multiplier("MARKET|SCALP") == 1.0


def test_unknown_fingerprint_never_penalised(db_path):
    alloc = _make(db_path, min_trades_for_scoring=1)
    _feed(alloc, "MARKET|SCALP", 1.0, 60)
    alloc.rebalance(force=True)
    # A brand-new style not yet in the book sizes at full — never penalised.
    assert alloc.get_sizing_multiplier("BRAND|NEW") == 1.0


# ── 10. Multiplier semantics: strongest = 1.0, weaker scaled down ────────────


def test_strongest_fingerprint_sizes_full_weaker_scaled_down(db_path):
    alloc = _make(db_path, min_trades_for_scoring=1, max_allocation_shift=1.0)
    _feed(alloc, "STRONG|SWING", 2.0, 200)
    _feed(alloc, "WEAK|SCALP", -1.0, 200)
    alloc.rebalance(force=True)
    strong = alloc.get_sizing_multiplier("STRONG|SWING")
    weak = alloc.get_sizing_multiplier("WEAK|SCALP")
    assert _close(strong, 1.0, 1e-6)   # best style at full size
    assert weak < strong                             # weak style de-risked
    assert weak >= alloc.min_allocation - 1e-9       # never below the floor


# ── 11. RiskEngine sizing-chain integration ──────────────────────────────────


def test_risk_engine_applies_strategy_allocation():
    from risk.risk_engine import RiskEngine

    re = RiskEngine()
    hwm = {"is_at_peak": True, "drawdown_from_peak_pct": 0.0}
    full = re.compute_position_size_risk(
        base_risk_pct=0.01, conviction=0.9, score=85, hwm_state=hwm,
        strategy_allocation=1.0,
    )
    half = re.compute_position_size_risk(
        base_risk_pct=0.01, conviction=0.9, score=85, hwm_state=hwm,
        strategy_allocation=0.5,
    )
    default = re.compute_position_size_risk(
        base_risk_pct=0.01, conviction=0.9, score=85, hwm_state=hwm,
    )
    assert _close(half, full * 0.5, 1e-6)
    assert default == full          # default 1.0 leaves the chain unchanged
    # de-risking only: allocation can never inflate above base*other factors
    assert full <= re._RISK_PCT_CAP


def test_risk_engine_allocation_clamped_to_unit_interval():
    from risk.risk_engine import RiskEngine

    re = RiskEngine()
    hwm = {"is_at_peak": True, "drawdown_from_peak_pct": 0.0}
    base = re.compute_position_size_risk(
        base_risk_pct=0.01, conviction=0.9, score=85, hwm_state=hwm,
        strategy_allocation=1.0,
    )
    # An out-of-range allocation (>1) is clamped to 1.0 — never amplifies.
    over = re.compute_position_size_risk(
        base_risk_pct=0.01, conviction=0.9, score=85, hwm_state=hwm,
        strategy_allocation=5.0,
    )
    assert over == base


# ── 12. Tunable protocol + rebalance interval ────────────────────────────────


def test_tunable_protocol_compliance(db_path):
    alloc = _make(db_path)
    t = CapitalAllocatorTunable(alloc, min_trades=10)
    assert isinstance(t, Tunable)
    assert t.tunable_name == "capital_allocator"
    assert t.frequency == TuneFrequency.ON_TRADE_BATCH
    ok, _ = t.validate_params(alloc.get_current_params())
    assert ok


def test_tunable_tune_rebalances_and_snapshots(db_path):
    alloc = _make(db_path, min_trades_for_scoring=1)
    _feed(alloc, "MARKET|SCALP", 0.5, 30)
    _feed(alloc, "PENDING|SWING", 1.0, 30)
    t = CapitalAllocatorTunable(alloc, min_trades=10)
    result = t.tune(TuneContext(total_trades=60))
    assert result.success
    assert "rebalanced" in result.reason
    assert result.params_before == result.params_after  # rebalance doesn't move knobs


def test_tunable_rollback_restores_params(db_path):
    alloc = _make(db_path)
    t = CapitalAllocatorTunable(alloc)
    t._begin()  # snapshot current params
    alloc.short_weight = 0.99
    assert t.rollback()
    assert _close(alloc.short_weight, 0.2, 1e-6)


def test_tunable_validate_rejects_bad_params(db_path):
    alloc = _make(db_path)
    t = CapitalAllocatorTunable(alloc)
    ok, _ = t.validate_params({"min_allocation": 1.5})
    assert not ok
    ok, _ = t.validate_params({"max_allocation_shift": 0.0})
    assert not ok
    ok, _ = t.validate_params({"rebalance_interval_trades": 0})
    assert not ok


def test_rebalance_interval_enforced(db_path):
    alloc = _make(db_path, rebalance_interval_trades=10, min_trades_for_scoring=1)
    for _ in range(9):
        alloc.record_outcome("MARKET|SCALP", 1.0)
    assert not alloc.due_for_rebalance()
    assert alloc.maybe_rebalance() is False
    alloc.record_outcome("MARKET|SCALP", 1.0)  # 10th
    assert alloc.due_for_rebalance()
    assert alloc.maybe_rebalance() is True
    assert not alloc.due_for_rebalance()       # counter reset after rebalance


# ── 13. Multiple fingerprints with different profiles ────────────────────────


def test_multiple_fingerprints_ranked_by_expectancy(db_path):
    alloc = _make(db_path, min_trades_for_scoring=1, max_allocation_shift=1.0)
    _feed(alloc, "PENDING|SWING", 1.5, 100)    # best
    _feed(alloc, "MARKET|SCALP", 0.4, 100)     # middling
    _feed(alloc, "LIMIT|MIXED", -0.8, 100)     # worst
    alloc.rebalance(force=True)
    a_best = alloc.get_allocation("PENDING|SWING")
    a_mid = alloc.get_allocation("MARKET|SCALP")
    a_worst = alloc.get_allocation("LIMIT|MIXED")
    assert a_best > a_mid > a_worst
    state = alloc.get_state()
    assert state["fingerprint_count"] == 3
    # get_state is dashboard-shaped and ordered best-first.
    assert state["fingerprints"][0]["fingerprint"] == "PENDING|SWING"


def test_get_state_shape_when_idle(db_path):
    alloc = _make(db_path)
    state = alloc.get_state()
    assert state["enabled"] is True
    assert state["active"] is False
    assert state["total_trades"] == 0
    assert state["fingerprints"] == []


# ── 14. Config wiring ─────────────────────────────────────────────────────────


def test_config_defaults_active():
    cfg = CapitalAllocationConfig()
    assert cfg.enabled is True
    assert _close(cfg.short_weight + cfg.medium_weight + cfg.long_weight, 1.0)
    assert cfg.long_weight > cfg.short_weight  # long horizon dominates


def test_config_rejects_bad_values():
    with pytest.raises(ValueError):
        CapitalAllocationConfig(min_allocation=1.5)
    with pytest.raises(ValueError):
        CapitalAllocationConfig(max_allocation_shift=0.0)
    with pytest.raises(ValueError):
        CapitalAllocationConfig(allocation_temperature=0.0)


def test_fingerprint_stats_dataclass_defaults():
    s = FingerprintStats(fingerprint="MARKET|SCALP")
    assert s.sizing_multiplier == 1.0
    assert s.allocation == 0.0
