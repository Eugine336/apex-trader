"""
Tests for P1 risk-safety: asset-cluster correlation, margin guardian, realized P&L.
Uses REAL instruments (GER40, JP225, US500, AUS200, ETHUSD, V75_1S, CRASH1000)
to prove the correlation cap actually bites on the instruments the system trades.
"""

import pytest
from brain.correlation_engine import (
    ASSET_CLUSTER,
    CorrelationEngine,
    OpenTrade,
)
from brain.currency_strength import CURRENCY_PAIRS
from config import RiskConfig


# ═══════════════════════════════════════════════════════════════════════════
# ITEM 1 — ASSET-CLUSTER CORRELATION
# ═══════════════════════════════════════════════════════════════════════════

class TestAssetClusterMap:
    def test_all_indices_mapped(self):
        for sym in ["US100", "US30", "US500", "GER40", "UK100", "JP225", "AUS200", "FRA40", "HK50"]:
            assert sym in ASSET_CLUSTER, f"{sym} missing from ASSET_CLUSTER"
            assert ASSET_CLUSTER[sym] == "equity_risk_on"

    def test_commodities_mapped(self):
        assert ASSET_CLUSTER["XAUUSD"] == "metals"
        assert ASSET_CLUSTER["XAGUSD"] == "metals"
        assert ASSET_CLUSTER["XBRUSD"] == "energy"
        assert ASSET_CLUSTER["XTIUSD"] == "energy"

    def test_crypto_mapped(self):
        for sym in ["BTCUSD", "ETHUSD", "LTCUSD", "SOLUSD"]:
            assert ASSET_CLUSTER[sym] == "crypto"

    def test_synthetics_mapped(self):
        assert ASSET_CLUSTER["V75_1S"] == "volatility_index"
        assert ASSET_CLUSTER["CRASH1000"] == "crash"
        assert ASSET_CLUSTER["BOOM500"] == "boom"

    def test_forex_not_in_cluster(self):
        assert "EURUSD" not in ASSET_CLUSTER
        assert "GBPJPY" not in ASSET_CLUSTER


class TestClusterCorrelationBlocking:
    """Prove that 3+ same-direction index longs get blocked — the exact
    scenario from the live logs (AUS200+GER40+JP225+US500 all LONG)."""

    def test_third_index_long_blocked(self):
        engine = CorrelationEngine(max_cluster_same_direction=2)
        existing = [
            OpenTrade(pair="GER40", direction="LONG", risk_pct=0.01),
            OpenTrade(pair="US500", direction="LONG", risk_pct=0.01),
        ]
        ok, reason = engine.can_open_trade("AUS200", "LONG", existing)
        assert not ok
        assert "equity_risk_on" in reason
        assert "2" in reason

    def test_opposite_direction_allowed(self):
        engine = CorrelationEngine(max_cluster_same_direction=2)
        existing = [
            OpenTrade(pair="GER40", direction="LONG", risk_pct=0.01),
            OpenTrade(pair="US500", direction="LONG", risk_pct=0.01),
        ]
        ok, _ = engine.can_open_trade("JP225", "SHORT", existing)
        assert ok, "Opposite direction in same cluster should be allowed"

    def test_different_cluster_allowed(self):
        engine = CorrelationEngine(max_cluster_same_direction=2)
        existing = [
            OpenTrade(pair="GER40", direction="LONG", risk_pct=0.01),
            OpenTrade(pair="US500", direction="LONG", risk_pct=0.01),
        ]
        ok, _ = engine.can_open_trade("XAUUSD", "LONG", existing)
        assert ok, "Different cluster should not be blocked"

    def test_crypto_cluster_blocks(self):
        engine = CorrelationEngine(max_cluster_same_direction=2)
        existing = [
            OpenTrade(pair="BTCUSD", direction="LONG", risk_pct=0.01),
            OpenTrade(pair="ETHUSD", direction="LONG", risk_pct=0.01),
        ]
        ok, reason = engine.can_open_trade("SOLUSD", "LONG", existing)
        assert not ok
        assert "crypto" in reason

    def test_synthetic_vol_cluster_blocks(self):
        engine = CorrelationEngine(max_cluster_same_direction=2)
        existing = [
            OpenTrade(pair="V75_1S", direction="SHORT", risk_pct=0.01),
            OpenTrade(pair="V100_1S", direction="SHORT", risk_pct=0.01),
        ]
        ok, reason = engine.can_open_trade("V50_1S", "SHORT", existing)
        assert not ok
        assert "volatility_index" in reason


class TestForexCorrelationUnchanged:
    """Regression: forex-major correlation logic must not change."""

    def test_eurusd_gbpusd_correlated(self):
        engine = CorrelationEngine(max_correlated_trades=1)
        existing = [OpenTrade(pair="EURUSD", direction="LONG", risk_pct=0.02)]
        ok, reason = engine.can_open_trade("GBPUSD", "LONG", existing)
        assert not ok
        assert "correlated" in reason.lower()

    def test_eurusd_usdjpy_correlated(self):
        engine = CorrelationEngine(max_correlated_trades=1)
        existing = [OpenTrade(pair="EURUSD", direction="LONG", risk_pct=0.02)]
        ok, _ = engine.can_open_trade("USDJPY", "SHORT", existing)
        assert not ok

    def test_unrelated_forex_allowed(self):
        engine = CorrelationEngine(max_correlated_trades=2)
        existing = [OpenTrade(pair="EURUSD", direction="LONG", risk_pct=0.02)]
        ok, _ = engine.can_open_trade("AUDNZD", "LONG", existing)
        assert ok


