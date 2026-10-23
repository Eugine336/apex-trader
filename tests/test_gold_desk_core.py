from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from cognition.evidence_adapters import evidence_from_gold_market
from cognition.loop import CognitionLoop, EvidenceConsolidator
from gold_desk.apex_observer import ApexGoldDeskShadowObserver, _frame_to_closed_candles
from gold_desk.indicators import atr
from gold_desk.models import Candle, EventWindow, MarketSnapshot, PatternObservation, Venue
from gold_desk.news_context import build_verified_event_window
from gold_desk.strategies import CandleRangeTheoryDetector, EventWindowBreakoutDetector, default_pattern_detectors
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


def _payload(symbol: str = "XAUUSD", observed_at: datetime | None = None) -> dict:
    now = observed_at or datetime.now(timezone.utc)
    return {
        "observed_at": now.isoformat(),
        "symbol": symbol,
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
        "event_context": {
            "name": "US CPI",
            "impact": "HIGH",
            "starts_at": (now - timedelta(minutes=15)).isoformat(),
            "ends_at": (now + timedelta(minutes=5)).isoformat(),
            "pre_event_high": 2010.0,
            "pre_event_low": 1990.0,
        },
        "pattern_observations": [{
            "family": "crt_range_observation",
            "facts": ["anchor boundary was swept", "completed candle reclaimed the range"],
            "measurements": {
                "anchor_high": 2001.0, "anchor_low": 1990.0, "sweep_buffer": 0.2,
                "sweep": True, "absorption": True, "sweep_side": "buy", "bos": True,
                "displacement": -2.0,
            },
            "timeframe": "M5",
            "source_bar_time": now.isoformat(),
        }],
        "decision_authority": "CognitiveBrain",
        "overlap_policy": "evidence_only_no_vote_summing",
        "orders_sent": False,
    }


def test_eight_detectors_are_evidence_sources_not_strategies():
    detectors = default_pattern_detectors()
    assert {detector.pattern_id for detector in detectors} == {
        "trend_continuation",
        "liquidity_sweep",
        "volatility_expansion",
        "failed_breakout",
        "statistical_displacement",
        "trend_exhaustion",
        "event_window_breakout",
        "crt_range_observation",
    }
    assert len(detectors) == 8
    for detector in detectors:
        assert callable(detector.observe)
        assert not hasattr(detector, "decide")
        assert not hasattr(detector, "execute")


def test_crt_emits_facts_and_measurements_without_trade_geometry():
    snapshot = _snapshot()
    findings = CandleRangeTheoryDetector(anchor_timeframe="H1").observe(snapshot)
    assert len(findings) == 1
    finding = findings[0]
    assert isinstance(finding, PatternObservation)
    assert finding.pattern_id == "crt_range_observation"
    assert finding.measurements["swept_boundary"] == "anchor_high"
    assert finding.measurements["structure_boundary_broken"] == "previous_m5_low"
    assert any("anchor high was swept" in fact for fact in finding.facts)
    for forbidden in ("direction", "entry", "stop", "target", "quality_score", "reward_to_risk"):
        assert not hasattr(finding, forbidden)


def test_gold_symbol_guard_uses_exact_allowlist_not_prefix_matching():
    guard = GoldOnlyGuard()
    assert guard.canonical_symbol("XAUUSD") == "XAUUSD"
    assert guard.canonical_symbol("xauusdm") == "XAUUSD"
    assert guard.canonical_symbol("GOLD") == "XAUUSD"
    assert guard.canonical_symbol("XAUUSD_FAKE") is None
    assert guard.canonical_symbol("EURUSD") is None


def test_snapshot_rejects_future_candles_and_naive_timestamps():
    now = datetime.now(timezone.utc)
    with pytest.raises(ValueError):
        Candle(datetime.now(), 10, 11, 9, 10)
    future = Candle(now + timedelta(minutes=1), 10, 11, 9, 10)
    with pytest.raises(ValueError, match="after the snapshot time"):
        MarketSnapshot("XAUUSD", "MT5", now, 10, 10.1, {"M5": [future]})


def test_atr_is_deterministic_and_positive_for_valid_bars():
    value = atr(_snapshot().bars("M5"), 14)
    assert value is not None and value > 0


