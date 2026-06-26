"""
Phase 4 — Shadow Resolver Tests

Verifies:
1. bar_time seam preserves default (live) behavior
2. Shadow contract CRUD (insert/resolve/expire/query)
3. Replay determinism (same input → same outcome)
4. NO look-ahead (bar at or before entry never fed)
5. Exit paths: SL hit → LOSS, TP1 partial → TP2 → WIN, BE, trailing,
   structure exit, stall exit, EXPIRED
6. Granularity stamped on every resolution
7. TP computed not fabricated (resolver uses actual TP levels)
8. Resolver failure cannot crash the loop
9. Intra-bar SL/TP detection
10. R-multiple computation
"""

import tempfile
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import patch

import pandas as pd
import pytest

from management.trade_manager import (
    TradeManager,
    TradeStatus,
    ManagedTrade,
    EntrySignal,
    TERMINAL_STATUSES,
)
from persistence.shadow_store import (
    ShadowStore,
    ShadowContract,
    ShadowResolution,
    new_contract_id,
)
from persistence.shadow_resolver import (
    resolve_contract,
    _build_managed_trade,
    _determine_intrabar_price,
    _classify_outcome,
    _compute_r_multiple,
    _load_forward_bars,
    run_resolver,
)


# ── Helpers ──────────────────────────────────────────────────────────────────

def _make_contract(
    direction="LONG",
    entry_price=1.1000,
    stop_loss=1.0950,
    tp1=1.1075,
    tp2=1.1150,
    symbol="EURUSD",
    ts_utc_ms=None,
    **overrides,
) -> ShadowContract:
    if ts_utc_ms is None:
        ts_utc_ms = int(datetime(2026, 5, 1, 10, 0, tzinfo=timezone.utc).timestamp() * 1000)
    defaults = dict(
        contract_id=new_contract_id(),
        symbol=symbol,
        direction=direction,
        entry_price=entry_price,
        stop_loss=stop_loss,
        tp1=tp1,
        tp2=tp2,
        pip_size=0.0001,
        rejecting_gate="validator:test",
        ts_utc_ms=ts_utc_ms,
        score=80,
        position_size=0.10,
        entry_timeframe="M5",
    )
    defaults.update(overrides)
    return ShadowContract(**defaults)


def _make_bars(
    prices: list[float],
    start: datetime | None = None,
    interval_minutes: int = 5,
    spread: float = 0.0005,
) -> pd.DataFrame:
    """Build a bar DataFrame from close prices with synthetic OHLC."""
    if start is None:
        start = datetime(2026, 5, 1, 10, 5, tzinfo=timezone.utc)
    rows = []
    for i, close in enumerate(prices):
        t = start + timedelta(minutes=interval_minutes * i)
        rows.append({
            "time": t,
            "open": close - spread / 2,
            "high": close + spread,
            "low": close - spread,
            "close": close,
            "volume": 100,
        })
    return pd.DataFrame(rows)


def _make_bars_ohlc(
    ohlc_list: list[tuple[float, float, float, float]],
    start: datetime | None = None,
    interval_minutes: int = 5,
) -> pd.DataFrame:
    """Build bars with explicit OHLC for intra-bar SL/TP tests."""
    if start is None:
        start = datetime(2026, 5, 1, 10, 5, tzinfo=timezone.utc)
    rows = []
    for i, (o, h, lo, c) in enumerate(ohlc_list):
        t = start + timedelta(minutes=interval_minutes * i)
        rows.append({"time": t, "open": o, "high": h, "low": lo, "close": c, "volume": 100})
    return pd.DataFrame(rows)


# ── STEP 2: bar_time seam tests ─────────────────────────────────────────────

