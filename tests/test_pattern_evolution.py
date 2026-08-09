"""Pattern evolution (Article X) — the Brain learns which detected structural
patterns precede winning vs losing campaigns and evolves its rule set.

Covers the :class:`~cognition.pattern_evolution.PatternOutcomeTracker`:
origination/outcome recording + statistics, the neutral-below-min-samples and
active-when-significant confidence modifier, new-rule discovery that excludes
already-expressed patterns, thread safety, and bounded FIFO history.
"""

import threading

from cognition.pattern_evolution import PatternOutcomeTracker


def test_record_and_stats():
    tracker = PatternOutcomeTracker(min_samples=10)
    for i in range(10):
        tracker.record_origination("displacement_confirmed", "BTCUSD", "LONG", 0.7, float(i))
    for _ in range(7):
        tracker.record_outcome("displacement_confirmed", "BTCUSD", won=True, pnl_r=2.0)
    for _ in range(3):
        tracker.record_outcome("displacement_confirmed", "BTCUSD", won=False, pnl_r=-1.0)

    stats = tracker.pattern_stats("displacement_confirmed")
    assert stats["attempts"] == 10
    assert stats["wins"] == 7
    assert stats["losses"] == 3
    assert stats["pending"] == 0
    assert abs(stats["win_rate"] - 0.7) < 1e-9
    # avg over 7*2.0 + 3*(-1.0) = 11.0 / 10 = 1.1
    assert abs(stats["avg_pnl_r"] - 1.1) < 1e-9
    # modifier active (>= min_samples): 1 + 1.0 * (0.7 - 0.5) = 1.2
    assert abs(stats["confidence_modifier"] - 1.2) < 1e-9


def test_confidence_modifier_neutral_below_min_samples():
    tracker = PatternOutcomeTracker(min_samples=10)
    for _ in range(9):
        tracker.record_origination("structural_break", "ETHUSD", "LONG", 0.8, 0.0)
        tracker.record_outcome("structural_break", "ETHUSD", won=True, pnl_r=1.0)
    stats = tracker.pattern_stats("structural_break")
    assert stats["attempts"] == 9  # below min_samples
    assert stats["confidence_modifier"] == 1.0
    assert tracker.confidence_modifier("structural_break") == 1.0
    # A pattern never seen is also neutral.
    assert tracker.confidence_modifier("never_seen") == 1.0


def test_confidence_modifier_active():
    tracker = PatternOutcomeTracker(min_samples=10)
    for _ in range(12):
        tracker.record_origination("momentum_acceleration", "SOLUSD", "LONG", 0.8, 0.0)
    for _ in range(9):
        tracker.record_outcome("momentum_acceleration", "SOLUSD", won=True, pnl_r=1.5)
    for _ in range(3):
        tracker.record_outcome("momentum_acceleration", "SOLUSD", won=False, pnl_r=-1.0)
    mod = tracker.confidence_modifier("momentum_acceleration")
    # win_rate 0.75 → 1 + (0.75-0.5) = 1.25, within [0.7, 1.3]
    assert abs(mod - 1.25) < 1e-9
    assert 0.7 <= mod <= 1.3

    # A pattern that keeps winning is capped at the upper bound (bounded modifier).
    strong = PatternOutcomeTracker(min_samples=10)
    for _ in range(12):
        strong.record_origination("volume_confirmed_breakout", "XRPUSD", "LONG", 0.9, 0.0)
        strong.record_outcome("volume_confirmed_breakout", "XRPUSD", won=True, pnl_r=2.0)
    assert strong.confidence_modifier("volume_confirmed_breakout") == 1.3
    # A pattern that keeps losing is floored at the lower bound.
    weak = PatternOutcomeTracker(min_samples=10)
    for _ in range(12):
        weak.record_origination("weak_pattern", "USDJPY", "SHORT", 0.6, 0.0)
        weak.record_outcome("weak_pattern", "USDJPY", won=False, pnl_r=-1.0)
    assert weak.confidence_modifier("weak_pattern") == 0.7


def test_suggest_new_rules():
    tracker = PatternOutcomeTracker()
    log = []
    # Novel 3-domain combination that keeps winning → should be suggested.
    for i in range(6):
        log.append((["structure", "momentum", "order_flow"], "LONG", i < 5))  # 5/6 win
    # A combination already expressed by an existing rule (displacement_confirmed
    # keys on structure + volume) → must be EXCLUDED even though it wins.
    for i in range(6):
        log.append((["structure", "volume"], "LONG", True))
    # A losing combination → excluded by the 60% floor.
    for i in range(6):
        log.append((["liquidity", "volatility"], "SHORT", i < 2))  # 2/6 win
    # A winning but too-rare combination → excluded by the 5-occurrence floor.
    for i in range(3):
        log.append((["metals", "energy"], "LONG", True))

    suggestions = tracker.suggest_new_rules(log)
    names = {s["suggested_name"] for s in suggestions}
    domains = [tuple(s["domains"]) for s in suggestions]

    assert "learned_momentum_order_flow_structure" in names
    # existing pattern signature excluded
    assert ("structure", "volume") not in domains
    # low win-rate and rare combinations excluded
    assert ("liquidity", "volatility") not in domains
    assert ("energy", "metals") not in domains
    novel = next(s for s in suggestions if s["suggested_name"] == "learned_momentum_order_flow_structure")
    assert novel["occurrences"] == 6
    assert novel["win_rate"] >= 0.6


def test_thread_safety():
    tracker = PatternOutcomeTracker(max_records=100_000)
    n_threads, per_thread = 8, 100

    def worker(tid):
        for i in range(per_thread):
            tracker.record_origination(f"pattern_{tid}", f"SYM{tid}", "LONG", 0.7, float(i))

    threads = [threading.Thread(target=worker, args=(t,)) for t in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    status = tracker.get_status()
    assert status["total_records"] == n_threads * per_thread
    assert status["patterns_tracked"] == n_threads


def test_fifo_eviction():
    tracker = PatternOutcomeTracker(max_records=5)
    for i in range(7):
        tracker.record_origination(f"p{i}", f"SYM{i}", "LONG", 0.7, float(i))
    status = tracker.get_status()
    assert status["total_records"] == 5
    # The two oldest (p0, p1) were evicted; the five newest remain.
    assert tracker.pattern_stats("p0")["pending"] == 0
    assert tracker.pattern_stats("p1")["pending"] == 0
    assert tracker.pattern_stats("p2")["pending"] == 1
    assert tracker.pattern_stats("p6")["pending"] == 1
