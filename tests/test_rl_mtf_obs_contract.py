"""
APEX RL — MTF Observation Contract Tests
==========================================
Definition-of-done for Phase 1.  All fixtures are synthetic
in-memory frames (no disk / network).
"""

from __future__ import annotations

import sys
from unittest.mock import MagicMock

if "torch" not in sys.modules:
    _m = MagicMock()
    for _name in [
        "torch", "torch.nn", "torch.nn.functional",
        "torch.optim", "torch.distributions",
    ]:
        sys.modules.setdefault(_name, _m)

from datetime import datetime, timezone, timedelta

import numpy as np
import pandas as pd
import pytest

from rl.contracts import (
    INSTRUMENT_CONTEXT_FEATURES,
    MARKET_FEATURES,
    N_CONTEXT_FEATURES,
    N_MARKET_FEATURES,
    N_TIMEFRAMES,
    OBS_SHAPE,
    TF_ORDER,
    TF_SECONDS,
    WINDOW,
    ATR_PERIOD,
    OBS_CONTRACT_VERSION,
    schema,
    schema_hash,
    assert_compatible,
)
from rl.multi_tf_obs_builder import (
    MultiTFObservationBuilder,
    build_context,
    symbol_id_for,
)


# ── Helpers ──────────────────────────────────────────────────────────────────

def _make_ohlcv(
    n: int,
    start: datetime,
    tf_seconds: int,
    base_price: float = 1.1000,
    seed: int = 42,
) -> pd.DataFrame:
    """Generate *n* synthetic OHLCV rows separated by *tf_seconds*."""
    rng = np.random.RandomState(seed)
    rows = []
    t = start
    price = base_price
    for _ in range(n):
        change = rng.normal(0, 0.0005)
        o = price
        c = price + change
        h = max(o, c) + abs(rng.normal(0, 0.0003))
        lo = min(o, c) - abs(rng.normal(0, 0.0003))
        v = float(rng.randint(100, 10000))
        rows.append({"time": t, "open": o, "high": h, "low": lo, "close": c, "volume": v})
        price = c
        t = t + timedelta(seconds=tf_seconds)
    return pd.DataFrame(rows)


def _enough_bars() -> int:
    return WINDOW + ATR_PERIOD + 30


def _make_frames(
    anchor: datetime,
    n: int | None = None,
    base_price: float = 1.1000,
    seed: int = 42,
) -> dict[str, pd.DataFrame]:
    """Build aligned synthetic frames for all four TFs ending near *anchor*."""
    if n is None:
        n = _enough_bars()
    frames: dict[str, pd.DataFrame] = {}
    for i, tf in enumerate(TF_ORDER):
        sec = TF_SECONDS[tf]
        start = anchor - timedelta(seconds=sec * n)
        frames[tf] = _make_ohlcv(n, start, sec, base_price=base_price, seed=seed + i)
    return frames


# ── (a) Shape, dtype, NaN-free, context shape, int symbol_id ─────────────────

class TestObservationShape:

    def test_obs_shape_and_dtype(self):
        anchor = datetime(2025, 6, 1, 12, 0, tzinfo=timezone.utc)
        frames = _make_frames(anchor)
        builder = MultiTFObservationBuilder()
        result = builder.build_from_frames(frames, "EURUSD")
        assert result is not None
        obs, ctx, sid = result

        assert obs.shape == OBS_SHAPE, f"Expected {OBS_SHAPE}, got {obs.shape}"
        assert obs.dtype == np.float32
        assert not np.any(np.isnan(obs))

    def test_context_shape(self):
        anchor = datetime(2025, 6, 1, 12, 0, tzinfo=timezone.utc)
        frames = _make_frames(anchor)
        builder = MultiTFObservationBuilder()
        result = builder.build_from_frames(frames, "EURUSD")
        assert result is not None
        _, ctx, _ = result

        assert ctx.shape == (N_CONTEXT_FEATURES,)
        assert ctx.dtype == np.float32

    def test_symbol_id_is_int(self):
        anchor = datetime(2025, 6, 1, 12, 0, tzinfo=timezone.utc)
        frames = _make_frames(anchor)
        builder = MultiTFObservationBuilder()
        result = builder.build_from_frames(frames, "EURUSD")
        assert result is not None
        _, _, sid = result
        assert isinstance(sid, int)

    def test_returns_none_on_insufficient_data(self):
        anchor = datetime(2025, 6, 1, 12, 0, tzinfo=timezone.utc)
        frames = _make_frames(anchor, n=10)
        builder = MultiTFObservationBuilder()
        result = builder.build_from_frames(frames, "EURUSD")
        assert result is None