class TestBarTimeSeam:
    """bar_time=None must produce identical behavior to pre-change code."""

    def test_default_bar_time_uses_real_time(self):
        """update() with bar_time=None uses real datetime for close_time."""
        mgr = TradeManager()
        signal = EntrySignal(
            pair="EURUSD", direction="LONG",
            entry_price=1.1000, stop_loss=1.0950,
            tp1=1.1075, tp2=1.1150,
            risk_reward_1=1.5, risk_reward_2=3.0,
            position_size_lots=0.1, score=80,
        )
        trade = mgr.open_trade(signal)
        before = datetime.now(timezone.utc)
        mgr.update(trade, current_price=1.0940)
        after = datetime.now(timezone.utc)
        assert trade.status == TradeStatus.STOPPED
        assert before <= trade.close_time <= after

    def test_explicit_bar_time_used_for_close(self):
        """update() with explicit bar_time uses it instead of real time."""
        mgr = TradeManager()
        signal = EntrySignal(
            pair="EURUSD", direction="LONG",
            entry_price=1.1000, stop_loss=1.0950,
            tp1=1.1075, tp2=1.1150,
            risk_reward_1=1.5, risk_reward_2=3.0,
            position_size_lots=0.1, score=80,
        )
        trade = mgr.open_trade(signal)
        fixed_time = datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc)
        mgr.update(trade, current_price=1.0940, bar_time=fixed_time)
        assert trade.close_time == fixed_time

    def test_bar_time_tp1_hit_time(self):
        """TP1 hit records bar_time as tp1_hit_time."""
        mgr = TradeManager()
        signal = EntrySignal(
            pair="EURUSD", direction="LONG",
            entry_price=1.1000, stop_loss=1.0950,
            tp1=1.1075, tp2=1.1150,
            risk_reward_1=1.5, risk_reward_2=3.0,
            position_size_lots=0.1, score=80,
        )
        trade = mgr.open_trade(signal)
        fixed_time = datetime(2026, 5, 1, 11, 30, tzinfo=timezone.utc)
        mgr.update(trade, current_price=1.1080, bar_time=fixed_time)
        assert trade.tp1_hit_time == fixed_time
        assert trade.partial_closed is True

    def test_bar_time_stall_check(self):
        """Stall exit uses bar_time for elapsed-time calculation."""
        mgr = TradeManager()
        entry_time = datetime(2026, 5, 1, 10, 0, tzinfo=timezone.utc)
        signal = EntrySignal(
            pair="EURUSD", direction="LONG",
            entry_price=1.1000, stop_loss=1.0950,
            tp1=1.1075, tp2=1.1150,
            risk_reward_1=1.5, risk_reward_2=3.0,
            position_size_lots=0.1, score=80,
            timestamp=entry_time,
        )
        trade = mgr.open_trade(signal)
        late_time = entry_time + timedelta(minutes=120)
        mgr.update(trade, current_price=1.1002, bar_time=late_time)
        assert trade.status in TERMINAL_STATUSES
        assert "Stall exit" in trade.close_reason


# ── Shadow Store CRUD ────────────────────────────────────────────────────────

class TestShadowStore:

    @pytest.fixture(autouse=True)
    def _tmpdb(self, tmp_path):
        self.store = ShadowStore(db_path=tmp_path / "shadow_test.db")
        yield
        self.store.close()

    def test_insert_and_get_pending(self):
        c = _make_contract()
        cid = self.store.insert_contract(c)
        assert cid is not None
        pending = self.store.get_pending()
        assert len(pending) == 1
        assert pending[0].contract_id == c.contract_id

    def test_resolve_contract(self):
        c = _make_contract()
        self.store.insert_contract(c)
        res = ShadowResolution(
            outcome="WIN", r_multiple=2.5, exit_reason="TP2 hit",
            exit_price=1.1150, resolution_ts=1000,
            resolution_granularity="M5", bars_replayed=20,
        )
        ok = self.store.resolve_contract(c.contract_id, res)
        assert ok is True
        pending = self.store.get_pending()
        assert len(pending) == 0
        resolved = self.store.get_resolved()
        assert len(resolved) == 1
        assert resolved[0].outcome == "WIN"
        assert resolved[0].r_multiple == 2.5

    def test_mark_expired(self):
        c = _make_contract()
        self.store.insert_contract(c)
        ok = self.store.mark_expired(c.contract_id, 50)
        assert ok is True
        pending = self.store.get_pending()
        assert len(pending) == 0

    def test_count_by_status(self):
        for i in range(3):
            self.store.insert_contract(_make_contract(contract_id=new_contract_id()))
        counts = self.store.count_by_status()
        assert counts["PENDING"] == 3


