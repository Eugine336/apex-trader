"""Tests for the backtest harness (P0)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from backtest.broker import SimulatedBroker
from backtest.data import (
    Candle,
    DataValidationError,
    HistoricalDataLoader,
    validate_candles,
)
from backtest.results import BacktestResults, BacktestReporter, TradeRecord
from backtest.runner import BacktestRunner
from backtest.strategy import BarContext, Signal
from backtest import synthetic_data as sd


# ── helpers ────────────────────────────────────────────────────────────────

T0 = datetime(2024, 1, 1, tzinfo=timezone.utc)


def _candle(i: int, o: float, h: float, lo: float, c: float, vol: float = 100.0) -> Candle:
    return Candle(time=T0 + timedelta(minutes=5 * i), open=o, high=h, low=lo, close=c, volume=vol)


def _flat_series(n: int, price: float = 1.10) -> list[Candle]:
    return [_candle(i, price, price + 1e-4, price - 1e-4, price) for i in range(n)]


# ── HistoricalDataLoader ─────────────────────────────────────────────────────

def test_loader_csv_roundtrip(tmp_path: Path):
    p = tmp_path / "eur.csv"
    p.write_text(
        "time,open,high,low,close,volume\n"
        "2024-01-01T00:00:00Z,1.1000,1.1010,1.0990,1.1005,1000\n"
        "2024-01-01T00:05:00Z,1.1005,1.1020,1.1000,1.1015,1200\n"
    )
    candles = HistoricalDataLoader().load_csv(p, pair="EURUSD")
    assert len(candles) == 2
    assert candles[0].open == 1.1000
    assert candles[1].close == 1.1015


def test_loader_csv_column_aliases(tmp_path: Path):
    p = tmp_path / "eur.csv"
    p.write_text(
        "timestamp,o,h,l,c,vol\n"
        "1704067200,1.10,1.11,1.09,1.105,500\n"
    )
    candles = HistoricalDataLoader().load_csv(p, pair="EURUSD")
    assert len(candles) == 1
    assert candles[0].high == 1.11


def test_loader_json(tmp_path: Path):
    p = tmp_path / "eur.json"
    payload = [
        {"time": "2024-01-01T00:00:00Z", "open": 1.1, "high": 1.2, "low": 1.0, "close": 1.15},
        {"time": "2024-01-01T01:00:00Z", "open": 1.15, "high": 1.25, "low": 1.1, "close": 1.2},
    ]
    p.write_text(json.dumps(payload))
    candles = HistoricalDataLoader().load_json(p, pair="EURUSD")
    assert len(candles) == 2
    assert candles[-1].close == 1.2


def test_loader_auto_detect_json(tmp_path: Path):
    p = tmp_path / "x.json"
    p.write_text(json.dumps({"candles": [
        {"time": 1704067200, "open": 1, "high": 2, "low": 0.5, "close": 1.5},
    ]}))
    candles = HistoricalDataLoader().load(p)
    assert len(candles) == 1


def test_validation_rejects_bad_ohlc():
    bad = [Candle(time=T0, open=1.1, high=1.0, low=0.9, close=1.05)]  # high < open
    with pytest.raises(DataValidationError):
        validate_candles(bad)


def test_validation_rejects_duplicate_timestamps():
    dupe = [
        Candle(time=T0, open=1, high=1.1, low=0.9, close=1.05),
        Candle(time=T0, open=1.05, high=1.1, low=0.9, close=1.0),
    ]
    with pytest.raises(DataValidationError):
        validate_candles(dupe)


def test_validation_allows_gaps():
    gapped = [
        _candle(0, 1.1, 1.11, 1.09, 1.10),
        _candle(100, 1.1, 1.11, 1.09, 1.10),  # large gap, still increasing
    ]
    assert validate_candles(gapped) is gapped


def test_validation_rejects_empty():
    with pytest.raises(DataValidationError):
        validate_candles([])


# ── SimulatedBroker ──────────────────────────────────────────────────────────

def _broker_with_price(price: float = 1.10, **kw) -> SimulatedBroker:
    b = SimulatedBroker(slippage_pips=0.0, spread_pips=0.0, **kw)
    b.load_data("EURUSD", {"M5": _flat_series(3, price)})
    b.advance("EURUSD", _candle(0, price, price, price, price))
    return b


def test_broker_open_position():
    b = _broker_with_price()
    res = b.place_order("EURUSD", "BUY", 0.1, 1.0990, 1.1020)
    assert res.success
    assert b.open_count == 1
    assert b.get_position_info(res.order_id).direction == "BUY"


def test_broker_buy_sl_hit():
    b = _broker_with_price(1.10)
    b.place_order("EURUSD", "BUY", 1.0, 1.0990, 1.1100)
    # Next bar dips to the stop.
    fills = b.advance("EURUSD", _candle(1, 1.10, 1.10, 1.0980, 1.099))
    assert len(fills) == 1
    assert fills[0].exit_reason == "sl"
    assert fills[0].pnl < 0


def test_broker_buy_tp_hit():
    b = _broker_with_price(1.10)
    b.place_order("EURUSD", "BUY", 1.0, 1.0990, 1.1050)
    fills = b.advance("EURUSD", _candle(1, 1.10, 1.1060, 1.0995, 1.105))
    assert len(fills) == 1
    assert fills[0].exit_reason == "tp"
    assert fills[0].pnl > 0


def test_broker_sell_sl_hit():
    b = _broker_with_price(1.10)
    b.place_order("EURUSD", "SELL", 1.0, 1.1010, 1.0950)
    fills = b.advance("EURUSD", _candle(1, 1.10, 1.1020, 1.10, 1.101))
    assert len(fills) == 1
    assert fills[0].exit_reason == "sl"
    assert fills[0].pnl < 0


def test_broker_sell_tp_hit():
    b = _broker_with_price(1.10)
    b.place_order("EURUSD", "SELL", 1.0, 1.1010, 1.0950)
    fills = b.advance("EURUSD", _candle(1, 1.10, 1.10, 1.0940, 1.095))
    assert len(fills) == 1
    assert fills[0].exit_reason == "tp"
    assert fills[0].pnl > 0


def test_broker_sl_first_when_bar_straddles_both():
    # Pessimistic fill: if a bar can reach both SL and TP, SL fills first.
    b = _broker_with_price(1.10)
    b.place_order("EURUSD", "BUY", 1.0, 1.0990, 1.1010)
    fills = b.advance("EURUSD", _candle(1, 1.10, 1.1020, 1.0980, 1.10))
    assert fills[0].exit_reason == "sl"


def test_broker_spread_applied_on_entry():
    b = SimulatedBroker(slippage_pips=0.0, spread_pips=2.0)
    b.load_data("EURUSD", {"M5": _flat_series(2)})
    b.advance("EURUSD", _candle(0, 1.10, 1.10, 1.10, 1.10))
    res = b.place_order("EURUSD", "BUY", 0.1, 1.09, 1.11)
    # BUY fills at ask = mid + half-spread (1 pip = 0.0001).
    assert res.fill_price > 1.10
    assert abs(res.fill_price - 1.1001) < 1e-9


def test_broker_no_lookahead():
    b = SimulatedBroker()
    b.load_data("EURUSD", {"M5": _flat_series(10)})
    b.advance("EURUSD", _flat_series(10)[3])
    df = b.get_ohlcv("EURUSD", "M5", count=100)
    # Only bars at or before cursor (index 3) are visible → 4 rows.
    assert len(df) == 4


def test_broker_partial_close():
    b = _broker_with_price(1.10)
    res = b.place_order("EURUSD", "BUY", 1.0, 1.09, 1.11)
    out = b.close_order(res.order_id, lots=0.4)
    assert out.success
    assert abs(out.lots_closed - 0.4) < 1e-9
    # Remaining position still open with reduced size.
    assert abs(b.get_position_info(res.order_id).lots - 0.6) < 1e-9


def test_broker_modify_order():
    b = _broker_with_price(1.10)
    res = b.place_order("EURUSD", "BUY", 1.0, 1.09, 1.11)
    assert b.modify_order(res.order_id, new_sl=1.095, new_tp=1.12)
    pos = b.get_position_info(res.order_id)
    assert pos.sl == 1.095 and pos.tp == 1.12


def test_broker_trailing_stop_ratchets():
    b = _broker_with_price(1.10)
    res = b.place_order("EURUSD", "BUY", 1.0, 1.0990, 1.2000)
    b._positions[res.order_id].trail_distance_pips = 10.0
    # Price climbs — the trailing stop should ratchet up below the high.
    b.advance("EURUSD", _candle(1, 1.10, 1.1050, 1.1040, 1.1050))
    assert b.get_position_info(res.order_id).sl > 1.0990


def test_broker_account_equity_reflects_open_pnl():
    b = _broker_with_price(1.10)
    b.place_order("EURUSD", "BUY", 1.0, 1.09, 1.20)
    b.advance("EURUSD", _candle(1, 1.10, 1.1050, 1.10, 1.1050))
    info = b.get_account_info()
    assert info.equity > info.balance  # unrealised gain on the open long


# ── synthetic data ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("name", sorted(sd.GENERATORS))
def test_synthetic_generators_produce_valid_data(name: str):
    candles = sd.generate(name, candles=120, pair="EURUSD", timeframe="M5", seed=3)
    assert len(candles) >= 100
    validate_candles(candles)  # raises if structurally invalid


def test_synthetic_reproducible():
    a = sd.trending(candles=200, seed=11)
    b = sd.trending(candles=200, seed=11)
    assert [c.close for c in a] == [c.close for c in b]


def test_synthetic_unknown_raises():
    with pytest.raises(KeyError):
        sd.generate("does_not_exist")


# ── BacktestResults metrics ──────────────────────────────────────────────────

def _trade(pnl: float, r: float, i: int = 0, regime: str = "TRENDING_UP") -> TradeRecord:
    return TradeRecord(
        pair="EURUSD", direction="BUY", lots=0.1, open_price=1.1,
        close_price=1.1 + pnl / 1000.0,
        open_time=T0 + timedelta(hours=i), close_time=T0 + timedelta(hours=i, minutes=30),
        pnl=pnl, pnl_pips=pnl, pnl_r=r, exit_reason="tp" if pnl > 0 else "sl",
        regime=regime,
    )


def test_results_basic_metrics():
    res = BacktestResults(10_000.0)
    for i, (pnl, r) in enumerate([(100, 2.0), (-50, -1.0), (100, 2.0), (-50, -1.0)]):
        res.record_trade(_trade(pnl, r, i))
    res.record_equity(T0, 10_000)
    res.record_equity(T0 + timedelta(hours=4), 10_100)
    m = res.metrics()
    assert m["num_trades"] == 4
    assert m["wins"] == 2 and m["losses"] == 2
    assert m["win_rate"] == 0.5
    assert m["profit_factor"] == pytest.approx(2.0)  # 200 gains / 100 losses
    assert m["expectancy_r"] == pytest.approx(0.5)


def test_results_max_drawdown():
    res = BacktestResults(10_000.0)
    for i, eq in enumerate([10_000, 10_500, 10_200, 9_900, 10_300]):
        res.record_equity(T0 + timedelta(hours=i), eq)
    max_dd, length = res.max_drawdown()
    # Peak 10_500 → trough 9_900 = 600/10_500 ≈ 0.0571
    assert max_dd == pytest.approx(600 / 10_500, abs=1e-4)
    assert length >= 1


def test_results_breakdowns():
    res = BacktestResults(10_000.0)
    res.record_trade(_trade(100, 2.0, 0, regime="TRENDING_UP"))
    res.record_trade(_trade(-50, -1.0, 1, regime="RANGING"))
    per_regime = res.per_regime()
    assert set(per_regime) == {"TRENDING_UP", "RANGING"}
    assert per_regime["TRENDING_UP"]["trades"] == 1


def test_reporter_json_and_csv(tmp_path: Path):
    res = BacktestResults(10_000.0)
    res.record_trade(_trade(100, 2.0))
    res.record_equity(T0, 10_100)
    rep = BacktestReporter(res)
    jpath = tmp_path / "out.json"
    cpath = tmp_path / "trades.csv"
    rep.to_json(jpath)
    rep.to_trade_csv(cpath)
    assert jpath.exists() and cpath.exists()
    data = json.loads(jpath.read_text())
    assert data["metrics"]["num_trades"] == 1
    assert "EURUSD" in cpath.read_text()
    assert isinstance(rep.console_summary(), str)


# ── BacktestRunner ───────────────────────────────────────────────────────────

def test_runner_empty_data_raises():
    runner = BacktestRunner(data={}, base_timeframe="M5", progress_interval=0)
    with pytest.raises(ValueError):
        runner.run()


def test_runner_runs_synthetic_and_produces_results():
    candles = sd.trend_reversal(candles=400, pair="EURUSD", timeframe="M5", seed=5)
    runner = BacktestRunner(
        data={"EURUSD": {"M5": candles}}, base_timeframe="M5",
        progress_interval=0, slippage_pips=0.0,
    )
    res = runner.run()
    assert res.equity_curve  # non-empty
    assert isinstance(res.metrics()["num_trades"], int)
    runner.close()


def test_runner_single_trade_lifecycle():
    # One strategy signal → one order → one closed trade by end of run.
    class OneShot:
        fired = False

        def on_bar(self, ctx: BarContext):
            if not self.fired and len(ctx.history) >= 2:
                self.fired = True
                from backtest.broker import _instrument_pip_size
                pip = _instrument_pip_size(ctx.pair)
                price = ctx.candle.close
                return [Signal(ctx.pair, "BUY", price - 20 * pip, price + 40 * pip, 0.1)]
            return []

    candles = sd.trending(candles=100, seed=9)
    runner = BacktestRunner(
        data={"EURUSD": {"M5": candles}}, base_timeframe="M5",
        strategy=OneShot(), progress_interval=0, use_risk=False, use_regime=False,
    )
    res = runner.run()
    assert len(res.trades) == 1
    runner.close()


def test_runner_risk_gate_blocks_entries():
    # A risk manager that denies everything → zero trades, blocks recorded.
    class DenyAll:
        def can_open_position(self, *a, **kw):
            class _D:
                allowed = False
                rule = "test_block"
                reason = "blocked for test"
            return _D()

        def on_trade_closed(self, *a, **kw):
            pass

    candles = sd.trend_reversal(candles=300, seed=2)
    runner = BacktestRunner(
        data={"EURUSD": {"M5": candles}}, base_timeframe="M5",
        progress_interval=0, use_regime=False, use_risk=True,
        risk_manager=DenyAll(),
    )
    res = runner.run()
    assert len(res.trades) == 0
    assert res.blocked_signals > 0
    runner.close()


def test_runner_reproducible():
    def run():
        candles = sd.trend_reversal(candles=300, seed=4)
        r = BacktestRunner(
            data={"EURUSD": {"M5": candles}}, base_timeframe="M5",
            progress_interval=0, seed=99,
        )
        out = r.run()
        r.close()
        return out.metrics()["total_pnl"], len(out.trades)

    assert run() == run()


def test_runner_uses_temp_db_not_production(tmp_path, monkeypatch):
    # The runner must not create data/regime_detection.db etc. in the repo.
    candles = sd.trending(candles=120, seed=1)
    runner = BacktestRunner(
        data={"EURUSD": {"M5": candles}}, base_timeframe="M5",
        progress_interval=0,
    )
    runner.run()
    assert runner._tmpdir is not None
    # Regime/risk DBs live under the temp dir, not data/.
    tmpdir = runner._tmpdir
    assert "apex_backtest_" in str(tmpdir)
    assert Path(tmpdir).exists()
    runner.close()
    # close() removes the temp dir entirely.
    assert not Path(tmpdir).exists()


# ── config isolation ─────────────────────────────────────────────────────────

def test_backtest_config_defaults_active():
    from config import AppConfig

    cfg = AppConfig()
    assert cfg.backtest.enabled is True
    assert cfg.backtest.default_spread_pips == 2.0
    # The decision config still exists alongside the new backtest section.
    assert cfg.decision is not None


def test_backtest_config_validation():
    from config import BacktestConfig

    with pytest.raises(ValueError):
        BacktestConfig(default_spread_pips=-1.0)
    with pytest.raises(ValueError):
        BacktestConfig(starting_balance=0.0)