# ── (b) No look-ahead: in-progress H4 bar must NOT change obs ───────────────

class TestNoLookAhead:

    def test_in_progress_h4_bar_ignored(self):
        anchor = datetime(2025, 6, 1, 12, 0, tzinfo=timezone.utc)
        frames_base = _make_frames(anchor)

        builder1 = MultiTFObservationBuilder()
        r1 = builder1.build_from_frames(frames_base, "EURUSD")
        assert r1 is not None
        obs_before, _, _ = r1

        frames_with_leak = {tf: df.copy() for tf, df in frames_base.items()}
        leak_time = anchor + timedelta(seconds=1)
        leak_row = pd.DataFrame([{
            "time": leak_time,
            "open": 9999.0,
            "high": 9999.0,
            "low": 9999.0,
            "close": 9999.0,
            "volume": 999999.0,
        }])
        frames_with_leak["H4"] = pd.concat(
            [frames_with_leak["H4"], leak_row], ignore_index=True,
        )

        builder2 = MultiTFObservationBuilder()
        r2 = builder2.build_from_frames(frames_with_leak, "EURUSD")
        assert r2 is not None
        obs_after, _, _ = r2

        np.testing.assert_array_equal(
            obs_before, obs_after,
            err_msg="In-progress H4 bar leaked into the observation",
        )

    def test_in_progress_h1_bar_ignored(self):
        anchor = datetime(2025, 6, 1, 12, 0, tzinfo=timezone.utc)
        frames_base = _make_frames(anchor)

        builder1 = MultiTFObservationBuilder()
        r1 = builder1.build_from_frames(frames_base, "EURUSD")
        assert r1 is not None
        obs_before, _, _ = r1

        frames_with_leak = {tf: df.copy() for tf, df in frames_base.items()}
        leak_time = anchor + timedelta(seconds=1)
        leak_row = pd.DataFrame([{
            "time": leak_time,
            "open": 9999.0,
            "high": 9999.0,
            "low": 9999.0,
            "close": 9999.0,
            "volume": 999999.0,
        }])
        frames_with_leak["H1"] = pd.concat(
            [frames_with_leak["H1"], leak_row], ignore_index=True,
        )

        builder2 = MultiTFObservationBuilder()
        r2 = builder2.build_from_frames(frames_with_leak, "EURUSD")
        assert r2 is not None
        obs_after, _, _ = r2

        np.testing.assert_array_equal(
            obs_before, obs_after,
            err_msg="In-progress H1 bar leaked into the observation",
        )


# ── (c) Closed-bar alignment ────────────────────────────────────────────────