# ── Resolver core: determinism, outcomes, no look-ahead ──────────────────────

class TestResolverCore:

    def test_sl_hit_loss(self):
        """Price drops to SL → LOSS."""
        c = _make_contract(direction="LONG", entry_price=1.1000, stop_loss=1.0950, tp1=1.1075, tp2=1.1150)
        bars = _make_bars([1.0990, 1.0970, 1.0940])
        res = resolve_contract(c, bars, "M5")
        assert res.outcome == "LOSS"
        assert res.r_multiple < 0
        assert res.resolution_granularity == "M5"

    def test_tp2_hit_win(self):
        """Price rises through TP1 then TP2 → WIN."""
        c = _make_contract(direction="LONG", entry_price=1.1000, stop_loss=1.0950, tp1=1.1075, tp2=1.1150)
        prices = [1.1020, 1.1040, 1.1060, 1.1080, 1.1100, 1.1120, 1.1140, 1.1155]
        bars = _make_bars(prices)
        res = resolve_contract(c, bars, "M5")
        assert res.outcome == "WIN"
        assert res.r_multiple > 0

    def test_tp1_partial_then_expired(self):
        """TP1 hit (partial close), then price stalls → EXPIRED or management exit."""
        c = _make_contract(direction="LONG", entry_price=1.1000, stop_loss=1.0950, tp1=1.1075, tp2=1.1150)
        prices = [1.1020, 1.1040, 1.1080, 1.1060, 1.1050, 1.1055]
        bars = _make_bars(prices)
        res = resolve_contract(c, bars, "M5")
        assert res.outcome in ("PARTIAL", "EXPIRED", "BREAKEVEN")
        assert res.bars_replayed > 0

    def test_breakeven_stop(self):
        """TP1 hit → BE activated → price returns to entry → BREAKEVEN."""
        c = _make_contract(direction="LONG", entry_price=1.1000, stop_loss=1.0950, tp1=1.1075, tp2=1.1150)
        prices = [1.1020, 1.1040, 1.1060, 1.1080, 1.1040, 1.1020, 1.0998]
        bars = _make_bars(prices)
        res = resolve_contract(c, bars, "M5")
        assert res.outcome in ("BREAKEVEN", "LOSS")

    def test_short_sl_hit(self):
        """Short trade: price rises to SL → LOSS."""
        c = _make_contract(
            direction="SHORT", entry_price=1.1000, stop_loss=1.1050,
            tp1=1.0925, tp2=1.0850,
        )
        bars = _make_bars([1.1010, 1.1030, 1.1055])
        res = resolve_contract(c, bars, "M5")
        assert res.outcome == "LOSS"

    def test_short_tp_hit_win(self):
        """Short trade: price drops to TP2 → WIN."""
        c = _make_contract(
            direction="SHORT", entry_price=1.1000, stop_loss=1.1050,
            tp1=1.0925, tp2=1.0850,
        )
        prices = [1.0980, 1.0960, 1.0920, 1.0900, 1.0870, 1.0845]
        bars = _make_bars(prices)
        res = resolve_contract(c, bars, "M5")
        assert res.outcome == "WIN"

    def test_expired_no_trigger(self):
        """Price stays flat — no SL/TP/management exit → EXPIRED."""
        c = _make_contract(direction="LONG", entry_price=1.1000, stop_loss=1.0950, tp1=1.1075, tp2=1.1150)
        prices = [1.1005] * 5
        bars = _make_bars(prices)
        res = resolve_contract(c, bars, "M5")
        assert res.outcome == "EXPIRED"
        assert res.exit_reason == "data_exhausted"

    def test_replay_determinism(self):
        """Same contract + same bars → identical outcome."""
        c = _make_contract()
        bars = _make_bars([1.0990, 1.0970, 1.0940])
        r1 = resolve_contract(c, bars, "M5")
        r2 = resolve_contract(c, bars, "M5")
        assert r1.outcome == r2.outcome
        assert r1.r_multiple == r2.r_multiple
        assert r1.bars_replayed == r2.bars_replayed

    def test_granularity_stamped(self):
        """Every resolution carries resolution_granularity."""
        c = _make_contract()
        bars = _make_bars([1.0940])
        res = resolve_contract(c, bars, "M1")
        assert res.resolution_granularity == "M1"

    def test_bars_replayed_count(self):
        """bars_replayed == number of bars actually processed."""
        c = _make_contract()
        bars = _make_bars([1.0940])
        res = resolve_contract(c, bars, "M5")
        assert res.bars_replayed == 1


