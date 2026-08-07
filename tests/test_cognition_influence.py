"""Tests for adaptive influence + Brain calibration (Part VIII)."""

from cognition.contracts import Evidence, EvidenceDomain, MarketState
from cognition.influence import CalibrationTracker, InfluenceLedger


# ── InfluenceLedger ────────────────────────────────────────────────────────────

def test_weight_is_neutral_below_min_samples():
    led = InfluenceLedger(min_samples=10)
    for _ in range(5):
        led.observe("mod.a", won=True)
    assert led.weight_for("mod.a") == 1.0     # not yet significant


def test_winning_source_earns_influence_above_one():
    led = InfluenceLedger(min_samples=10, max_weight=1.5, gain=1.0)
    for _ in range(10):
        led.observe("mod.a", won=True)        # 100% win rate
    w = led.weight_for("mod.a")
    assert w > 1.0 and w <= 1.5


def test_losing_source_loses_influence_below_one():
    led = InfluenceLedger(min_samples=10, min_weight=0.5, gain=1.0)
    for _ in range(10):
        led.observe("mod.b", won=False)       # 0% win rate
    w = led.weight_for("mod.b")
    assert w < 1.0 and w >= 0.5


def test_weight_is_bounded():
    led = InfluenceLedger(min_samples=4, min_weight=0.5, max_weight=1.5, gain=100.0)
    for _ in range(4):
        led.observe("hot", won=True)
    for _ in range(4):
        led.observe("cold", won=False)
    assert led.weight_for("hot") == 1.5
    assert led.weight_for("cold") == 0.5


def test_observe_many_and_status():
    led = InfluenceLedger(min_samples=2)
    led.observe_many(["a", "b"], won=True)
    led.observe_many(["a", "b"], won=False)
    st = led.get_status()
    assert st["tracked_sources"] == 2
    assert "a" in st["sources"]


def test_ledger_fail_safe_on_blank_source():
    led = InfluenceLedger()
    led.observe("", won=True)                 # no raise, ignored
    led.observe_many(None, won=True)
    assert led.weight_for("") == 1.0


# ── Weighted consolidation (MarketState) ──────────────────────────────────────

def _ev(src, polarity, conf=0.8):
    return Evidence(source_module=src, domain=EvidenceDomain.MOMENTUM,
                    symbol="EURUSD", polarity=polarity, confidence=conf)


def test_consolidation_unweighted_by_default():
    ms = MarketState(symbol="EURUSD")
    ms.add(_ev("a", 0.8))
    ms.add(_ev("b", -0.8))
    c = ms.consolidation()
    assert c["influence_weighted"] is False
    # Part XXV — evidence is non-directional, so there is no directional conflict.
    assert c["conflict_ratio"] == 0.0


def test_consolidation_weighting_shifts_weighted_mean():
    ms = MarketState(symbol="EURUSD")
    ms.add(_ev("trusted", 0.0, conf=0.9))
    ms.add(_ev("noisy", 0.0, conf=0.3))
    unweighted = ms.consolidation()["mean_confidence"]
    # Trust the high-confidence source far more → weighted mean rises toward it.
    ms.influence_weights = {"trusted": 1.5, "noisy": 0.1}
    c = ms.consolidation()
    assert c["influence_weighted"] is True
    assert c["mean_confidence"] > unweighted


def test_consolidation_weighted_matches_unweighted_when_all_one():
    ms = MarketState(symbol="EURUSD")
    ms.add(_ev("a", 0.0, conf=0.8))
    ms.add(_ev("b", 0.0, conf=0.4))
    base = ms.consolidation()["mean_confidence"]
    ms.influence_weights = {"a": 1.0, "b": 1.0}
    assert ms.consolidation()["mean_confidence"] == base


# ── CalibrationTracker ─────────────────────────────────────────────────────────

def test_calibration_perfect_has_zero_gap():
    cal = CalibrationTracker()
    # Confidence 1.0 always won, 0.0 always lost → perfectly calibrated aggregate.
    for _ in range(5):
        cal.observe(1.0, won=True)
    for _ in range(5):
        cal.observe(0.0, won=False)
    m = cal.metrics()
    assert m["samples"] == 10
    assert m["reliability_gap"] == 0.0
    assert m["brier"] == 0.0


def test_calibration_overconfident_has_gap_and_brier():
    cal = CalibrationTracker()
    # Always says 0.9 confident but only wins half the time.
    for i in range(10):
        cal.observe(0.9, won=(i % 2 == 0))
    m = cal.metrics()
    assert m["mean_confidence"] == 0.9
    assert m["win_rate"] == 0.5
    assert m["reliability_gap"] == 0.4
    assert m["brier"] > 0.0


def test_calibration_empty_is_safe():
    assert CalibrationTracker().metrics()["samples"] == 0
