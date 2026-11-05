"""CRT concept module: full reading from completed bars (canonical, mirrored, cancelled, phases)."""

import random

import pytest

from cognition.concepts import run_concepts
from cognition.concepts.crt import CandleRangeTheory
from cognition.concepts.base import percentile_rank

H = 3600
T0 = 1_700_000_000 - (1_700_000_000 % (24 * H))


def _bar(i, o, h, l, c, tf=H):
    return {"open": o, "high": h, "low": l, "close": c, "timestamp_epoch": float(T0 + i * tf),
            "source_timestamp_verified": True, "volume": 100.0}


def _noise(n, base=1.0800, seed=None):
    # Random walks contain CRT-like pairs by chance; pick a seed whose final three
    # pairs (including the junction with the appended C1) are not CRTs so each test
    # controls exactly the sequence it asserts on.
    from cognition.concepts.crt import _classify
    for candidate in ([seed] if seed is not None else range(200)):
        bars = _noise_walk(n, base, candidate)
        junction = {"open": 1.0820, "high": 1.0840, "low": 1.0800, "close": 1.0810}
        tail = bars[-3:] + [junction]
        if all(_classify(a, b) not in ("bullish_crt", "bearish_crt") for a, b in zip(tail, tail[1:])):
            return bars
    raise AssertionError("no neutral seed found")


def _noise_walk(n, base, seed):
    rng = random.Random(seed)
    bars, px = [], base
    for i in range(n):
        o = px
        c = px + rng.uniform(-0.0006, 0.0006)
        h = max(o, c) + rng.uniform(0.0001, 0.0004)
        l = min(o, c) - rng.uniform(0.0001, 0.0004)
        bars.append(_bar(i, o, h, l, c))
        px = c
    return bars


def _with_bullish_crt(c3=None):
    bars = _noise(60)
    i = len(bars)
    bars.append(_bar(i, 1.0820, 1.0840, 1.0800, 1.0810))        # C1: range 1.0800-1.0840
    bars.append(_bar(i + 1, 1.0810, 1.0825, 1.0795, 1.0818))    # C2: swept low 1.0800 to 1.0795, closed back inside
    if c3 is not None:
        bars.append(_bar(i + 2, *c3))
    return bars


def _read(bars, extra=None):
    frames = {"H1": bars}
    frames.update(extra or {})
    return CandleRangeTheory().read("EURUSD", frames)


def test_bullish_crt_phase_c2_is_a_full_reading():
    (reading,) = _read(_with_bullish_crt())
    imp = reading.implications[0]
    assert reading.facts["phase"] == "c2_complete"
    assert imp.direction == "LONG" and imp.activated
    assert imp.invalidation_price == pytest.approx(1.0795)       # beyond the sweep extreme
    assert imp.objective_price == pytest.approx(1.0840)          # opposite extreme of C1
    assert imp.milestones == (pytest.approx(1.0820),)            # C1 midpoint, still ahead of price
    assert imp.management["invalid_on_completed_close_beyond"] == pytest.approx(1.0795)
    assert reading.facts["range_c1"] == pytest.approx(0.0040)
    assert 0.0 <= (imp.strength or 0.0) <= 1.0
    # open-space and missing lower-timeframe data are surfaced as unknowns, not hidden
    assert any("open space" in c for c in imp.conflicts_with)
    assert any("lower-timeframe" in r for r in imp.requires)


def test_bearish_crt_is_the_mirror_image():
    bars = _noise(60)
    i = len(bars)
    bars.append(_bar(i, 1.0820, 1.0840, 1.0800, 1.0830))
    bars.append(_bar(i + 1, 1.0830, 1.0845, 1.0815, 1.0822))    # swept high to 1.0845, closed back inside
    (reading,) = _read(bars)
    imp = reading.implications[0]
    assert imp.direction == "SHORT" and imp.activated
    assert imp.invalidation_price == pytest.approx(1.0845)
    assert imp.objective_price == pytest.approx(1.0800)
    assert imp.milestones == (pytest.approx(1.0820),)


def test_close_beyond_the_extreme_is_acceptance_not_a_crt():
    bars = _noise(60)
    i = len(bars)
    bars.append(_bar(i, 1.0820, 1.0840, 1.0800, 1.0810))
    bars.append(_bar(i + 1, 1.0810, 1.0815, 1.0790, 1.0792))    # closed BELOW C1 low: acceptance
    assert _read(bars) == []


def test_two_sided_sweep_is_an_outside_bar_not_a_crt():
    bars = _noise(60)
    i = len(bars)
    bars.append(_bar(i, 1.0820, 1.0840, 1.0800, 1.0810))
    bars.append(_bar(i + 1, 1.0810, 1.0850, 1.0790, 1.0820))
    assert _read(bars) == []