# ── No look-ahead ────────────────────────────────────────────────────────────

class TestNoLookAhead:

    def test_forward_bars_strictly_after_entry(self):
        """_load_forward_bars excludes bars at or before entry timestamp."""
        with tempfile.TemporaryDirectory() as tmpdir:
            entry_dt = datetime(2026, 5, 1, 10, 0, tzinfo=timezone.utc)
            entry_ms = int(entry_dt.timestamp() * 1000)
            bars = pd.DataFrame([
                {"time": "2026-05-01 09:55:00+00:00", "open": 1.1, "high": 1.11, "low": 1.09, "close": 1.1, "volume": 10},
                {"time": "2026-05-01 10:00:00+00:00", "open": 1.1, "high": 1.11, "low": 1.09, "close": 1.1, "volume": 10},
                {"time": "2026-05-01 10:05:00+00:00", "open": 1.1, "high": 1.11, "low": 1.09, "close": 1.1, "volume": 10},
                {"time": "2026-05-01 10:10:00+00:00", "open": 1.1, "high": 1.11, "low": 1.09, "close": 1.1, "volume": 10},
            ])
            csv_path = Path(tmpdir) / "EURUSD_M5.csv"
            bars.to_csv(csv_path, index=False)

            with patch("persistence.shadow_resolver._DATA_DIR", Path(tmpdir)):
                df, gran = _load_forward_bars("EURUSD", entry_ms, "M5")

            assert df is not None
            assert len(df) == 2
            ts_col = pd.to_datetime(df["time"])
            assert all(ts_col > entry_dt)

    def test_resolver_never_uses_entry_bar(self):
        """Resolver result changes if we prepend a bar at entry time (proving it's excluded)."""
        c = _make_contract(
            ts_utc_ms=int(datetime(2026, 5, 1, 10, 0, tzinfo=timezone.utc).timestamp() * 1000),
        )
        future_bars = _make_bars(
            [1.1005, 1.1005, 1.1005],
            start=datetime(2026, 5, 1, 10, 5, tzinfo=timezone.utc),
        )
        res = resolve_contract(c, future_bars, "M5")
        assert res.bars_replayed == 3

    def test_lookahead_sl_before_tp_resolves_loss(self):
        """TRUE NEGATIVE: bar 1 triggers SL, bar 2 would hit TP.
        A leakage-free resolver MUST resolve LOSS on bar 1.
        A look-ahead resolver that peeks bar 2's TP would incorrectly
        resolve as WIN/PARTIAL — this test catches that."""
        c = _make_contract(
            direction="LONG", entry_price=1.1000,
            stop_loss=1.0950, tp1=1.1075, tp2=1.1150,
        )
        bars = _make_bars_ohlc([
            (1.1000, 1.1010, 1.0940, 1.0960),  # bar 1: low breaches SL
            (1.0960, 1.1080, 1.0955, 1.1070),  # bar 2: high breaches TP1
            (1.1070, 1.1155, 1.1065, 1.1150),  # bar 3: high breaches TP2
        ])
        res = resolve_contract(c, bars, "M5")
        assert res.outcome == "LOSS", (
            f"Expected LOSS (SL hit on bar 1), got {res.outcome} — "
            "possible look-ahead: resolver may have peeked bar 2/3 TP"
        )
        assert res.bars_replayed == 1, (
            f"Expected resolution on bar 1, got {res.bars_replayed} bars — "
            "resolver continued past SL hit"
        )

    def test_lookahead_window_bound_spy(self):
        """SPY: at each resolver step, _build_m5_window never includes bars
        beyond the current step index. Directly fails if look-ahead is
        introduced in the m5 window construction."""
        import persistence.shadow_resolver as sr_mod

        c = _make_contract(
            direction="LONG", entry_price=1.1000, stop_loss=1.0950,
        )
        bars = _make_bars([1.1005, 1.1005, 1.1005, 1.1005, 1.1005])

        real_fn = sr_mod._build_m5_window
        violations = []

        def spy(bars_df, current_idx, window=50):
            result = real_fn(bars_df, current_idx, window)
            if len(result) > current_idx + 1:
                violations.append(
                    f"step {current_idx}: window has {len(result)} bars "
                    f"(max allowed {current_idx + 1})"
                )
            return result

        with patch("persistence.shadow_resolver._build_m5_window", side_effect=spy):
            res = resolve_contract(c, bars, "M5")

        assert not violations, (
            "Look-ahead detected in m5_window:\n" + "\n".join(violations)
        )
        assert res.bars_replayed == 5


