"""Tests for entry.m1_confirmation — M1CandleConfirmer."""

from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest

from entry.m1_confirmation import M1CandleConfirmer, ConfirmationResult
from entry.models import EntryConfig, EntryZone, ZoneType


def _make_zone(direction="LONG", top=1.0850, bottom=1.0840) -> EntryZone:
    now = datetime.now(timezone.utc)
    return EntryZone(
        symbol="EURUSD", direction=direction, zone_type=ZoneType.FVG_MIDPOINT,
        top=top, bottom=bottom, midpoint=(top + bottom) / 2,
        invalidation_level=bottom - 0.0005 if direction == "LONG" else top + 0.0005,
        conviction=85, created_at=now,
        expires_at=now + timedelta(minutes=15), timeframe="M5",
    )


def _bullish_m1(n=20, base=1.0840) -> pd.DataFrame:
    """Generates a bullish M1 dataframe (rising closes)."""
    data = {
        "open": [base + i * 0.0001 for i in range(n)],
        "high": [base + i * 0.0001 + 0.0003 for i in range(n)],
        "low": [base + i * 0.0001 - 0.0001 for i in range(n)],
        "close": [base + (i + 1) * 0.0001 for i in range(n)],
        "tick_volume": [100 + i * 10 for i in range(n)],
    }
    return pd.DataFrame(data)


def _bearish_m1(n=20, base=1.0860) -> pd.DataFrame:
    """Generates a bearish M1 dataframe (falling closes)."""
    data = {
        "open": [base - i * 0.0001 for i in range(n)],
        "high": [base - i * 0.0001 + 0.0001 for i in range(n)],
        "low": [base - i * 0.0001 - 0.0003 for i in range(n)],
        "close": [base - (i + 1) * 0.0001 for i in range(n)],
        "tick_volume": [100 + i * 10 for i in range(n)],
    }
    return pd.DataFrame(data)


def _flat_m1(n=20, base=1.0845) -> pd.DataFrame:
    """Flat / ranging M1 data."""
    data = {
        "open": [base] * n,
        "high": [base + 0.0001] * n,
        "low": [base - 0.0001] * n,
        "close": [base] * n,
        "tick_volume": [50] * n,
    }
    return pd.DataFrame(data)


class TestM1ConfirmationMomentum:
    def test_bullish_momentum_confirms_long(self):
        confirmer = M1CandleConfirmer()
        zone = _make_zone("LONG")
        df = _bullish_m1()
        result = confirmer.on_m1_close("EURUSD", "LONG", zone, df)
        assert result.confirmed is True

    def test_bearish_momentum_confirms_short(self):
        confirmer = M1CandleConfirmer()
        zone = _make_zone("SHORT", top=1.0860, bottom=1.0850)
        df = _bearish_m1()
        result = confirmer.on_m1_close("EURUSD", "SHORT", zone, df)
        assert result.confirmed is True

    def test_flat_data_no_confirmation(self):
        confirmer = M1CandleConfirmer()
        zone = _make_zone("LONG")
        df = _flat_m1()
        result = confirmer.on_m1_close("EURUSD", "LONG", zone, df)
        assert result.confirmed is False


class TestM1ConfirmationTimeout:
    def test_timeout_after_n_candles(self):
        cfg = EntryConfig(m1_confirmation_timeout_candles=3)
        confirmer = M1CandleConfirmer(config=cfg)
        zone = _make_zone("LONG")
        df = _flat_m1()
        # The first (timeout - 1) candles are still "waiting"; the timeout-th
        # candle gives up (count >= timeout).
        for i in range(2):
            result = confirmer.on_m1_close("EURUSD", "LONG", zone, df)
            assert result.confirmed is False
        result = confirmer.on_m1_close("EURUSD", "LONG", zone, df)
        assert result.confirmed is False
        assert "timeout" in result.reason.lower()

    def test_clear_resets_candle_count(self):
        cfg = EntryConfig(m1_confirmation_timeout_candles=3)
        confirmer = M1CandleConfirmer(config=cfg)
        zone = _make_zone("LONG")
        df = _flat_m1()
        confirmer.on_m1_close("EURUSD", "LONG", zone, df)
        confirmer.clear("EURUSD")
        confirmer.on_m1_close("EURUSD", "LONG", zone, df)
        result = confirmer.on_m1_close("EURUSD", "LONG", zone, df)
        assert "timeout" not in result.reason.lower()


class TestM1ConfirmationInsufficientData:
    def test_too_few_bars_rejects(self):
        cfg = EntryConfig(m1_min_bars=50)
        confirmer = M1CandleConfirmer(config=cfg)
        zone = _make_zone("LONG")
        df = _bullish_m1(n=10)
        result = confirmer.on_m1_close("EURUSD", "LONG", zone, df)
        assert result.confirmed is False
        assert "Insufficient" in result.reason

    def test_none_dataframe_rejects(self):
        confirmer = M1CandleConfirmer()
        zone = _make_zone("LONG")
        result = confirmer.on_m1_close("EURUSD", "LONG", zone, None)
        assert result.confirmed is False

    def test_confirmed_clears_state(self):
        confirmer = M1CandleConfirmer()
        zone = _make_zone("LONG")
        df = _bullish_m1()
        result = confirmer.on_m1_close("EURUSD", "LONG", zone, df)
        assert result.confirmed is True
        assert "EURUSD" not in confirmer._pending_candle_counts

    def test_reset_clears_all(self):
        confirmer = M1CandleConfirmer()
        confirmer._pending_candle_counts["EURUSD"] = 3
        confirmer._pending_candle_counts["GBPUSD"] = 2
        confirmer.reset()
        assert len(confirmer._pending_candle_counts) == 0
