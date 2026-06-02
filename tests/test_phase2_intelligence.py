"""
Tests for Phase 2 — Intelligence upgrades.
Covers: zone-proximity momentum filter, multi-bar BOS/CHOCH,
timeframe-aware stall exit, dynamic signal expiry, adaptive scan frequency.
"""

import pandas as pd
import numpy as np
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from brain.structure_engine import StructureEngine, StructureEvent
from trigger.entry_engine import EntryEngine, EntrySignal
from management.trade_manager import (
    TradeManager,
    TradeStatus,
    EntrySignal as TMEntrySignal,
)
from scanner.scan_scheduler import ScanScheduler


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_m1_momentum(direction: str, n: int = 25, include_volume: bool = False,
                       base_price: float = 1.08450) -> pd.DataFrame:
    """Create M1 candles with clear directional momentum in the last 5 bars.
    The first n-5 bars stay flat around base_price. The last 5 bars show
    small momentum steps (1 pip each) keeping price near the starting level."""
    pip = 0.0001
    np.random.seed(42)
    rows = []
    price = base_price
    for i in range(n):
        if i >= n - 5:
            if direction == "LONG":
                o = price
                c = price + 1 * pip
            else:
                o = price
                c = price - 1 * pip
        else:
            o = price
            c = price
        h = max(o, c) + pip
        lo = min(o, c) - pip
        row = {
            "time": pd.Timestamp("2025-01-01 08:00") + pd.Timedelta(minutes=i),
            "open": o, "high": h, "low": lo, "close": c,
        }
        if include_volume:
            row["tick_volume"] = 200 if i >= n - 5 else 100
        rows.append(row)
        price = c
    return pd.DataFrame(rows)


def _make_session_status(session: str = "LONDON"):
    return SimpleNamespace(
        current_session=session,
        is_tradeable=True,
        session_open_minutes=60,
    )


def _make_news_status(is_clear: bool = True):
    return SimpleNamespace(is_clear=is_clear, warning_message="")


# ===========================================================================
# 2.1 — Zone-Proximity Momentum Filter
# ===========================================================================

class TestMomentumZoneProximity:
    def setup_method(self):
        self.engine = EntryEngine()
        self.pip = 0.0001

    def test_momentum_at_zone_passes(self):
        zone = {"type": "FVG_MIDPOINT", "top": 1.08460, "bottom": 1.08440,
                "midpoint": 1.08450, "fvg": None, "ob": None, "has_sweep": False}
        df = _make_m1_momentum("LONG", base_price=1.08445)
        result = self.engine._detect_momentum_confirmation(df, "LONG", zone, self.pip)
        assert result is True

    def test_momentum_far_from_zone_rejected(self):
        zone = {"type": "FVG_MIDPOINT", "top": 1.08460, "bottom": 1.08440,
                "midpoint": 1.08450, "fvg": None, "ob": None, "has_sweep": False}
        df = _make_m1_momentum("LONG", base_price=1.09000)
        result = self.engine._detect_momentum_confirmation(df, "LONG", zone, self.pip)
        assert result is False

    def test_short_momentum_far_above_zone_rejected(self):
        zone = {"type": "OB_MIDPOINT", "top": 1.08460, "bottom": 1.08440,
                "midpoint": 1.08450, "fvg": None, "ob": None, "has_sweep": False}
        df = _make_m1_momentum("SHORT", base_price=1.07900)
        result = self.engine._detect_momentum_confirmation(df, "SHORT", zone, self.pip)
        assert result is False

    def test_no_zone_still_works(self):
        """When entry_zone is None, momentum check runs without proximity gate."""
        df = _make_m1_momentum("LONG", base_price=1.09000)
        result = self.engine._detect_momentum_confirmation(df, "LONG", None, self.pip)
        assert result is True

    def test_volume_filter_rejects_low_volume(self):
        """Momentum with all candles below average volume is rejected."""
        zone = {"type": "FVG_MIDPOINT", "top": 1.08460, "bottom": 1.08440,
                "midpoint": 1.08450, "fvg": None, "ob": None, "has_sweep": False}
        df = _make_m1_momentum("LONG", n=25, include_volume=True, base_price=1.08445)
        df["tick_volume"] = 50  # all below the 200 avg we'd set for momentum candles
        df.loc[df.index[-20:], "tick_volume"] = 200  # set non-momentum bars high
        df.loc[df.index[-5:], "tick_volume"] = 10    # momentum bars: very low
        result = self.engine._detect_momentum_confirmation(df, "LONG", zone, self.pip)
        assert result is False

    def test_volume_filter_passes_with_volume(self):
        """Momentum with at least one above-average volume candle passes."""
        zone = {"type": "FVG_MIDPOINT", "top": 1.08460, "bottom": 1.08440,
                "midpoint": 1.08450, "fvg": None, "ob": None, "has_sweep": False}
        df = _make_m1_momentum("LONG", n=25, include_volume=True, base_price=1.08445)
        result = self.engine._detect_momentum_confirmation(df, "LONG", zone, self.pip)
        assert result is True

    def test_no_tick_volume_column_skips_filter(self):
        """If tick_volume column missing, volume filter is skipped — still passes."""
        zone = {"type": "FVG_MIDPOINT", "top": 1.08460, "bottom": 1.08440,
                "midpoint": 1.08450, "fvg": None, "ob": None, "has_sweep": False}
        df = _make_m1_momentum("LONG", n=25, include_volume=False, base_price=1.08445)
        assert "tick_volume" not in df.columns
        result = self.engine._detect_momentum_confirmation(df, "LONG", zone, self.pip)
        assert result is True