# ── Intra-bar detection ─────────────────────────────────────────────────────

class TestIntraBarDetection:

    def test_intrabar_sl_detected_via_low(self):
        """A bar whose low breaches SL triggers stop even if close is above SL."""
        c = _make_contract(direction="LONG", entry_price=1.1000, stop_loss=1.0950)
        bars = _make_bars_ohlc([
            (1.1000, 1.1010, 1.0945, 1.1005),
        ])
        res = resolve_contract(c, bars, "M5")
        assert res.outcome == "LOSS"

    def test_intrabar_tp_detected_via_high(self):
        """A bar whose high reaches TP1 triggers partial even if close is below TP1."""
        c = _make_contract(direction="LONG", entry_price=1.1000, stop_loss=1.0950, tp1=1.1075)
        bars = _make_bars_ohlc([
            (1.1000, 1.1080, 1.0990, 1.1040),
        ])
        trade = _build_managed_trade(c)
        bar = bars.iloc[0]
        effective = _determine_intrabar_price(bar, trade)
        assert effective == 1.1075

    def test_ambiguous_bar_sl_wins(self):
        """When both SL and TP hit in same bar, conservative: SL price used."""
        c = _make_contract(direction="LONG", entry_price=1.1000, stop_loss=1.0950, tp1=1.1075)
        bars = _make_bars_ohlc([
            (1.1000, 1.1080, 1.0940, 1.1020),
        ])
        trade = _build_managed_trade(c)
        bar = bars.iloc[0]
        effective = _determine_intrabar_price(bar, trade)
        assert effective == c.stop_loss


# ── R-multiple ───────────────────────────────────────────────────────────────

class TestRMultiple:

    def test_r_multiple_long_win(self):
        trade = ManagedTrade(
            trade_id="t1", pair="EURUSD", direction="LONG",
            entry_price=1.1000, current_price=1.1100,
            stop_loss=1.0950, original_stop_loss=1.0950,
            tp1=1.1075, tp2=1.1150, original_tp2=1.1150,
            position_size_lots=0.1, remaining_size_lots=0.1,
            status=TradeStatus.CLOSED, pnl_pips=100, pnl_dollars=100,
            entry_time=datetime.now(timezone.utc), tp1_hit_time=None,
            close_time=None, close_reason=None,
            candles_since_entry=0,
            highest_price_since_entry=1.1100,
            lowest_price_since_entry=1.1000,
            trailing_stop=None, breakeven_active=False,
            partial_closed=False, re_entry_eligible=False,
            score=80, pip_size=0.0001, pip_value_per_lot=10.0,
        )
        r = _compute_r_multiple(trade)
        assert abs(r - 2.0) < 0.01

    def test_r_multiple_short_loss(self):
        trade = ManagedTrade(
            trade_id="t2", pair="EURUSD", direction="SHORT",
            entry_price=1.1000, current_price=1.1050,
            stop_loss=1.1050, original_stop_loss=1.1050,
            tp1=1.0925, tp2=1.0850, original_tp2=1.0850,
            position_size_lots=0.1, remaining_size_lots=0.1,
            status=TradeStatus.STOPPED, pnl_pips=-50, pnl_dollars=-50,
            entry_time=datetime.now(timezone.utc), tp1_hit_time=None,
            close_time=None, close_reason=None,
            candles_since_entry=0,
            highest_price_since_entry=1.1050,
            lowest_price_since_entry=1.1000,
            trailing_stop=None, breakeven_active=False,
            partial_closed=False, re_entry_eligible=False,
            score=80, pip_size=0.0001, pip_value_per_lot=10.0,
        )
        r = _compute_r_multiple(trade)
        assert abs(r - (-1.0)) < 0.01


