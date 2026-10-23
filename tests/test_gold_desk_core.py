from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from gold_desk.coordinator import CoordinatorConfig, GoldOnlyGuard, StrategyCoordinator
from gold_desk.indicators import atr
from gold_desk.models import Candle, Direction, MarketSnapshot, TradeIdea, Venue
from gold_desk.risk import GoldExecutionGate, RiskState
from gold_desk.strategies import CandleRangeTheory, Strategy, default_strategies


def _snapshot(symbol: str = "XAUUSD") -> MarketSnapshot:
    now = datetime.now(timezone.utc)
    bars = []
    for i in range(24):
        close_time = now - timedelta(minutes=5 * (24 - i))
        if i == 23:
            bars.append(Candle(close_time, open=2088.0, high=2105.0, low=2075.0, close=2080.0, volume=100))
        elif i == 22:
            bars.append(Candle(close_time, open=2090.0, high=2092.0, low=2082.0, close=2088.0, volume=80))
        else:
            bars.append(Candle(close_time, open=2084.0, high=2092.0, low=2060.0, close=2085.0, volume=50))
    anchor = Candle(now - timedelta(minutes=40), open=2020.0, high=2100.0, low=1900.0, close=2050.0, volume=500)
    return MarketSnapshot(
        symbol=symbol,
        venue=Venue.MT5,
        observed_at=now,
        bid=2079.9,
        ask=2080.1,
        candles={"M5": bars, "H1": [anchor]},
    )


def test_all_eight_strategy_families_are_registered():
    ids = {strategy.strategy_id for strategy in default_strategies()}
    assert ids == {
        "trend_continuation",
        "liquidity_sweep_reversal",
        "volatility_expansion_breakout",
        "failed_breakout_trap",
        "statistical_mean_reversion",
        "trend_exhaustion_reversal",
        "event_driven_gold_breakout",
        "crt",
    }


def test_crt_is_a_distinct_sweep_reclaim_and_structure_shift_strategy():
    snapshot = _snapshot()
    ideas = CandleRangeTheory(anchor_timeframe="H1").evaluate(snapshot)
    assert len(ideas) == 1
    idea = ideas[0]
    assert idea.strategy_id == "crt"
    assert idea.direction is Direction.SHORT
    assert idea.target < idea.entry < idea.stop
    assert idea.reward_to_risk >= 1.2
    assert any("M5 close broke prior low" in item for item in idea.evidence)


def test_coordinator_coalesces_overlapping_ideas_without_score_inflation():
    snapshot = _snapshot()
    result = StrategyCoordinator().evaluate(snapshot)
    assert len(result.ideas) == 1
    idea = result.ideas[0]
    assert idea.direction is Direction.SHORT
    assert "crt" in idea.supporting_strategies
    assert "liquidity_sweep_reversal" in idea.supporting_strategies
    assert "failed_breakout_trap" in idea.supporting_strategies
    assert idea.quality_score == 70  # the winner's score is retained, not summed


def test_non_gold_symbol_is_rejected_before_strategy_execution():
    result = StrategyCoordinator().evaluate(_snapshot("EURUSD"))
    assert not result.ideas
    assert "symbol rejected: EURUSD" in result.diagnostics[0]


def test_gold_symbol_guard_uses_exact_allowlist_not_prefix_matching():
    guard = GoldOnlyGuard()
    assert guard.canonical_symbol("XAUUSD") == "XAUUSD"
    assert guard.canonical_symbol("xauusdm") == "XAUUSD"
    assert guard.canonical_symbol("GOLD") == "XAUUSD"
    assert guard.canonical_symbol("XAUUSD_FAKE") is None
    assert guard.canonical_symbol("EURUSD") is None


def test_opposing_similar_setups_cause_abstention():
    snapshot = _snapshot()
    current = snapshot.mid
    source_time = snapshot.latest_bar("M5").timestamp

    def make_idea(direction, score, family, stop, target):
        return TradeIdea(
            symbol="XAUUSD", venue="MT5", direction=direction, strategy_id=family,
            entry=current, stop=stop, target=target,
            created_at=snapshot.observed_at,
            expires_at=snapshot.observed_at + timedelta(minutes=5),
            source_bar_time=source_time, atr=10.0, quality_score=score,
            rationale="test fixture", evidence=("synthetic test evidence",),
        )

    class StaticStrategy:
        def __init__(self, strategy_id, idea):
            self.strategy_id = strategy_id
            self.idea = idea

        def evaluate(self, _snapshot):
            return [self.idea]

    long_idea = make_idea(Direction.LONG, 70, "test_long", current - 10, current + 20)
    short_idea = make_idea(Direction.SHORT, 69, "test_short", current + 10, current - 20)
    result = StrategyCoordinator(strategies=[
        StaticStrategy("test_long", long_idea), StaticStrategy("test_short", short_idea),
    ]).evaluate(snapshot)
    assert not result.ideas
    assert any("abstaining" in diagnostic for diagnostic in result.diagnostics)


def test_gate_defaults_to_shadow_only_even_for_valid_crt_candidate():
    snapshot = _snapshot()
    idea = CandleRangeTheory().evaluate(snapshot)[0]
    state = RiskState(equity=10_000, peak_equity=10_000, day_start_equity=10_000)
    gate = GoldExecutionGate()
    blocked = gate.authorize(idea, snapshot, state, now=snapshot.observed_at)
    assert not blocked.allowed
    assert "shadow-only" in blocked.reason

    allowed = gate.authorize(
        idea, snapshot, state, live_armed=True, now=snapshot.observed_at,
    )
    assert allowed.allowed, allowed.reason


def test_gate_rejects_when_daily_drawdown_cap_is_reached():
    snapshot = _snapshot()
    idea = CandleRangeTheory().evaluate(snapshot)[0]
    state = RiskState(equity=9_700, peak_equity=10_000, day_start_equity=10_000)
    result = GoldExecutionGate().authorize(
        idea, snapshot, state, live_armed=True, now=snapshot.observed_at,
    )
    assert not result.allowed
    assert "daily drawdown" in result.reason


def test_snapshot_rejects_future_candles_and_naive_timestamps():
    now = datetime.now(timezone.utc)
    with pytest.raises(ValueError):
        Candle(datetime.now(), 10, 11, 9, 10)
    future = Candle(now + timedelta(minutes=1), 10, 11, 9, 10)
    with pytest.raises(ValueError, match="after the snapshot time"):
        MarketSnapshot("XAUUSD", "MT5", now, 10, 10.1, {"M5": [future]})


def test_atr_is_deterministic_and_positive_for_valid_bars():
    snapshot = _snapshot()
    value = atr(snapshot.bars("M5"), 14)
    assert value is not None and value > 0


def test_coordinator_rejects_ambiguous_venue():
    now = datetime.now(timezone.utc)
    bar = Candle(now - timedelta(minutes=5), 2000, 2001, 1999, 2000)
    snapshot = MarketSnapshot("XAUUSD", "BOTH", now, 2000, 2000.2, {"M5": [bar]})
    result = StrategyCoordinator().evaluate(snapshot)
    assert not result.ideas
    assert "venue must be explicitly" in result.diagnostics[0]