# ===========================================================================
# 2.2 — Multi-bar BOS/CHOCH Confirmation
# ===========================================================================

class TestMultiBarBOSCHOCH:
    def _build_ohlc(self, data: list[dict]) -> pd.DataFrame:
        return pd.DataFrame(data)

    def test_body_confirmed_bullish_bos(self):
        """Full body above swing high → BOS confirmed.
        Signal bar is second-to-last; last bar is the still-forming candle
        which is ignored by the closed-bar contract (H2 fix).
        """
        engine = StructureEngine(swing_lookback=1, min_swing_size_pips=0.5)
        closes = [
            1.1000, 1.1010, 1.1005, 1.1015, 1.1008, 1.1020,
            1.1012, 1.1025, 1.1018, 1.1030,
            1.1022, 1.1035, 1.1028, 1.1042,
            1.1045,
        ]
        opens = [closes[0]] + closes[:-1]
        highs = [c + 0.0008 for c in closes]
        lows = [c - 0.0008 for c in closes]
        # Signal bar (now second-to-last): open AND close both above the last swing high
        opens[-2] = 1.1044
        closes[-2] = 1.1050
        highs[-2] = 1.1052
        lows[-2] = 1.1043
        df = pd.DataFrame({
            "time": pd.date_range("2025-01-01", periods=len(closes), freq="5min"),
            "open": opens, "high": highs, "low": lows, "close": closes,
        })
        analysis = engine.analyze(df)
        assert analysis.last_event in (StructureEvent.BOS_BULLISH, StructureEvent.CHOCH_BULLISH)

    def test_wick_only_break_rejected(self):
        """Close above swing high but open well below → wick fake, rejected.
        Signal bar is second-to-last per the closed-bar contract.
        """
        engine = StructureEngine(swing_lookback=1, min_swing_size_pips=0.5)
        closes = [
            1.1000, 1.1010, 1.1005, 1.1015, 1.1008, 1.1020,
            1.1012, 1.1025, 1.1018, 1.1030,
            1.1022, 1.1035, 1.1028, 1.1042,
            1.1040,
        ]
        opens = [closes[0]] + closes[:-1]
        highs = [c + 0.0008 for c in closes]
        lows = [c - 0.0008 for c in closes]
        # Signal bar (second-to-last): close barely above swing high, but open far below
        # AND bar before it closed well below the swing high
        opens[-2] = 1.1020  # open well below
        closes[-2] = 1.1044  # close barely above
        highs[-2] = 1.1046
        lows[-2] = 1.1018
        opens[-3] = 1.1025
        closes[-3] = 1.1028  # prev close well below
        df = pd.DataFrame({
            "time": pd.date_range("2025-01-01", periods=len(closes), freq="5min"),
            "open": opens, "high": highs, "low": lows, "close": closes,
        })
        analysis = engine.analyze(df)
        assert analysis.last_event == StructureEvent.NONE

    def test_prev_bar_confirmation_passes(self):
        """Close above swing high, open below, but previous bar also closed above → confirmed.
        Signal bar is second-to-last per the closed-bar contract.
        """
        engine = StructureEngine(swing_lookback=1, min_swing_size_pips=0.5)
        closes = [
            1.1000, 1.1010, 1.1005, 1.1015, 1.1008, 1.1020,
            1.1012, 1.1025, 1.1018, 1.1030,
            1.1022, 1.1035, 1.1028, 1.1042,
            1.1045,
        ]
        opens = [closes[0]] + closes[:-1]
        highs = [c + 0.0008 for c in closes]
        lows = [c - 0.0008 for c in closes]
        # Bar before signal closed above the swing high
        opens[-3] = 1.1038
        closes[-3] = 1.1044  # prev close above swing high
        highs[-3] = 1.1046
        lows[-3] = 1.1036
        # Signal bar (second-to-last): close above, open below (wick-like body but prev confirms)
        opens[-2] = 1.1030
        closes[-2] = 1.1048
        highs[-2] = 1.1050
        lows[-2] = 1.1028
        df = pd.DataFrame({
            "time": pd.date_range("2025-01-01", periods=len(closes), freq="5min"),
            "open": opens, "high": highs, "low": lows, "close": closes,
        })
        analysis = engine.analyze(df)
        assert analysis.last_event in (StructureEvent.BOS_BULLISH, StructureEvent.CHOCH_BULLISH)


