from cognition.concepts.swing_geometry import SwingGeometryConcepts


def _history():
    rows = []
    for i in range(30):
        opening = 102.0 + i * 0.01
        closing = opening + 0.02
        rows.append({
            "open": opening, "high": closing + 0.8, "low": opening - 0.8,
            "close": closing, "timestamp_epoch": 1_700_000_000 + i * 3600,
            "source_timestamp_verified": True,
        })
    # Confirmed swing low at 20 followed by confirmed swing high at 24.
    rows[19]["low"] = 100.0
    rows[20].update({"open": 96.0, "high": 97.0, "low": 95.0, "close": 96.5})
    rows[21]["low"] = 100.0
    rows[23]["high"] = 103.0
    rows[24].update({"open": 108.0, "high": 110.0, "low": 107.0, "close": 109.0})
    rows[25]["high"] = 103.0
    rows[-1].update({"open": 101.0, "high": 103.0, "low": 100.5, "close": 102.0})
    return rows


def test_fibonacci_ote_uses_confirmed_swing_anchors_and_requires_reaction():
    readings = SwingGeometryConcepts().read("TEST", {"H1": _history()})
    fib = next(item for item in readings if item.concept == "fibonacci")
    assert fib.facts["impulse_direction"] == "LONG"
    assert fib.facts["start_pivot_index"] == 20
    assert fib.facts["end_pivot_index"] == 24
    assert fib.facts["inside_ote"] is True
    assert fib.implications[0].activated is True
    assert "reaction and renewed directional structure" in fib.implications[0].requires


def test_regression_channel_is_descriptive_and_does_not_claim_calibrated_probability():
    readings = SwingGeometryConcepts().read("TEST", {"H1": _history()})
    channel = next(item for item in readings if item.concept == "trend_channels")
    assert channel.facts["lookback_bars"] == 30
    assert channel.implications[-1].direction == "FLAT"
    assert "not a calibrated confidence interval" in channel.unknowns[0]
