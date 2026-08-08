"""
Tests for brain-level sizing inputs: OpportunityDensityTracker and
SystemVolatilityMonitor.

Covers:
  - Density cadence-independence (same symbols, different scan frequencies → same tier)
  - Persistence dedup (one symbol READY across many scans → counted once)
  - Tier/multiplier correctness (LOW/NORMAL/HIGH thresholds)
  - LOW multiplier is exactly 1.0 (no boost)
  - Eviction removes stale symbols
  - Vol-monitor unclassifiable-pair dilution fix
  - Vol-monitor all-unclassifiable → NORMAL
  - Vol-monitor regression guard for normal mixed input
"""

from datetime import datetime, timedelta, timezone

from brain.opportunity_density import OpportunityDensityTracker
from brain.regime_detector import MarketRegime, RegimeAnalysis, SystemVolatilityMonitor

# ─── Helpers ────────────────────────────────────────────────────────────────


def _make_analysis(volatility_ratio: float) -> RegimeAnalysis:
    return RegimeAnalysis(
        regime=MarketRegime.RANGING,
        atr_current=0.001,
        atr_average=0.001,
        volatility_ratio=volatility_ratio,
        directional_strength=0.2,
        confidence=0.5,
        tradeable=True,
    )


# ─── Density: cadence-independence ──────────────────────────────────────────


