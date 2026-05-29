"""
APEX TRADER — Scanner Tests
Verifies the pair scanner, ranker, and instrument registry.
"""

import pandas as pd
import numpy as np
import pytest
from datetime import datetime, timezone

from config import (
    get_instrument, get_instruments_by_category, get_instruments_by_platform,
    get_pip_size, get_all_symbols, INSTRUMENT_REGISTRY, AppConfig,
)
from scanner.pair_scanner import PairScanner, PairScanResult, ScanReport
from scanner.pair_ranker import PairRanker, RankedSetup
from scanner.scan_scheduler import ScanScheduler
from brain.session_engine import SessionStatus, NewsStatus, NewsEvent


# ---------------------------------------------------------------------------
# Helpers — create synthetic OHLC data
# ---------------------------------------------------------------------------

def _make_df(rows: int = 100, base: float = 1.27, pip_size: float = 0.0001) -> pd.DataFrame:
    np.random.seed(42)
    closes = base + np.cumsum(np.random.randn(rows) * pip_size * 5)
    highs = closes + np.random.rand(rows) * pip_size * 10
    lows = closes - np.random.rand(rows) * pip_size * 10
    opens = closes + np.random.randn(rows) * pip_size * 3
    times = pd.date_range("2025-01-01", periods=rows, freq="5min")
    return pd.DataFrame({
        "open": opens, "high": highs, "low": lows, "close": closes, "time": times,
    })


# ---------------------------------------------------------------------------
# Instrument registry tests
# ---------------------------------------------------------------------------

class TestInstrumentRegistry:
    def test_forex_major_pip_size(self):
        assert get_pip_size("EURUSD") == 0.0001
        assert get_pip_size("GBPUSD") == 0.0001

    def test_jpy_pair_pip_size(self):
        assert get_pip_size("USDJPY") == 0.01
        assert get_pip_size("EURJPY") == 0.01
        assert get_pip_size("GBPJPY") == 0.01

    def test_gold_pip_size(self):
        assert get_pip_size("XAUUSD") == 0.01

    def test_silver_pip_size(self):
        assert get_pip_size("XAGUSD") == 0.001

    def test_index_pip_size(self):
        assert get_pip_size("US100") == 0.1
        assert get_pip_size("JP225") == 1.0

    def test_synthetic_pip_size(self):
        assert get_pip_size("V75_1S") == 0.001
        assert get_pip_size("BOOM500") == 0.01

    def test_unknown_symbol_raises(self):
        with pytest.raises(KeyError):
            get_pip_size("FAKEPAIR")

    def test_total_instruments(self):
        assert len(INSTRUMENT_REGISTRY) == 59

    def test_category_counts(self):
        assert len(get_instruments_by_category("forex")) == 28
        assert len(get_instruments_by_category("commodity")) == 4
        assert len(get_instruments_by_category("index")) == 10
        assert len(get_instruments_by_category("synthetic")) == 17

    def test_platform_filter(self):
        mt5 = get_instruments_by_platform("mt5")
        deriv = get_instruments_by_platform("deriv")
        assert all(i.platform.value in ("mt5", "both") for i in mt5)
        assert all(i.platform.value in ("deriv", "both") for i in deriv)


# ---------------------------------------------------------------------------
# PairScanner tests
# ---------------------------------------------------------------------------

class TestPairScanner:
    def setup_method(self):
        self.scanner = PairScanner()
        self.utc = datetime(2025, 1, 6, 13, 0, tzinfo=timezone.utc)  # Monday London/NY overlap

    def test_scan_pair_returns_valid_result(self):
        h4 = _make_df(100)
        h1 = _make_df(200)
        m15 = _make_df(300)
        m5 = _make_df(500)
        result = self.scanner.scan_pair("EURUSD", h4, h1, m15, m5, utc_now=self.utc)
        assert isinstance(result, PairScanResult)
        assert result.pair == "EURUSD"

    def test_score_within_bounds(self):
        h4 = _make_df(100)
        h1 = _make_df(200)
        m15 = _make_df(300)
        m5 = _make_df(500)
        result = self.scanner.scan_pair("EURUSD", h4, h1, m15, m5, utc_now=self.utc)
        assert 0 <= result.score <= 100

    def test_status_values(self):
        h4 = _make_df(100)
        h1 = _make_df(200)
        m15 = _make_df(300)
        m5 = _make_df(500)
        result = self.scanner.scan_pair("GBPUSD", h4, h1, m15, m5, utc_now=self.utc)
        assert result.status in ("READY", "WATCHLIST", "WAITING")

    def test_gold_uses_correct_pip_size(self):
        h4 = _make_df(100, base=2000.0, pip_size=0.01)
        h1 = _make_df(200, base=2000.0, pip_size=0.01)
        m15 = _make_df(300, base=2000.0, pip_size=0.01)
        m5 = _make_df(500, base=2000.0, pip_size=0.01)
        result = self.scanner.scan_pair("XAUUSD", h4, h1, m15, m5, utc_now=self.utc)
        assert result.instrument_category == "commodity"

    def test_scan_all_produces_report(self):
        market_data = {
            "EURUSD": {
                "H4": _make_df(100), "H1": _make_df(200),
                "M15": _make_df(300), "M5": _make_df(500),
            },
            "GBPUSD": {
                "H4": _make_df(100), "H1": _make_df(200),
                "M15": _make_df(300), "M5": _make_df(500),
            },
        }
        report = self.scanner.scan_all(market_data, utc_now=self.utc)
        assert isinstance(report, ScanReport)
        assert report.total_pairs_scanned == 2
        assert report.results == sorted(report.results, key=lambda r: r.score, reverse=True)

    def test_ranging_caps_score(self):
        np.random.seed(99)
        flat = pd.DataFrame({
            "open": [1.27] * 100,
            "high": [1.2705] * 100,
            "low": [1.2695] * 100,
            "close": [1.27] * 100,
            "time": pd.date_range("2025-01-01", periods=100, freq="5min"),
        })
        result = self.scanner.scan_pair("EURUSD", flat, flat, flat, flat, utc_now=self.utc)
        assert result.score <= self.scanner.config.scoring.ranging_score_cap