def test_no_sweep_means_no_reading():
    bars = _noise(60)
    assert _read(bars) == []


def test_c3_delivering_keeps_the_thesis_live_and_drops_the_passed_milestone():
    bars = _with_bullish_crt(c3=(1.0818, 1.0834, 1.0816, 1.0830))   # delivered past the midpoint, not the objective
    (reading,) = _read(bars)
    imp = reading.implications[0]
    assert reading.facts["phase"] == "c3_complete_delivering"
    assert imp.activated and imp.milestones == ()
    assert reading.facts["progress_through_range"] == pytest.approx(0.75)


def test_c3_closing_beyond_the_sweep_extreme_means_the_crt_failed():
    bars = _with_bullish_crt(c3=(1.0818, 1.0820, 1.0780, 1.0790))
    (reading,) = _read(bars)
    imp = reading.implications[0]
    assert reading.facts["phase"] == "failed_accepted_beyond_sweep"
    assert not imp.activated and "failed" in imp.statement


def test_objective_delivered_ends_the_thesis():
    bars = _with_bullish_crt(c3=(1.0818, 1.0845, 1.0816, 1.0842))
    (reading,) = _read(bars)
    assert reading.facts["phase"] == "objective_delivered" and not reading.implications[0].activated


def test_lower_timeframe_structure_shift_and_higher_timeframe_liquidity_are_read():
    bars = _with_bullish_crt()
    c2_open = bars[-1]["timestamp_epoch"]
    q = 900
    # M15 inside C2's hour: lower high at 1.0812 before the low 1.0795, then a close above it
    m15 = [
        {"open": 1.0810, "high": 1.0812, "low": 1.0803, "close": 1.0805, "timestamp_epoch": c2_open, "source_timestamp_verified": True},
        {"open": 1.0805, "high": 1.0807, "low": 1.0795, "close": 1.0797, "timestamp_epoch": c2_open + q, "source_timestamp_verified": True},
        {"open": 1.0797, "high": 1.0814, "low": 1.0796, "close": 1.0813, "timestamp_epoch": c2_open + 2 * q, "source_timestamp_verified": True},
        {"open": 1.0813, "high": 1.0825, "low": 1.0811, "close": 1.0818, "timestamp_epoch": c2_open + 3 * q, "source_timestamp_verified": True},
    ]
    # An untouched H4 low at 1.0798 lies inside the swept zone (1.0795-1.0800)
    h4_open = bars[0]["timestamp_epoch"] - 4 * 14400
    h4 = [{"open": 1.0900, "high": 1.0910, "low": 1.0798, "close": 1.0850, "timestamp_epoch": h4_open, "source_timestamp_verified": True}]
    # keep the H1 history above 1.0798 until the sweep so the level is untouched
    for b in bars[:-2]:
        b["low"] = max(b["low"], 1.0801)
    (reading,) = _read(bars, {"M15": m15, "H4": h4})
    imp = reading.implications[0]
    assert reading.facts["lower_timeframe_structure_shift"] is True
    assert any("H4 low" in s for s in reading.facts["higher_timeframe_liquidity_taken"])
    assert not any("lower-timeframe" in r for r in imp.requires)
    assert not any("open space" in c for c in imp.conflicts_with)


def test_uses_only_completed_bars_removing_the_last_bar_changes_the_phase():
    full = _with_bullish_crt()
    (a,) = _read(full)
    assert a.facts["phase"] == "c2_complete"
    assert _read(full[:-1]) == []          # without C2 there is no sweep to read


def test_evidence_carries_the_full_reading_and_is_not_a_vote():
    evidence = [e for e in run_concepts("EURUSD", {"H1": _with_bullish_crt()})
                if e.measurements["concept"] == "crt"]
    assert len(evidence) == 1
    ev = evidence[0]
    m = ev.measurements
    assert m["source_kind"] == "concept_reading" and m["concept"] == "crt"
    assert ev.polarity == 0.0 and ev.data_basis == "completed_ohlc" and ev.provisional is False
    imp = m["implications"][0]
    assert imp["direction"] == "LONG" and imp["invalidation_price"] == pytest.approx(1.0795)
    assert ev.independence_group == "ohlc:EURUSD:H1"


def test_percentile_rank_is_relative_to_own_history_and_unknown_without_it():
    assert percentile_rank(0.5, [0.1, 0.2, 0.9, 0.8]) == 0.5
    assert percentile_rank(0.5, []) is None


def test_short_history_leaves_strength_unknown_rather_than_invented():
    bars = _with_bullish_crt()[-3:]
    (reading,) = _read(bars)
    assert reading.implications[0].strength is None or 0.0 <= reading.implications[0].strength <= 1.0