def test_density_cadence_independence_same_tier():
    """
    Feeding the SAME set of 5 distinct symbols at 10s cadence (many scans)
    vs 60s cadence (few scans) must yield the SAME tier and multiplier.
    """
    symbols = ["EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "NZDUSD"]
    base = datetime(2025, 6, 1, 12, 0, 0, tzinfo=timezone.utc)

    # 10s cadence → 360 scans/hr
    fast = OpportunityDensityTracker(window_minutes=60)
    for i in range(360):
        fast.record_scan(symbols, utc_now=base + timedelta(seconds=10 * i))

    # 60s cadence → 60 scans/hr
    slow = OpportunityDensityTracker(window_minutes=60)
    for i in range(60):
        slow.record_scan(symbols, utc_now=base + timedelta(seconds=60 * i))

    fast_snap = fast.get_snapshot()
    slow_snap = slow.get_snapshot()

    assert fast_snap is not None and slow_snap is not None
    assert fast_snap.ready_count_1h == slow_snap.ready_count_1h == 5
    assert fast_snap.tier == slow_snap.tier == "NORMAL"
    assert fast_snap.size_multiplier == slow_snap.size_multiplier == 1.0


# ─── Density: persistence dedup ────────────────────────────────────────────


def test_density_persistent_symbol_counted_once():
    """One symbol reported READY across 30 scans → distinct count 1 → LOW."""
    tracker = OpportunityDensityTracker(window_minutes=60)
    base = datetime(2025, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
    for i in range(30):
        tracker.record_scan(["EURUSD"], utc_now=base + timedelta(seconds=10 * i))

    snap = tracker.get_snapshot()
    assert snap is not None
    assert snap.ready_count_1h == 1
    assert snap.tier == "LOW"


# ─── Density: HIGH threshold ───────────────────────────────────────────────


def test_density_high_tier_above_threshold():
    """>8 distinct symbols within window → HIGH tier → 0.80."""
    tracker = OpportunityDensityTracker(window_minutes=60)
    now = datetime(2025, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
    symbols = [f"PAIR{i}" for i in range(9)]
    tracker.record_scan(symbols, utc_now=now)

    snap = tracker.get_snapshot()
    assert snap is not None
    assert snap.ready_count_1h == 9
    assert snap.tier == "HIGH"
    assert snap.size_multiplier == 0.80


# ─── Density: LOW multiplier is exactly 1.0 ────────────────────────────────


def test_density_low_tier_multiplier_is_one():
    """LOW tier must NOT boost — multiplier is exactly 1.0."""
    tracker = OpportunityDensityTracker(window_minutes=60)
    now = datetime(2025, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
    tracker.record_scan(["EURUSD", "GBPUSD"], utc_now=now)

    snap = tracker.get_snapshot()
    assert snap is not None
    assert snap.tier == "LOW"
    assert snap.size_multiplier == 1.0


# ─── Density: eviction ─────────────────────────────────────────────────────


def test_density_eviction_drops_stale_symbols():
    """Symbols older than the window drop out of the distinct count."""
    tracker = OpportunityDensityTracker(window_minutes=60)
    t0 = datetime(2025, 6, 1, 10, 0, 0, tzinfo=timezone.utc)
    t1 = t0 + timedelta(minutes=61)

    tracker.record_scan(["EURUSD", "GBPUSD", "USDJPY"], utc_now=t0)
    snap_before = tracker.get_snapshot()
    assert snap_before is not None
    assert snap_before.ready_count_1h == 3

    tracker.record_scan(["AUDUSD"], utc_now=t1)
    snap_after = tracker.get_snapshot()
    assert snap_after is not None
    assert snap_after.ready_count_1h == 1
    assert snap_after.tier == "LOW"


# ─── Density: cold start ───────────────────────────────────────────────────


def test_density_cold_start_returns_one():
    tracker = OpportunityDensityTracker()
    assert tracker.get_size_multiplier() == 1.0


# ─── Density: legacy int fallback ──────────────────────────────────────────


def test_density_legacy_int_input():
    """Passing an int still works (backward compatibility)."""
    tracker = OpportunityDensityTracker(window_minutes=60)
    now = datetime(2025, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
    tracker.record_scan(5, utc_now=now)
    snap = tracker.get_snapshot()
    assert snap is not None
    assert snap.ready_count_1h == 5
    assert snap.tier == "NORMAL"


# ─── Vol monitor: dilution fix ──────────────────────────────────────────────


def test_vol_monitor_unclassifiable_pairs_excluded_from_denominator():
    """
    3 pairs with vol_ratio > 1.8 plus 7 pairs with vol_ratio == 0.0
    → SPIKE (3/3 = 100% of classifiable), NOT diluted to 30%.
    """
    monitor = SystemVolatilityMonitor()
    spike_analyses = [_make_analysis(2.0) for _ in range(3)]
    junk_analyses = [_make_analysis(0.0) for _ in range(7)]

    state = monitor.update(spike_analyses + junk_analyses)
    assert state.state == "SPIKE"
    assert state.size_multiplier == SystemVolatilityMonitor.SPIKE_MULTIPLIER
    assert state.total_pairs_checked == 3


# ─── Vol monitor: all unclassifiable → NORMAL ──────────────────────────────


def test_vol_monitor_all_unclassifiable_defaults_normal():
    """Every pair has volatility_ratio 0.0 → all filtered out → NORMAL."""
    monitor = SystemVolatilityMonitor()
    analyses = [_make_analysis(0.0) for _ in range(5)]

    state = monitor.update(analyses)
    assert state.state == "NORMAL"
    assert state.size_multiplier == 1.0
    assert state.total_pairs_checked == 0


# ─── Vol monitor: normal mixed case (regression guard) ─────────────────────


def test_vol_monitor_normal_mixed_case():
    """
    10 classifiable pairs, 1 with vol_ratio > 1.8 (10% < 30% threshold)
    → NORMAL.
    """
    monitor = SystemVolatilityMonitor()
    analyses = [_make_analysis(1.0) for _ in range(9)]
    analyses.append(_make_analysis(2.0))

    state = monitor.update(analyses)
    assert state.state == "NORMAL"
    assert state.size_multiplier == 1.0
    assert state.total_pairs_checked == 10


# ─── Vol monitor: elevated detection still works ───────────────────────────


def test_vol_monitor_elevated_detection():
    """
    10 classifiable pairs, 2 with vol_ratio > 1.4 (20% >= 15% threshold)
    but only 0 with > 1.8 → ELEVATED.
    """
    monitor = SystemVolatilityMonitor()
    analyses = [_make_analysis(1.0) for _ in range(8)]
    analyses.extend([_make_analysis(1.5), _make_analysis(1.6)])

    state = monitor.update(analyses)
    assert state.state == "ELEVATED"
    assert state.size_multiplier == SystemVolatilityMonitor.ELEVATED_MULTIPLIER


# ─── Vol monitor: cold start ───────────────────────────────────────────────


def test_vol_monitor_cold_start():
    monitor = SystemVolatilityMonitor()
    assert monitor.get_size_multiplier() == 1.0


# ─── Vol monitor: empty list → NORMAL ──────────────────────────────────────


def test_vol_monitor_empty_list():
    monitor = SystemVolatilityMonitor()
    state = monitor.update([])
    assert state.state == "NORMAL"
    assert state.size_multiplier == 1.0
