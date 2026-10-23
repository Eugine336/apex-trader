from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from cognition.evidence_adapters import evidence_from_gold_market
from gold_desk.apex_observer import (
    ApexGoldDeskShadowObserver,
    _frame_to_closed_candles,
)
from gold_desk.indicators import atr
from gold_desk.models import Candle, Direction, MarketSnapshot, TradeIdea, Venue
from gold_desk.risk import GoldExecutionGate, RiskState
from gold_desk.strategies import CandleRangeTheory, default_pattern_detectors
from gold_desk.symbols import GoldOnlyGuard


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


def test_all_eight_pattern_detectors_are_registered():
    ids = {detector.pattern_id for detector in default_pattern_detectors()}
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


def test_crt_detector_requires_completed_anchor_sweep_reclaim_and_structure_shift():
    snapshot = _snapshot()
    findings = CandleRangeTheory(anchor_timeframe="H1").evaluate(snapshot)
    assert len(findings) == 1
    finding = findings[0]
    assert finding.strategy_id == "crt"  # internal detector result; not exposed as a trade
    assert finding.direction is Direction.SHORT
    assert finding.target < finding.entry < finding.stop
    assert finding.reward_to_risk >= 1.2
    assert any("M5 close broke prior low" in item for item in finding.evidence)


def test_gold_symbol_guard_uses_exact_allowlist_not_prefix_matching():
    guard = GoldOnlyGuard()
    assert guard.canonical_symbol("XAUUSD") == "XAUUSD"
    assert guard.canonical_symbol("xauusdm") == "XAUUSD"
    assert guard.canonical_symbol("GOLD") == "XAUUSD"
    assert guard.canonical_symbol("XAUUSD_FAKE") is None
    assert guard.canonical_symbol("EURUSD") is None


def test_gate_defaults_to_shadow_only_even_for_valid_crt_candidate():
    snapshot = _snapshot()
    finding = CandleRangeTheory().evaluate(snapshot)[0]
    state = RiskState(equity=10_000, peak_equity=10_000, day_start_equity=10_000)
    gate = GoldExecutionGate()
    blocked = gate.authorize(finding, snapshot, state, now=snapshot.observed_at)
    assert not blocked.allowed
    assert "shadow-only" in blocked.reason

    allowed = gate.authorize(
        finding, snapshot, state, live_armed=True, now=snapshot.observed_at,
    )
    assert allowed.allowed, allowed.reason