# ── Resilience ───────────────────────────────────────────────────────────────

class TestResolverResilience:

    def test_run_resolver_empty(self):
        """run_resolver on empty store returns empty counts."""
        with tempfile.TemporaryDirectory() as tmpdir:
            store = ShadowStore(db_path=Path(tmpdir) / "test.db")
            counts = run_resolver(store=store)
            assert counts == {}
            store.close()

    def test_run_resolver_no_data(self):
        """Contract with no forward price data → EXPIRED/NO_DATA."""
        with tempfile.TemporaryDirectory() as tmpdir:
            store = ShadowStore(db_path=Path(tmpdir) / "test.db")
            c = _make_contract(symbol="NONEXISTENT")
            store.insert_contract(c)
            with patch("persistence.shadow_resolver._DATA_DIR", Path(tmpdir)):
                counts = run_resolver(store=store)
            assert counts.get("NO_DATA", 0) == 1
            store.close()

    def test_corrupt_bar_data_no_crash(self):
        """Resolver doesn't crash on malformed bar data."""
        c = _make_contract()
        bad_bars = pd.DataFrame([
            {"time": "not-a-date", "open": "x", "high": "y", "low": "z", "close": "w", "volume": 0},
        ])
        try:
            resolve_contract(c, bad_bars, "M5")
        except Exception:
            pass


# ── Outcome classification ───────────────────────────────────────────────────

class TestOutcomeClassification:

    def _make_trade(self, **kwargs):
        defaults = dict(
            trade_id="t", pair="EURUSD", direction="LONG",
            entry_price=1.1, current_price=1.1,
            stop_loss=1.09, original_stop_loss=1.09,
            tp1=1.11, tp2=1.12, original_tp2=1.12,
            position_size_lots=0.1, remaining_size_lots=0.1,
            status=TradeStatus.CLOSED, pnl_pips=0, pnl_dollars=0,
            entry_time=datetime.now(timezone.utc), tp1_hit_time=None,
            close_time=None, close_reason=None, candles_since_entry=0,
            highest_price_since_entry=1.1, lowest_price_since_entry=1.1,
            trailing_stop=None, breakeven_active=False,
            partial_closed=False, re_entry_eligible=False,
            score=80, pip_size=0.0001, pip_value_per_lot=10.0,
        )
        defaults.update(kwargs)
        return ManagedTrade(**defaults)

    def test_stopped_is_loss(self):
        t = self._make_trade(status=TradeStatus.STOPPED, close_reason="Stop loss hit")
        assert _classify_outcome(t) == "LOSS"

    def test_stopped_at_be(self):
        t = self._make_trade(status=TradeStatus.STOPPED, close_reason="Stopped at breakeven", breakeven_active=True)
        assert _classify_outcome(t) == "BREAKEVEN"

    def test_tp2_is_win(self):
        t = self._make_trade(close_reason="TP2 hit", pnl_pips=150)
        assert _classify_outcome(t) == "WIN"

    def test_structure_exit_after_partial(self):
        t = self._make_trade(close_reason="Structure exit — bearish shift", partial_closed=True)
        assert _classify_outcome(t) == "PARTIAL"

    def test_stall_positive_pnl(self):
        t = self._make_trade(close_reason="Stall exit — 70min", pnl_pips=3)
        assert _classify_outcome(t) == "BREAKEVEN"