# ===========================================================================
# 2.3 — Timeframe-Aware Stall Exit
# ===========================================================================

class TestTimeframeStallExit:
    def _make_trade(self, entry_timeframe: str = "M5", minutes_ago: int = 80):
        tm = TradeManager()
        sig = TMEntrySignal(
            pair="EURUSD", direction="LONG",
            entry_price=1.10000, stop_loss=1.09800,
            tp1=1.10200, tp2=1.10400,
            risk_reward_1=1.0, risk_reward_2=2.0,
            position_size_lots=0.50, score=90,
            entry_timeframe=entry_timeframe,
        )
        trade = tm.open_trade(sig)
        trade.entry_time = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
        return tm, trade

    def test_m5_stall_at_60_min(self):
        """M5 entry should stall-exit at 60 min, not 75."""
        tm, trade = self._make_trade("M5", minutes_ago=65)
        tm.update(trade, 1.10001)
        assert trade.status == TradeStatus.TIME_EXIT

    def test_m5_no_stall_at_55_min(self):
        """M5 entry should NOT stall before 60 min."""
        tm, trade = self._make_trade("M5", minutes_ago=55)
        tm.update(trade, 1.10001)
        assert trade.status != TradeStatus.TIME_EXIT

    def test_h1_survives_90_min(self):
        """H1 entry should survive at 90 min (limit is 180)."""
        tm, trade = self._make_trade("H1", minutes_ago=90)
        tm.update(trade, 1.10001)
        assert trade.status != TradeStatus.TIME_EXIT

    def test_h1_stalls_at_185_min(self):
        """H1 entry should stall-exit at 185 min."""
        tm, trade = self._make_trade("H1", minutes_ago=185)
        tm.update(trade, 1.10001)
        assert trade.status == TradeStatus.TIME_EXIT

    def test_m1_stalls_fast(self):
        """M1 entry stalls at 30 min."""
        tm, trade = self._make_trade("M1", minutes_ago=35)
        tm.update(trade, 1.10001)
        assert trade.status == TradeStatus.TIME_EXIT

    def test_default_fallback(self):
        """Unknown timeframe falls back to 75 min."""
        tm, trade = self._make_trade("UNKNOWN", minutes_ago=80)
        tm.update(trade, 1.10001)
        assert trade.status == TradeStatus.TIME_EXIT

    def test_stall_skipped_after_tp1(self):
        """Stall exit should not fire after TP1 partial close."""
        tm, trade = self._make_trade("M5", minutes_ago=65)
        tm.update(trade, 1.10200)  # hits TP1
        assert trade.partial_closed
        tm.update(trade, 1.10001)
        assert trade.status != TradeStatus.TIME_EXIT

    def test_entry_timeframe_in_managed_trade(self):
        """ManagedTrade stores entry_timeframe from signal."""
        tm = TradeManager()
        sig = TMEntrySignal(
            pair="EURUSD", direction="LONG",
            entry_price=1.10000, stop_loss=1.09800,
            tp1=1.10200, tp2=1.10400,
            risk_reward_1=1.0, risk_reward_2=2.0,
            position_size_lots=0.50, score=90,
            entry_timeframe="H4",
        )
        trade = tm.open_trade(sig)
        assert trade.entry_timeframe == "H4"