def test_gate_rejects_when_daily_drawdown_cap_is_reached():
    snapshot = _snapshot()
    finding = CandleRangeTheory().evaluate(snapshot)[0]
    state = RiskState(equity=9_700, peak_equity=10_000, day_start_equity=10_000)
    result = GoldExecutionGate().authorize(
        finding, snapshot, state, live_armed=True, now=snapshot.observed_at,
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


def test_gold_market_adapter_emits_one_neutral_evidence_record():
    now = datetime.now(timezone.utc)
    payload = {
        "observed_at": now.isoformat(),
        "symbol": "XAUUSD",
        "venue": "MT5",
        "market_open": True,
        "data_quality_ok": True,
        "quote": {"bid": 2000.0, "ask": 2000.2, "spread": 0.2},
        "completed_bars": {
            "M5": [{
                "close_time": now.isoformat(),
                "open": 1999.0,
                "high": 2001.0,
                "low": 1998.0,
                "close": 2000.0,
                "volume": 100.0,
            }],
        },
        "pattern_observations": [{
            "family": "crt",
            "facts": ["anchor high swept", "close reclaimed inside the range"],
            "source_bar_time": now.isoformat(),
        }],
        "orders_sent": False,
    }
    evidence = evidence_from_gold_market("XAUUSD", payload)
    assert len(evidence) == 1
    item = evidence[0]
    assert item.source_module == "gold_market_desk"
    assert item.polarity == 0.0
    assert item.measurements["pattern_families"] == ["crt"]
    assert item.measurements["overlap_policy"] == "single_brain_synthesis_no_vote_summing"
    assert item.measurements["orders_sent"] is False
    forbidden = {"direction", "entry", "stop", "target", "quality_score", "reward_to_risk"}
    assert forbidden.isdisjoint(item.measurements)
    assert "NOT independent votes" in item.observation


def test_gold_market_adapter_rejects_stale_payload_and_non_gold_symbol():
    old = datetime.now(timezone.utc) - timedelta(seconds=30)
    payload = {
        "observed_at": old.isoformat(),
        "symbol": "XAUUSD",
        "venue": "MT5",
        "market_open": True,
        "data_quality_ok": True,
        "quote": {"bid": 2000.0, "ask": 2000.2, "spread": 0.2},
        "completed_bars": {},
        "pattern_observations": [],
    }
    assert evidence_from_gold_market("XAUUSD", payload) == []
    payload["observed_at"] = datetime.now(timezone.utc).isoformat()
    payload["symbol"] = "EURUSD"
    assert evidence_from_gold_market("EURUSD", payload) == []


class _FakeFrame:
    columns = ["time", "open", "high", "low", "close", "volume"]

    def __init__(self, rows):
        self.rows = rows

    def __len__(self):
        return len(self.rows)

    def iterrows(self):
        for index, row in enumerate(self.rows):
            yield index, row


def test_observer_excludes_forming_candles():
    observed_at = datetime(2026, 10, 23, 12, 10, tzinfo=timezone.utc)
    frame = _FakeFrame([
        {"time": observed_at - timedelta(minutes=15), "open": 2000.0, "high": 2004.0, "low": 1998.0, "close": 2002.0, "volume": 100},
        {"time": observed_at - timedelta(minutes=10), "open": 2002.0, "high": 2006.0, "low": 2000.0, "close": 2004.0, "volume": 100},
        {"time": observed_at - timedelta(minutes=5), "open": 2004.0, "high": 2008.0, "low": 2002.0, "close": 2006.0, "volume": 100},
        {"time": observed_at, "open": 2006.0, "high": 2010.0, "low": 2004.0, "close": 2008.0, "volume": 100},
    ])
    candles = _frame_to_closed_candles(frame, "M5", observed_at)
    assert [candle.timestamp for candle in candles] == [
        observed_at - timedelta(minutes=10),
        observed_at - timedelta(minutes=5),
        observed_at,
    ]


def test_observer_wont_start_if_platform_route_disagrees_with_selected_venue():
    class FakeRouter:
        def __init__(self):
            self.callbacks = []

        def register_callback(self, callback):
            self.callbacks.append(callback)

        def unregister_callback(self, callback):
            self.callbacks.remove(callback)

    class FakeBus:
        def publish(self, _event_type, _event):
            raise AssertionError("No event should be published for a mismatched venue")

    router = FakeRouter()
    observer = ApexGoldDeskShadowObserver(
        symbols=["XAUUSD"],
        venue="DERIV",
        tick_router=router,
        event_bus=FakeBus(),
        candle_fetcher=lambda _symbol, _timeframe, _count: None,
        venue_resolver=lambda _symbol: "MT5Connector",
    )
    assert observer.start() is False
    assert router.callbacks == []


def test_observer_sends_only_facts_to_the_single_brain():
    snapshot = _snapshot()

    class FakeRouter:
        def register_callback(self, _callback):
            return None

        def unregister_callback(self, _callback):
            return None

    class FakeBus:
        def __init__(self):
            self.events = []

        def publish(self, event_type, payload):
            self.events.append((event_type, payload))

    class OneFindingDetector:
        pattern_id = "test_pattern"

        def evaluate(self, market):
            return [TradeIdea(
                symbol="XAUUSD",
                venue="MT5",
                direction=Direction.LONG,
                strategy_id="test_pattern",
                entry=market.mid,
                stop=market.mid - 10,
                target=market.mid + 20,
                created_at=market.observed_at,
                expires_at=market.observed_at + timedelta(minutes=5),
                source_bar_time=market.latest_bar("M5").timestamp,
                atr=10,
                quality_score=88,
                rationale="private detector-only thesis",
                evidence=("completed bar closed above the prior high",),
            )]

    bus = FakeBus()
    observer = ApexGoldDeskShadowObserver(
        symbols=["XAUUSD"],
        venue="MT5",
        tick_router=FakeRouter(),
        event_bus=bus,
        candle_fetcher=lambda _symbol, _timeframe, _count: None,
        venue_resolver=lambda _symbol: "MT5Connector",
        market_open=lambda _symbol: True,
        pattern_detectors=[OneFindingDetector()],
    )
    observer._selected_symbols = {"XAUUSD"}
    observer._bars = {
        ("XAUUSD", "M5"): snapshot.bars("M5"),
        ("XAUUSD", "H1"): snapshot.bars("H1"),
    }
    tick = SimpleNamespace(
        symbol="XAUUSD",
        source="mt5",
        timestamp=snapshot.observed_at,
        bid=snapshot.bid,
        ask=snapshot.ask,
    )
    observer._evaluate_tick(tick)

    assert len(bus.events) == 1
    event_type, payload = bus.events[0]
    assert event_type == "gold_market_evidence"
    assert payload["decision_authority"] == "CognitiveBrain"
    assert payload["orders_sent"] is False
    assert payload["pattern_observations"][0]["family"] == "test_pattern"
    assert "completed bar closed above the prior high" in payload["pattern_observations"][0]["facts"]
    assert "direction" not in payload
    assert "entry" not in payload
    assert "stop" not in payload
    assert "target" not in payload
    assert "quality_score" not in payload