# ---------------------------------------------------------------------------
# PairRanker tests
# ---------------------------------------------------------------------------

class TestPairRanker:
    def test_sorts_by_priority(self):
        now = datetime(2025, 1, 6, 13, 0, tzinfo=timezone.utc)
        r1 = PairScanResult(
            pair="EURUSD", direction="LONG", score=90, regime="BULLISH",
            trend_h4="BULLISH", trend_h1="BULLISH", bias_strength="STRONG",
            has_fvg=True, has_order_block=True, has_liquidity_target=True,
            sweep_detected=True, inducement_detected=False, wyckoff_phase="N/A",
            volume_confirmation=False, session_active=True,
            currency_strength_aligned=True, status="READY", timestamp=now,
        )
        r2 = PairScanResult(
            pair="GBPUSD", direction="LONG", score=85, regime="BULLISH",
            trend_h4="BULLISH", trend_h1="BULLISH", bias_strength="STRONG",
            has_fvg=True, has_order_block=False, has_liquidity_target=True,
            sweep_detected=False, inducement_detected=False, wyckoff_phase="N/A",
            volume_confirmation=False, session_active=True,
            currency_strength_aligned=False, status="READY", timestamp=now,
        )
        ranker = PairRanker()
        ranked = ranker.rank([r2, r1])
        assert ranked[0].result.pair == "EURUSD"
        assert ranked[1].result.pair == "GBPUSD"

    def test_correlation_penalty(self):
        now = datetime(2025, 1, 6, 13, 0, tzinfo=timezone.utc)
        r = PairScanResult(
            pair="EURUSD", direction="LONG", score=90, regime="BULLISH",
            trend_h4="BULLISH", trend_h1="BULLISH", bias_strength="STRONG",
            has_fvg=True, has_order_block=True, has_liquidity_target=True,
            sweep_detected=False, inducement_detected=False, wyckoff_phase="N/A",
            volume_confirmation=False, session_active=True,
            currency_strength_aligned=True, status="READY", timestamp=now,
        )
        ranker = PairRanker()
        without = ranker.rank([r])
        with_open = ranker.rank([r], open_trades=["EURGBP"])
        assert with_open[0].priority_score < without[0].priority_score


# ---------------------------------------------------------------------------
# ScanScheduler tests
# ---------------------------------------------------------------------------

class TestScanScheduler:
    def test_overlap_interval(self):
        scheduler = ScanScheduler()
        session = SessionStatus(
            current_session="OVERLAP_LONDON_NY", is_tradeable=True,
            liquidity="HIGH", best_pairs=[], minutes_to_next_session=60,
            session_open_minutes=30,
        )
        news = NewsStatus(
            is_clear=True, events_nearby=[], next_high_impact=None,
            affected_currencies=[], warning_message="",
        )
        assert scheduler.get_scan_interval(session, news) == 10

    def test_news_block_overrides(self):
        scheduler = ScanScheduler()
        session = SessionStatus(
            current_session="LONDON", is_tradeable=True,
            liquidity="HIGH", best_pairs=[], minutes_to_next_session=60,
            session_open_minutes=30,
        )
        news = NewsStatus(
            is_clear=False, events_nearby=[],
            next_high_impact=None, affected_currencies=["USD"],
            warning_message="NFP",
        )
        assert scheduler.get_scan_interval(session, news) == 120