# ===========================================================================
# 2.4 — Dynamic Signal Expiry
# ===========================================================================

class TestDynamicSignalExpiry:
    def test_entry_signal_has_timeframe(self):
        """EntrySignal carries entry_timeframe field."""
        sig = EntrySignal(
            pair="EURUSD", direction="LONG", entry_type="FVG_MIDPOINT",
            entry_price=1.08450, stop_loss=1.08400, tp1=1.08550, tp2=1.08650,
            risk_reward_1=2.0, risk_reward_2=4.0, risk_pips=5.0,
            position_size_lots=0.5, score=90,
            entry_timeframe="H1",
        )
        assert sig.entry_timeframe == "H1"

    def test_default_timeframe_is_m5(self):
        sig = EntrySignal(
            pair="EURUSD", direction="LONG", entry_type="FVG_MIDPOINT",
            entry_price=1.08450, stop_loss=1.08400, tp1=1.08550, tp2=1.08650,
            risk_reward_1=2.0, risk_reward_2=4.0, risk_pips=5.0,
            position_size_lots=0.5, score=90,
        )
        assert sig.entry_timeframe == "M5"

    def test_determine_entry_timeframe_from_fvg(self):
        fvg = SimpleNamespace(timeframe="M15")
        zone = {"fvg": fvg, "ob": None}
        assert EntryEngine._determine_entry_timeframe(zone) == "M15"

    def test_determine_entry_timeframe_from_ob(self):
        ob = SimpleNamespace(timeframe="H1")
        zone = {"fvg": None, "ob": ob}
        assert EntryEngine._determine_entry_timeframe(zone) == "H1"

    def test_determine_entry_timeframe_default(self):
        zone = {"fvg": None, "ob": None}
        assert EntryEngine._determine_entry_timeframe(zone) == "M5"


# ===========================================================================
# 2.5 — Adaptive Scan Frequency
# ===========================================================================

class TestAdaptiveScanFrequency:
    def setup_method(self):
        self.scheduler = ScanScheduler()

    def test_dead_zone_without_positions(self):
        interval = self.scheduler.get_scan_interval(
            _make_session_status("DEAD"),
            _make_news_status(True),
            has_active_positions=False,
        )
        assert interval == 300

    def test_dead_zone_with_positions_capped(self):
        interval = self.scheduler.get_scan_interval(
            _make_session_status("DEAD"),
            _make_news_status(True),
            has_active_positions=True,
        )
        assert interval == 15

    def test_active_session_no_change_with_positions(self):
        interval = self.scheduler.get_scan_interval(
            _make_session_status("LONDON"),
            _make_news_status(True),
            has_active_positions=True,
        )
        assert interval == 10  # already below 15

    def test_quiet_session_capped_with_positions(self):
        interval = self.scheduler.get_scan_interval(
            _make_session_status("TOKYO"),
            _make_news_status(True),
            has_active_positions=True,
        )
        assert interval <= 15

    def test_should_scan_respects_positions(self):
        now = datetime.now(timezone.utc)
        last_scan = now - timedelta(seconds=16)
        result = self.scheduler.should_scan_now(
            last_scan,
            _make_session_status("DEAD"),
            _make_news_status(True),
            has_active_positions=True,
        )
        assert result is True  # 16s > 15s cap

    def test_should_scan_without_positions_respects_dead(self):
        now = datetime.now(timezone.utc)
        last_scan = now - timedelta(seconds=16)
        result = self.scheduler.should_scan_now(
            last_scan,
            _make_session_status("DEAD"),
            _make_news_status(True),
            has_active_positions=False,
        )
        assert result is False  # 16s < 300s dead zone