def test_gold_market_adapter_emits_one_neutral_record_with_detector_measurements():
    evidence = evidence_from_gold_market("XAUUSD", _payload())
    assert len(evidence) == 3
    root = next(item for item in evidence if item.source_module == "gold_market_desk")
    assert all(item.polarity == 0.0 for item in evidence)
    assert root.measurements["pattern_families"] == ["crt_range_observation"]
    assert root.measurements["overlap_policy"] == "single_brain_synthesis_no_vote_summing"
    assert root.measurements["orders_sent"] is False
    observation = root.measurements["pattern_observations"][0]
    assert observation["measurements"]["anchor_high"] == 2001.0
    assert root.measurements["event_context"]["pre_event_high"] == 2010.0
    assert "Verified news context: US CPI" in root.observation
    assert {item.domain.value for item in evidence} == {"multi_timeframe", "liquidity", "structure"}
    liquidity = next(item for item in evidence if item.domain.value == "liquidity")
    structure = next(item for item in evidence if item.domain.value == "structure")
    assert liquidity.measurements["sweep"] is True
    assert liquidity.measurements["sweep_side"] == "buy"
    assert structure.measurements["bos"] is True
    assert structure.measurements["displacement"] == -2.0
    forbidden = {"direction", "entry", "stop", "target", "quality_score", "reward_to_risk"}
    assert forbidden.isdisjoint(root.measurements)
    assert "NOT independent votes" in root.observation


def test_gold_market_adapter_rejects_stale_and_non_gold_payloads():
    old = datetime.now(timezone.utc) - timedelta(seconds=30)
    assert evidence_from_gold_market("XAUUSD", _payload(observed_at=old)) == []
    assert evidence_from_gold_market("EURUSD", _payload(symbol="EURUSD")) == []


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


def test_observer_passes_only_pattern_facts_and_measurements_to_brain():
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

    class OneObservationDetector:
        pattern_id = "test_pattern"

        def observe(self, market):
            return [PatternObservation(
                symbol="XAUUSD",
                venue="MT5",
                pattern_id="test_pattern",
                timeframe="M5",
                observed_at=market.observed_at,
                source_bar_time=market.latest_bar("M5").timestamp,
                facts=("completed bar closed above the prior high",),
                measurements={"prior_high": 2079.0, "close": 2080.0},
            )]

    bus = FakeBus()
    wake_requests = []
    observer = ApexGoldDeskShadowObserver(
        symbols=["XAUUSD"],
        venue="MT5",
        tick_router=FakeRouter(),
        event_bus=bus,
        candle_fetcher=lambda _symbol, _timeframe, _count: None,
        venue_resolver=lambda _symbol: "MT5Connector",
        market_open=lambda _symbol: True,
        pattern_detectors=[OneObservationDetector()],
    )
    observer.set_evidence_change_callback(wake_requests.append)
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
    observation = payload["pattern_observations"][0]
    assert observation["family"] == "test_pattern"
    assert observation["measurements"]["prior_high"] == 2079.0
    for forbidden in ("direction", "entry", "stop", "target", "quality_score"):
        assert forbidden not in payload
        assert forbidden not in observation
    assert wake_requests == ["XAUUSD"]




def test_observer_feed_health_requires_fresh_open_quote_and_completed_m5_data():
    now = datetime.now(timezone.utc)
    observer = ApexGoldDeskShadowObserver(
        symbols=["XAUUSD"],
        venue="MT5",
        tick_router=object(),
        event_bus=object(),
        candle_fetcher=lambda _symbol, _timeframe, _count: None,
        venue_resolver=lambda _symbol: "MT5Connector",
        market_open=lambda _symbol: True,
    )
    assert observer.is_feed_healthy("XAUUSD", now_epoch=now.timestamp()) is False

    payload = _payload(observed_at=now)
    observer._latest_market_evidence["XAUUSD"] = payload
    assert observer.is_feed_healthy("XAUUSD", now_epoch=now.timestamp()) is True

    stale = _payload(observed_at=now - timedelta(seconds=20))
    observer._latest_market_evidence["XAUUSD"] = stale
    assert observer.is_feed_healthy("XAUUSD", now_epoch=now.timestamp()) is False

    closed = _payload(observed_at=now)
    closed["market_open"] = False
    observer._latest_market_evidence["XAUUSD"] = closed
    assert observer.is_feed_healthy("XAUUSD", now_epoch=now.timestamp()) is False

    bad_quote = _payload(observed_at=now)
    bad_quote["quote"] = {"bid": 2001.0, "ask": 2000.0, "spread": -1.0}
    observer._latest_market_evidence["XAUUSD"] = bad_quote
    assert observer.is_feed_healthy("XAUUSD", now_epoch=now.timestamp()) is False



