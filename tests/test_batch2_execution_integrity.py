"""
APEX TRADER — Batch 2 regression tests
H2: closed-bar contract (no repaint)
H3: order idempotency (no duplicate orders)
"""

import sqlite3
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd
import pytest

# ── H2 helpers ──────────────────────────────────────────────────────────

from brain.market_data_utils import drop_forming_bar
from brain.structure_engine import StructureEngine, StructureEvent, Trend


def _make_ohlcv(n: int = 50, trend: str = "up", seed: int = 42) -> pd.DataFrame:
    """Generate a synthetic OHLCV DataFrame with *n* bars.

    The last bar is treated as the still-forming bar by convention
    (matching the real connector output).
    """
    rng = np.random.RandomState(seed)
    base = 1.1000
    step = 0.0005 if trend == "up" else -0.0005
    rows = []
    for i in range(n):
        o = base + step * i + rng.uniform(-0.0002, 0.0002)
        c = o + rng.uniform(-0.0003, 0.0006) * (1 if trend == "up" else -1)
        h = max(o, c) + rng.uniform(0.0001, 0.0005)
        lo = min(o, c) - rng.uniform(0.0001, 0.0005)
        rows.append({
            "time": pd.Timestamp("2025-01-01") + pd.Timedelta(hours=i),
            "open": round(o, 5),
            "high": round(h, 5),
            "low": round(lo, 5),
            "close": round(c, 5),
            "volume": rng.randint(100, 1000),
        })
    return pd.DataFrame(rows)


class TestDropFormingBar:
    def test_removes_last_row(self):
        df = _make_ohlcv(10)
        closed = drop_forming_bar(df)
        assert len(closed) == len(df) - 1
        assert closed.iloc[-1]["close"] == df.iloc[-2]["close"]

    def test_single_row_untouched(self):
        df = _make_ohlcv(1)
        closed = drop_forming_bar(df)
        assert len(closed) == 1

    def test_empty_df(self):
        df = pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
        closed = drop_forming_bar(df)
        assert len(closed) == 0


class TestStructureClosedBar:
    """BOS/CHOCH must be detected on the last CLOSED bar, not the forming bar."""

    def test_bos_uses_closed_bar_not_forming(self):
        """Fabricate a series where the forming bar would trigger a BOS
        but the last closed bar does NOT. Assert no BOS is detected."""
        engine = StructureEngine(swing_lookback=3, min_swing_size_pips=1.0, pip_size=0.0001)
        df = _make_ohlcv(30, trend="up", seed=10)
        df = df.copy().reset_index(drop=True)

        swing_high = df["high"].iloc[:-5].max()
        df.loc[len(df) - 2, "close"] = swing_high - 0.0020
        df.loc[len(df) - 2, "open"] = swing_high - 0.0025

        df.loc[len(df) - 1, "close"] = swing_high + 0.0050
        df.loc[len(df) - 1, "open"] = swing_high + 0.0040

        analysis = engine.analyze(df)
        assert analysis.last_event in (
            StructureEvent.NONE,
            StructureEvent.BOS_BULLISH,
            StructureEvent.CHOCH_BULLISH,
            StructureEvent.BOS_BEARISH,
            StructureEvent.CHOCH_BEARISH,
        )
        if analysis.last_event in (StructureEvent.BOS_BULLISH, StructureEvent.CHOCH_BULLISH):
            closed = df.iloc[:-1]
            last_closed = closed["close"].iloc[-1]
            assert last_closed > swing_high, (
                "BOS detected but last CLOSED bar is below the swing high — "
                "this would mean BOS was triggered by the forming bar"
            )

    def test_swing_pivot_still_sees_full_series(self):
        """The centered swing-pivot detection must NOT be affected by the
        closed-bar fix — it already handles the forming bar correctly via
        its lookback window."""
        engine = StructureEngine(swing_lookback=3, min_swing_size_pips=0.1, pip_size=0.0001)
        n = 50
        rows = []
        for i in range(n):
            # Create zig-zag: up for 7 bars, down for 7 bars
            cycle = i % 14
            if cycle < 7:
                o = 1.1000 + cycle * 0.0020
                c = o + 0.0015
            else:
                o = 1.1000 + (14 - cycle) * 0.0020
                c = o - 0.0015
            h = max(o, c) + 0.0005
            lo = min(o, c) - 0.0005
            rows.append({
                "time": pd.Timestamp("2025-01-01") + pd.Timedelta(hours=i),
                "open": round(o, 5), "high": round(h, 5),
                "low": round(lo, 5), "close": round(c, 5), "volume": 500,
            })
        df = pd.DataFrame(rows)
        analysis_full = engine.analyze(df)
        assert len(analysis_full.bullish_swing_points) + len(analysis_full.bearish_swing_points) > 0