class TestClosedBarAlignment:

    def test_higher_tf_selects_only_closed_bars(self):
        anchor = datetime(2025, 6, 1, 12, 0, tzinfo=timezone.utc)
        h4_sec = TF_SECONDS["H4"]

        n = _enough_bars()
        start = anchor - timedelta(seconds=h4_sec * (n + 1))
        h4_df = _make_ohlcv(n + 1, start, h4_sec, seed=99)

        from rl.multi_tf_obs_builder import _select_closed

        selected = _select_closed(h4_df, "H4", anchor)
        times = pd.to_datetime(selected["time"], utc=True)

        for t in times:
            close_time = t + pd.Timedelta(seconds=h4_sec)
            assert close_time <= anchor, (
                f"H4 bar open={t} close={close_time} not closed at anchor={anchor}"
            )

    def test_next_bar_excluded(self):
        anchor = datetime(2025, 6, 1, 12, 0, tzinfo=timezone.utc)
        h4_sec = TF_SECONDS["H4"]

        not_yet_closed = anchor - timedelta(seconds=h4_sec - 1)

        rows = [
            {"time": anchor - timedelta(seconds=h4_sec * 2), "open": 1.0, "high": 1.1, "low": 0.9, "close": 1.0, "volume": 100},
            {"time": not_yet_closed, "open": 1.0, "high": 1.1, "low": 0.9, "close": 1.0, "volume": 100},
        ]
        df = pd.DataFrame(rows)

        from rl.multi_tf_obs_builder import _select_closed

        selected = _select_closed(df, "H4", anchor)
        assert len(selected) == 1, "Bar not yet closed should be excluded"

    def test_clock_tf_includes_anchor_bar(self):
        anchor = datetime(2025, 6, 1, 12, 0, tzinfo=timezone.utc)
        m5_sec = TF_SECONDS["M5"]

        rows = [
            {"time": anchor - timedelta(seconds=m5_sec), "open": 1.0, "high": 1.1, "low": 0.9, "close": 1.0, "volume": 100},
            {"time": anchor, "open": 1.0, "high": 1.1, "low": 0.9, "close": 1.0, "volume": 100},
        ]
        df = pd.DataFrame(rows)

        from rl.multi_tf_obs_builder import _select_closed

        selected = _select_closed(df, "M5", anchor)
        assert len(selected) == 2, "Clock TF should include the anchor bar"


# ── (d) Parity: build_from_frames == incremental add_bar/build ───────────────

class TestTrainServeParity:

    def test_parity(self):
        anchor = datetime(2025, 6, 1, 12, 0, tzinfo=timezone.utc)
        frames = _make_frames(anchor)

        builder_batch = MultiTFObservationBuilder()
        r_batch = builder_batch.build_from_frames(frames, "EURUSD", in_trade=1.5)
        assert r_batch is not None
        obs_batch, ctx_batch, sid_batch = r_batch

        builder_incr = MultiTFObservationBuilder()
        for tf in TF_ORDER:
            df = frames[tf]
            for _, row in df.iterrows():
                builder_incr.add_bar(
                    tf=tf,
                    time=row["time"],
                    open=row["open"],
                    high=row["high"],
                    low=row["low"],
                    close=row["close"],
                    volume=row["volume"],
                )

        r_incr = builder_incr.build("EURUSD", in_trade=1.5)
        assert r_incr is not None
        obs_incr, ctx_incr, sid_incr = r_incr

        np.testing.assert_array_equal(
            obs_batch, obs_incr,
            err_msg="build_from_frames and add_bar/build produced different obs",
        )
        np.testing.assert_array_equal(
            ctx_batch, ctx_incr,
            err_msg="build_from_frames and add_bar/build produced different context",
        )
        assert sid_batch == sid_incr


# ── (e) in_trade injected into all four blocks ──────────────────────────────

class TestInTradeInjection:

    def test_in_trade_set_in_all_blocks(self):
        anchor = datetime(2025, 6, 1, 12, 0, tzinfo=timezone.utc)
        frames = _make_frames(anchor)
        in_trade_val = 2.0

        builder = MultiTFObservationBuilder()
        result = builder.build_from_frames(frames, "EURUSD", in_trade=in_trade_val)
        assert result is not None
        obs, _, _ = result

        it_idx = MARKET_FEATURES.index("in_trade")
        for k in range(N_TIMEFRAMES):
            col = k * N_MARKET_FEATURES + it_idx
            expected = np.clip(in_trade_val, -3, 3)
            np.testing.assert_allclose(
                obs[:, col],
                np.full(WINDOW, expected),
                err_msg=f"in_trade not injected in TF block {k} ({TF_ORDER[k]})",
            )

    def test_in_trade_clamped(self):
        anchor = datetime(2025, 6, 1, 12, 0, tzinfo=timezone.utc)
        frames = _make_frames(anchor)

        builder = MultiTFObservationBuilder()
        result = builder.build_from_frames(frames, "EURUSD", in_trade=10.0)
        assert result is not None
        obs, _, _ = result

        it_idx = MARKET_FEATURES.index("in_trade")
        for k in range(N_TIMEFRAMES):
            col = k * N_MARKET_FEATURES + it_idx
            assert np.all(obs[:, col] <= 3.0)