def test_cognition_loop_can_wake_on_material_market_change_even_without_global_event_mode():
    clock_now = [100.0]
    loop = CognitionLoop(
        brain=object(),
        consolidator=EvidenceConsolidator(),
        symbols_provider=lambda: ["XAUUSD"],
        event_driven=False,
        event_min_interval_seconds=8.0,
        clock=lambda: clock_now[0],
    )
    assert loop.event_driven is False
    assert loop.request_reason("XAUUSD", reason="gold_market_evidence_changed", now=100.0) is True
    assert "XAUUSD" in loop._pending
    assert loop._wake.is_set()

    # The per-symbol debounce prevents a fast quote feed from forcing a reasoner
    # call on every small market update.
    assert loop.request_reason("XAUUSD", reason="small_repeat", now=101.0) is False
    assert loop.request_reason("XAUUSD", reason="new_material_change", now=109.0) is True




def test_event_window_uses_newsguard_time_and_pre_event_completed_candles():
    snapshot = _snapshot()
    event_time = snapshot.observed_at + timedelta(minutes=2)

    class FakeNewsGuard:
        pause_before = 15
        pause_after = 5

        def __init__(self):
            self.calls = []

        def check(self, symbols, now):
            self.calls.append((symbols, now))
            event = SimpleNamespace(
                title="US CPI",
                impact="HIGH",
                time_utc=event_time,
            )
            return SimpleNamespace(events_nearby=[event])

    guard = FakeNewsGuard()
    bars = list(snapshot.bars("M5"))
    # This bar is after the current observation (and still before the event),
    # so it must not leak into a pre-event range.
    future_bar = Candle(
        snapshot.observed_at + timedelta(minutes=1),
        open=2100.0, high=2300.0, low=2000.0, close=2200.0, volume=10,
    )
    candles = {"M5": [*bars, future_bar]}
    window = build_verified_event_window("XAUUSD", snapshot.observed_at, candles, guard)

    assert window is not None
    assert window.name == "US CPI"
    assert window.impact == "HIGH"
    assert window.is_active(snapshot.observed_at)
    assert window.pre_event_high == max(bar.high for bar in bars[-12:])
    assert window.pre_event_low == min(bar.low for bar in bars[-12:])
    assert guard.calls == [(["XAUUSD"], snapshot.observed_at)]
    assert window.pre_event_high < future_bar.high


def test_event_window_breakout_is_a_factual_observation_not_a_trade_signal():
    snapshot = _snapshot()
    event_window = EventWindow(
        name="US CPI",
        starts_at=snapshot.observed_at - timedelta(minutes=1),
        ends_at=snapshot.observed_at + timedelta(minutes=5),
        pre_event_high=2050.0,
        pre_event_low=2000.0,
        impact="HIGH",
    )
    with_window = MarketSnapshot(
        symbol="XAUUSD",
        venue="MT5",
        observed_at=snapshot.observed_at,
        bid=snapshot.bid,
        ask=snapshot.ask,
        candles=snapshot.candles,
        event_window=event_window,
        market_open=True,
        data_quality_ok=True,
    )
    findings = EventWindowBreakoutDetector().observe(with_window)
    assert len(findings) == 1
    finding = findings[0]
    assert finding.pattern_id == "event_window_breakout"
    assert finding.measurements["escaped_boundary"] == "pre_event_high"
    assert any("verified event window is active" in fact for fact in finding.facts)
    for forbidden in ("direction", "entry", "stop", "target", "quality_score"):
        assert not hasattr(finding, forbidden)

    inactive = EventWindow(
        name="US CPI",
        starts_at=snapshot.observed_at + timedelta(minutes=1),
        ends_at=snapshot.observed_at + timedelta(minutes=5),
        pre_event_high=2050.0,
        pre_event_low=2000.0,
        impact="HIGH",
    )
    inactive_snapshot = MarketSnapshot(
        symbol="XAUUSD", venue="MT5", observed_at=snapshot.observed_at,
        bid=snapshot.bid, ask=snapshot.ask, candles=snapshot.candles,
        event_window=inactive, market_open=True, data_quality_ok=True,
    )
    assert EventWindowBreakoutDetector().observe(inactive_snapshot) == []



def test_gold_observations_enter_existing_market_state_not_a_parallel_decider():
    payload = _payload()
    calls = []

    def source(symbol):
        calls.append(symbol)
        return payload

    consolidator = EvidenceConsolidator()
    consolidator.set_gold_market_source(source)
    state = consolidator.build("XAUUSD")
    gold_records = [item for item in state.evidence if item.source_module.startswith("gold_market_desk")]
    assert calls == ["XAUUSD"]
    assert len(gold_records) == 3
    assert all(item.polarity == 0.0 for item in gold_records)
    root = next(item for item in gold_records if item.source_module == "gold_market_desk")
    assert root.measurements["orders_sent"] is False
    assert any(item.domain.value == "liquidity" for item in gold_records)
    assert any(item.domain.value == "structure" for item in gold_records)

    consolidator.build("EURUSD")
    assert calls == ["XAUUSD"]