class TestCountCorrelatedNonForex:
    """Verify _count_correlated now returns >0 for non-forex."""

    def test_index_counted(self):
        engine = CorrelationEngine()
        existing = [
            OpenTrade(pair="US100", direction="LONG", risk_pct=0.01),
            OpenTrade(pair="US30", direction="LONG", risk_pct=0.01),
        ]
        count = engine._count_correlated("US500", existing)
        assert count == 2

    def test_forex_still_counted_by_currency(self):
        engine = CorrelationEngine()
        existing = [OpenTrade(pair="EURUSD", direction="LONG", risk_pct=0.02)]
        count = engine._count_correlated("GBPUSD", existing)
        assert count == 1

    def test_unknown_symbol_returns_zero(self):
        engine = CorrelationEngine()
        count = engine._count_correlated("ZZZZZ", [])
        assert count == 0


class TestClusterDirectionCounts:
    def test_cluster_counts_populated(self):
        engine = CorrelationEngine()
        trades = [
            OpenTrade(pair="GER40", direction="LONG", risk_pct=0.01),
            OpenTrade(pair="US500", direction="LONG", risk_pct=0.01),
            OpenTrade(pair="JP225", direction="SHORT", risk_pct=0.01),
        ]
        exposure = engine.calculate_exposure(trades)
        assert exposure.cluster_counts is not None
        eri = exposure.cluster_counts["equity_risk_on"]
        assert eri["LONG"] == 2
        assert eri["SHORT"] == 1


# ═══════════════════════════════════════════════════════════════════════════
# ITEM 2 — MARGIN GUARDIAN
# ═══════════════════════════════════════════════════════════════════════════

class TestMarginGuardianConfig:
    def test_defaults_off(self):
        cfg = RiskConfig()
        assert cfg.margin_guardian_enabled is False
        assert cfg.margin_warn_pct == 200.0
        assert cfg.margin_block_entry_pct == 150.0
        assert cfg.margin_flatten_pct == 100.0

    def test_cluster_default(self):
        cfg = RiskConfig()
        assert cfg.max_cluster_same_direction == 2


class MockConnector:
    def __init__(self, margin_level: float = 300.0):
        self._margin_level = margin_level

    def get_account_info(self):
        from platforms.base_connector import AccountInfo
        return AccountInfo(
            balance=10000, equity=9500, margin=1000, free_margin=8500,
            margin_level=self._margin_level, currency="USD", leverage=100,
            platform="mt5",
        )


class TestMarginEntryCheck:
    """Test the _check_margin_for_entry helper logic."""

    def test_margin_below_block_floor_rejected(self):
        from platforms.main_loop import TradingLoop
        loop = TradingLoop.__new__(TradingLoop)
        loop.config = type("C", (), {"risk": RiskConfig(margin_guardian_enabled=True, margin_block_entry_pct=150)})()
        loop.platforms = type("PM", (), {
            "get_connector": lambda self, s: MockConnector(margin_level=120.0),
            "mt5_connectors": [],
            "_mt5_connected_flags": [],
        })()
        ok, reason = loop._check_margin_for_entry("EURUSD")
        assert not ok
        assert "120" in reason
        assert "150" in reason

    def test_margin_above_floor_passes(self):
        from platforms.main_loop import TradingLoop
        loop = TradingLoop.__new__(TradingLoop)
        loop.config = type("C", (), {"risk": RiskConfig(margin_guardian_enabled=True, margin_block_entry_pct=150)})()
        loop.platforms = type("PM", (), {
            "get_connector": lambda self, s: MockConnector(margin_level=200.0),
            "mt5_connectors": [],
            "_mt5_connected_flags": [],
        })()
        ok, _ = loop._check_margin_for_entry("EURUSD")
        assert ok

    def test_margin_zero_skips_check(self):
        from platforms.main_loop import TradingLoop
        loop = TradingLoop.__new__(TradingLoop)
        loop.config = type("C", (), {"risk": RiskConfig(margin_guardian_enabled=True, margin_block_entry_pct=150)})()
        loop.platforms = type("PM", (), {
            "get_connector": lambda self, s: MockConnector(margin_level=0.0),
            "mt5_connectors": [],
            "_mt5_connected_flags": [],
        })()
        ok, reason = loop._check_margin_for_entry("V75_1S")
        assert ok, "margin_level=0 (Deriv/unknown) must not block"
        assert "unknown" in reason.lower()


# ═══════════════════════════════════════════════════════════════════════════
# ITEM 3 — REALIZED P&L
# ═══════════════════════════════════════════════════════════════════════════

class TestRealizedPnlBaseConnector:
    def test_base_returns_none(self):
        from platforms.base_connector import BaseConnector
        class Stub(BaseConnector):
            def connect(self): return True
            def disconnect(self): pass
            def is_connected(self): return True
            def get_account_info(self): pass
            def get_price(self, s): pass
            def get_ohlcv(self, s, t, c=200): pass
            def place_order(self, s, d, l, sl, tp, c=""): pass
            def modify_order(self, o, sl=None, tp=None): pass
            def close_order(self, o, l=None): pass
            def get_open_positions(self): return []
            def get_position_info(self, o): return None
            def get_spread(self, s): return 0.0
            def get_tick(self, s): pass
        stub = Stub()
        assert stub.get_realized_pnl("12345") is None
