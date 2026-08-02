"""Tests for Phase 2 Feature C — active compression / session filtering.

Covers the orchestrator's now-ACTIVE compression-state conviction boost, the
Asian-compression caution gate, and the session size multipliers that
event_driven_bootstrap applies to risk%. The conviction shaping is tested both
at the helper level (deterministic) and end-to-end (Asian caution skips a real
entry).
"""

from datetime import datetime, timezone

import pandas as pd

from brain.compression_detector import MarketState
from brain.fvg_detector import FairValueGap, FVGStatus
from brain.session_context import SessionContext, TradingSession
from brain.world_model import WorldModelStore, build_world_model
from entry.entry_orchestrator import EntryOrchestrator
from entry.models import EntryConfig


def _ts() -> datetime:
    return datetime.now(timezone.utc)


def _make_fvg(kind="BULLISH", top=1.0850, bottom=1.0840) -> FairValueGap:
    return FairValueGap(
        kind=kind, top=top, bottom=bottom, midpoint=(top + bottom) / 2,
        size_pips=10.0, strength="STRONG", status=FVGStatus.OPEN,
        candle_index=50, timestamp=_ts(), timeframe="M5",
    )


def _bullish_m1(n=30, base=1.0835) -> pd.DataFrame:
    step = 0.00005
    return pd.DataFrame({
        "open": [base + i * step for i in range(n)],
        "high": [base + i * step + 0.0003 for i in range(n)],
        "low": [base + i * step - 0.0001 for i in range(n)],
        "close": [base + (i + 0.5) * step for i in range(n)],
        "tick_volume": [100 + i * 10 for i in range(n)],
    })


class FakeSession:
    def __init__(self, session):
        self._session = session

    def get_session(self):
        return self._session

    def get_session_multiplier(self, symbol, session):
        return 0.5

    def get_session_zone_weight(self, symbol, session):
        return 0.8


class FakeTick:
    def __init__(self, symbol, bid, ask):
        self.symbol = symbol
        self.bid = bid
        self.ask = ask
        self.timestamp = _ts()


class TestActiveFilteringConfig:
    def test_entry_config_defaults(self):
        c = EntryConfig()
        assert c.compression_conviction_boost == 1.2
        assert c.expansion_conviction_boost == 1.5
        assert c.asian_compression_min_conviction == 80

    def test_gold_profile_values(self):
        from brain.instrument_profile import get_profile
        g = get_profile("XAUUSD")
        assert g.compression_conviction_boost == 1.2
        assert g.expansion_conviction_boost == 1.5
        assert g.asian_compression_min_conviction == 80


class TestConvictionMultiplier:
    def _orch(self):
        return EntryOrchestrator(
            world_model_store=WorldModelStore(),
            config=EntryConfig(),
            pip_size_lookup=lambda _s: 0.0001,
        )

    def test_compressing_boost(self):
        assert self._orch()._conviction_multiplier("EURUSD", "COMPRESSING") == 1.2

    def test_expanding_boost(self):
        assert self._orch()._conviction_multiplier("EURUSD", "EXPANDING") == 1.5

    def test_other_states_neutral(self):
        orch = self._orch()
        assert orch._conviction_multiplier("EURUSD", "TRENDING") == 1.0
        assert orch._conviction_multiplier("EURUSD", "RANGING") == 1.0
        assert orch._conviction_multiplier("EURUSD", "") == 1.0


class TestShapedConviction:
    def _orch(self):
        return EntryOrchestrator(
            world_model_store=WorldModelStore(),
            config=EntryConfig(),
            pip_size_lookup=lambda _s: 0.0001,
        )

    def test_compression_boosts_score(self):
        orch = self._orch()
        assert orch._shaped_conviction("EURUSD", "LONG", 70, "COMPRESSING", "NY") == 84

    def test_expansion_boosts_score(self):
        orch = self._orch()
        assert orch._shaped_conviction("EURUSD", "LONG", 70, "EXPANDING", "LONDON") == 105

    def test_asian_compression_skips_weak(self):
        orch = self._orch()
        # 70 <= asian_min (80) → skip vote (None).
        assert orch._shaped_conviction("EURUSD", "LONG", 70, "COMPRESSING", "ASIAN") is None
        assert orch.stats.get("asian_compression_skips", 0) == 1

    def test_asian_compression_allows_strong(self):
        orch = self._orch()
        # 90 > 80 → not skipped, then boosted (90 * 1.2 = 108).
        shaped = orch._shaped_conviction("EURUSD", "LONG", 90, "COMPRESSING", "ASIAN")
        assert shaped == 108

    def test_asian_compression_only_when_both(self):
        orch = self._orch()
        # ASIAN but not COMPRESSING → no caution skip.
        assert orch._shaped_conviction("EURUSD", "LONG", 70, "RANGING", "ASIAN") == 70


class TestAsianCompressionEndToEnd:
    def _setup(self, market_state, session):
        store = WorldModelStore()
        decisions: list = []
        orch = EntryOrchestrator(
            world_model_store=store,
            config=EntryConfig(min_entry_score=50),
            pip_size_lookup=lambda _s: 0.0001,
            on_entry_decision=lambda d: decisions.append(d),
            get_m1_dataframe=lambda _s: _bullish_m1(),
            get_market_state=lambda _s: market_state,
            session_context=FakeSession(session),
        )
        wm = build_world_model(
            symbol="EURUSD",
            version=store.next_version(),
            fvgs={"M5": [_make_fvg()]},
            bias={"direction": "LONG", "score": 70,
                  "long_probability": 0.7, "short_probability": 0.1},
        )
        store.publish(wm)
        orch.on_world_model_update("EURUSD")
        return orch, decisions

    def test_asian_compression_skips_entry(self):
        orch, decisions = self._setup(MarketState.COMPRESSING, TradingSession.ASIAN)
        orch.on_tick(FakeTick("EURUSD", 1.0844, 1.0845))
        orch.on_m1_close("EURUSD")
        assert decisions == []
        assert orch.stats.get("asian_compression_skips", 0) == 1

    def test_compressing_ny_still_enters(self):
        orch, decisions = self._setup(MarketState.COMPRESSING, TradingSession.NY)
        orch.on_tick(FakeTick("EURUSD", 1.0844, 1.0845))
        orch.on_m1_close("EURUSD")
        assert len(decisions) == 1


class TestSessionSizeMultipliers:
    """The session multipliers event_driven_bootstrap multiplies risk% by."""

    def test_default_session_multipliers(self):
        sc = SessionContext()
        assert sc.get_session_multiplier("XAUUSD", TradingSession.ASIAN) == 0.5
        assert sc.get_session_multiplier("XAUUSD", TradingSession.LONDON) == 1.2
        assert sc.get_session_multiplier("XAUUSD", TradingSession.NY) == 1.0
        assert sc.get_session_multiplier(
            "XAUUSD", TradingSession.LONDON_NY_OVERLAP
        ) == 1.3