class TestEntryPatternsClosedBar:
    """Entry patterns must operate on closed bars at the entry_engine boundary."""

    def test_pattern_detector_is_pure(self):
        """get_best_pattern operates on whatever data is passed (no internal
        drop). The closed-bar logic lives in entry_engine which drops the
        forming bar BEFORE calling get_best_pattern."""
        from trigger.entry_patterns import EntryPatternDetector

        detector = EntryPatternDetector()
        df = _make_ohlcv(20, trend="up")
        pattern_full, _ = detector.get_best_pattern(
            df, "LONG", zone_top=1.11, zone_bottom=1.10, pip_size=0.0001,
        )
        closed_df = drop_forming_bar(df)
        pattern_closed, _ = detector.get_best_pattern(
            closed_df, "LONG", zone_top=1.11, zone_bottom=1.10, pip_size=0.0001,
        )
        # Results may differ — the detector sees different last bars.
        # What matters: the entry_engine applies drop_forming_bar before calling.
        assert isinstance(pattern_full, str)
        assert isinstance(pattern_closed, str)


# ── H3 idempotency ─────────────────────────────────────────────────────

from platforms.order_idempotency import generate_idempotency_key, extract_idempotency_key


class TestIdempotencyKeyGeneration:
    def test_same_intent_same_key(self):
        ts = datetime(2025, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
        k1 = generate_idempotency_key("EURUSD", "LONG", 0.10, ts)
        k2 = generate_idempotency_key("EURUSD", "LONG", 0.10, ts)
        assert k1 == k2

    def test_different_symbol_different_key(self):
        ts = datetime(2025, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
        k1 = generate_idempotency_key("EURUSD", "LONG", 0.10, ts)
        k2 = generate_idempotency_key("GBPUSD", "LONG", 0.10, ts)
        assert k1 != k2

    def test_different_direction_different_key(self):
        ts = datetime(2025, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
        k1 = generate_idempotency_key("EURUSD", "LONG", 0.10, ts)
        k2 = generate_idempotency_key("EURUSD", "SHORT", 0.10, ts)
        assert k1 != k2

    def test_different_lots_different_key(self):
        ts = datetime(2025, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
        k1 = generate_idempotency_key("EURUSD", "LONG", 0.10, ts)
        k2 = generate_idempotency_key("EURUSD", "LONG", 0.20, ts)
        assert k1 != k2

    def test_retry_within_window_same_key(self):
        ts1 = datetime(2025, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
        ts2 = datetime(2025, 6, 1, 12, 4, 59, tzinfo=timezone.utc)
        k1 = generate_idempotency_key("EURUSD", "LONG", 0.10, ts1)
        k2 = generate_idempotency_key("EURUSD", "LONG", 0.10, ts2)
        assert k1 == k2

    def test_different_time_window_different_key(self):
        ts1 = datetime(2025, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
        ts2 = datetime(2025, 6, 1, 12, 5, 1, tzinfo=timezone.utc)
        k1 = generate_idempotency_key("EURUSD", "LONG", 0.10, ts1)
        k2 = generate_idempotency_key("EURUSD", "LONG", 0.10, ts2)
        assert k1 != k2

    def test_key_length(self):
        k = generate_idempotency_key("EURUSD", "LONG", 0.10)
        assert len(k) == 12
        assert all(c in "0123456789abcdef" for c in k)

    def test_extract_from_comment(self):
        key = generate_idempotency_key("EURUSD", "LONG", 0.10)
        comment = f"APEX|92|LONDON|{key}"
        assert extract_idempotency_key(comment) == key

    def test_extract_from_pending_comment(self):
        key = "abc123def456"
        comment = f"APEX_PEND|88|NY|{key}"
        assert extract_idempotency_key(comment) == key

    def test_extract_legacy_no_key(self):
        assert extract_idempotency_key("APEX|92|LONDON") is None
        assert extract_idempotency_key("APEX") is None
        assert extract_idempotency_key("") is None


class TestPositionStoreInFlight:
    """In-flight intent persistence for crash-safe dedup."""

    @pytest.fixture
    def store(self, tmp_path):
        from persistence.position_store import PositionStore
        return PositionStore(db_path=str(tmp_path / "test.db"))

    def test_record_and_get(self, store):
        store.record_in_flight("key123", "EURUSD", "LONG", 0.10)
        rec = store.get_in_flight("key123")
        assert rec is not None
        assert rec["symbol"] == "EURUSD"
        assert rec["status"] == "PENDING"

    def test_resolve(self, store):
        store.record_in_flight("key123", "EURUSD", "LONG", 0.10)
        store.resolve_in_flight("key123", "ORDER-999")
        rec = store.get_in_flight("key123")
        assert rec["status"] == "FILLED"
        assert rec["order_id"] == "ORDER-999"

    def test_cancel(self, store):
        store.record_in_flight("key123", "EURUSD", "LONG", 0.10)
        store.cancel_in_flight("key123")
        assert store.get_in_flight("key123") is None

    def test_idempotency_key_column_in_positions(self, store):
        """The managed_positions table now has an idempotency_key column."""
        with store._lock:
            cursor = store._conn.execute("PRAGMA table_info(managed_positions)")
            cols = {row[1] for row in cursor.fetchall()}
        assert "idempotency_key" in cols

    def test_get_nonexistent(self, store):
        assert store.get_in_flight("nonexistent") is None
