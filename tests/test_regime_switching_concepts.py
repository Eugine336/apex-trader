from cognition.concepts.regime_switching import RegimeSwitchingConcepts


def _bars(returns):
    close = 100.0
    bars = []
    for i, change in enumerate([0.0, *returns]):
        opening = close
        close = close * (1.0 + change) if i else close
        bars.append({
            "open": opening, "high": max(opening, close) + 0.01,
            "low": min(opening, close) - 0.01, "close": close,
            "timestamp_epoch": 1_700_000_000 + i * 3600,
            "source_timestamp_verified": True,
        })
    return bars


def test_regime_shift_is_reported_as_warning_not_directional_signal():
    returns = [1e-5 if i % 2 else -1e-5 for i in range(30)]
    returns += [0.002 + (1e-5 if i % 2 else -1e-5) for i in range(30)]
    reading = RegimeSwitchingConcepts().read("TEST", {"H1": _bars(returns)})[0]
    assert reading.concept == "regime_switching"
    assert reading.facts["mean_shift_detected"] is True
    assert reading.facts["possible_transition"] is True
    assert reading.facts["mean_shift_direction"] == "upward_mean_shift"
    assert reading.implications[0].direction == "FLAT"
    assert reading.implications[0].activated is True
    assert reading.implications[0].management["never_authorizes_entry"] is True


def test_regime_shift_stays_quiet_for_stable_return_distribution():
    returns = [1e-5 if i % 2 else -1e-5 for i in range(60)]
    reading = RegimeSwitchingConcepts().read("TEST", {"H1": _bars(returns)})[0]
    assert reading.facts["possible_transition"] is False
    assert reading.implications[0].activated is False


def test_regime_shift_requires_sixty_completed_returns():
    returns = [1e-5 if i % 2 else -1e-5 for i in range(59)]
    assert RegimeSwitchingConcepts().read("TEST", {"H1": _bars(returns)}) == []