# ── (f) Context vector distinguishes instruments ────────────────────────────

class TestContextDistinguishes:

    def test_eurusd_vs_usdjpy(self):
        ctx_eur = build_context("EURUSD")
        ctx_jpy = build_context("USDJPY")
        assert not np.array_equal(ctx_eur, ctx_jpy)

        jpy_idx = INSTRUMENT_CONTEXT_FEATURES.index("is_jpy_pair")
        assert ctx_jpy[jpy_idx] == 1.0
        assert ctx_eur[jpy_idx] == 0.0

    def test_btcusd_crypto(self):
        ctx = build_context("BTCUSD")
        crypto_idx = INSTRUMENT_CONTEXT_FEATURES.index("is_crypto")
        assert ctx[crypto_idx] == 1.0

        always_open_idx = INSTRUMENT_CONTEXT_FEATURES.index("is_always_open")
        assert ctx[always_open_idx] == 1.0

    def test_xauusd_metal(self):
        ctx = build_context("XAUUSD")
        metal_idx = INSTRUMENT_CONTEXT_FEATURES.index("is_metal")
        assert ctx[metal_idx] == 1.0

    def test_us100_index(self):
        ctx = build_context("US100")
        idx = INSTRUMENT_CONTEXT_FEATURES.index("is_index")
        assert ctx[idx] == 1.0

    def test_symbol_id_from_universe(self):
        universe = ["EURUSD", "USDJPY", "BTCUSD"]
        assert symbol_id_for("EURUSD", universe) == 0
        assert symbol_id_for("USDJPY", universe) == 1
        assert symbol_id_for("BTCUSD", universe) == 2

    def test_symbol_id_deterministic_without_universe(self):
        a = symbol_id_for("EURUSD")
        b = symbol_id_for("EURUSD")
        assert a == b
        assert a != symbol_id_for("USDJPY")

    def test_symbol_id_unknown_in_universe_returns_sentinel(self):
        # A symbol that is not part of the supplied universe must return the
        # -1 "unknown" sentinel rather than a large hash that would overflow
        # the agent's symbol embedding table.
        universe = ["EURUSD", "USDJPY", "BTCUSD"]
        assert symbol_id_for("AUDNZD", universe) == -1


# ── (g) Schema hash deterministic; assert_compatible ─────────────────────────

class TestSchemaContract:

    def test_schema_hash_deterministic(self):
        h1 = schema_hash()
        h2 = schema_hash()
        assert h1 == h2
        assert len(h1) == 16

    def test_schema_returns_dict(self):
        s = schema()
        assert isinstance(s, dict)
        assert s["version"] == OBS_CONTRACT_VERSION
        assert s["obs_shape"] == list(OBS_SHAPE)

    def test_assert_compatible_accepts_good_meta(self):
        meta = {
            "obs_contract_version": OBS_CONTRACT_VERSION,
            "obs_schema_hash": schema_hash(),
        }
        assert_compatible(meta)

    def test_assert_compatible_rejects_bad_version(self):
        meta = {
            "obs_contract_version": "wrong-v99",
            "obs_schema_hash": schema_hash(),
        }
        with pytest.raises(ValueError, match="obs_contract_version"):
            assert_compatible(meta)

    def test_assert_compatible_rejects_bad_hash(self):
        meta = {
            "obs_contract_version": OBS_CONTRACT_VERSION,
            "obs_schema_hash": "0000000000000000",
        }
        with pytest.raises(ValueError, match="obs_schema_hash"):
            assert_compatible(meta)
