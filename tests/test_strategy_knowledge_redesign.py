"""Regression tests for the market-aware strategy-knowledge registry."""

from types import SimpleNamespace

from cognition.strategy_knowledge import STRATEGY_LENSES, assess_strategy_lenses


def _evidence(domain: str, source: str, observation: str, measurements=None):
    return SimpleNamespace(
        domain=SimpleNamespace(value=domain),
        source_module=source,
        observation=observation,
        measurements=measurements or {},
    )


def test_registry_covers_distinct_analytical_and_operational_families():
    keys = {lens.key for lens in STRATEGY_LENSES}
    assert len(keys) == len(STRATEGY_LENSES)
    assert len(keys) >= 35
    assert {
        "ict_smc", "trend_following", "mean_reversion", "order_flow_auction",
        "session_intraday", "event_macro", "cross_asset", "execution_costs",
        "risk_of_ruin", "learning_calibration", "pattern_discovery",
        "market_regime_transition",
    } <= keys


def test_lens_matching_uses_token_boundaries_not_arbitrary_substrings():
    # "female" contains the letters "ema", but is not an EMA observation.
    evidence = _evidence("other", "profile", "female trader profile")
    results = assess_strategy_lenses([evidence])
    ema = next(item for item in results if item["key"] == "moving_average")
    assert ema["status"] == "not_evaluable"


def test_module_names_are_not_treated_as_market_concept_evidence():
    evidence = _evidence(
        "structure",
        "market_lenses.m5.structure",
        "completed candle close remains within prior range",
    )
    result = next(
        item for item in assess_strategy_lenses([evidence])
        if item["key"] == "support_resistance"
    )
    assert result["status"] == "watch"


def test_relevance_is_not_a_direction_or_execution_instruction():
    evidence = _evidence(
        "structure",
        "market_lenses.m5.structure",
        "close broke above prior range high",
        {"breakout_direction": "up", "bos": True, "timeframe": "M5"},
    )
    results = assess_strategy_lenses([evidence], ["structural_break"], {})
    assert len(results) == len(STRATEGY_LENSES)
    for result in results:
        assert "direction" not in result
        assert "trade" not in result
        assert result["limitation"]
        assert 0.0 <= result["relevance"] <= 1.0


def test_missing_requirements_remain_explicit_even_when_a_lens_is_relevant():
    evidence = _evidence(
        "structure",
        "market_lenses.m5.structure",
        "support resistance swing high",
        {"pattern_tags": ["support", "resistance"]},
    )
    result = next(
        item for item in assess_strategy_lenses([evidence])
        if item["key"] == "support_resistance"
    )
    assert result["status"] == "relevant"
    assert result["missing_context"]


def test_repeated_observations_from_same_module_do_not_inflate_lens_relevance():
    one = _evidence(
        "structure", "market_lenses.m5.structure",
        "support resistance swing high",
    )
    duplicate = _evidence(
        "structure", "market_lenses.m5.structure",
        "support resistance swing high",
    )
    a = next(item for item in assess_strategy_lenses([one]) if item["key"] == "support_resistance")
    b = next(item for item in assess_strategy_lenses([one, duplicate]) if item["key"] == "support_resistance")
    assert a["relevance"] == b["relevance"]


def test_absent_evidence_marks_all_lenses_not_evaluable():
    results = assess_strategy_lenses([], [], {})
    assert len(results) == len(STRATEGY_LENSES)
    assert all(item["status"] == "not_evaluable" for item in results)
    assert all(item["limitation"] for item in results)
