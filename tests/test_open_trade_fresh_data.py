"""
Regression tests for fresh full-timeframe open-trade analysis.
"""

import sys
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import ANY, MagicMock, patch


_STUB_MODULES = [
    "MetaTrader5",
    "loguru",
    "requests",
    "websockets",
    "websockets.sync",
    "websockets.sync.client",
    "aiosqlite",
    "sqlalchemy",
    "pandas",
    "numpy",
    "torch",
    "torch.nn",
    "torch.nn.functional",
    "torch.optim",
    "torch.distributions",
]
for _mod in _STUB_MODULES:
    if _mod not in sys.modules:
        sys.modules[_mod] = MagicMock()

sys.modules["loguru"].logger = MagicMock()


from platforms.main_loop import TradingLoop


class _BoolSeries:
    def __init__(self, values):
        self._values = list(values)

    def sum(self):
        return sum(1 for v in self._values if v)


class _Series:
    def __init__(self, values):
        self._values = list(values)

    def __gt__(self, other):
        return _BoolSeries(a > b for a, b in zip(self._values, other._values))

    def __lt__(self, other):
        return _BoolSeries(a < b for a, b in zip(self._values, other._values))


class _Frame:
    def __init__(self, data):
        self._data = {k: list(v) for k, v in data.items()}
        first = next(iter(self._data.values()), [])
        self._len = len(first)

    def __len__(self):
        return self._len

    def __getitem__(self, key):
        return _Series(self._data[key])

    def tail(self, n):
        return _Frame({k: v[-n:] for k, v in self._data.items()})

    @property
    def columns(self):
        return list(self._data.keys())


def _ohlc(count: int, freq: str, start: str, drift: float = 0.0001):
    base = 1.1000
    closes = [base + i * drift for i in range(count)]
    opens = [base] + closes[:-1]
    highs = [max(o, c) + 0.0002 for o, c in zip(opens, closes)]
    lows = [min(o, c) - 0.0002 for o, c in zip(opens, closes)]
    times = [f"{start}:{i}:{freq}" for i in range(count)]
    return _Frame(
        {
            "time": times,
            "open": opens,
            "high": highs,
            "low": lows,
            "close": closes,
        },
    )


def _build_run_once_loop(with_position: bool = True):
    loop = TradingLoop.__new__(TradingLoop)
    loop._current_cycle_id = None
    loop._current_setup_id = None
    loop._health_check_interval = 10
    loop._last_scan_time = None
    loop._last_market_data = {"stale": {"H4": "old"}}
    loop._recovery_completed = False
    loop._last_reconcile_time = None
    loop._reconcile_interval_seconds = 30
    loop._reconcile_positions = MagicMock()

    loop.watchdog = MagicMock()
    loop.watchdog._cycles = 1
    loop.watchdog.record_cycle = MagicMock()
    loop.watchdog.check_health = MagicMock(return_value=SimpleNamespace(is_healthy=True, warnings=[]))
    loop.watchdog.is_scanner_blind = MagicMock(return_value=(False, ""))
    loop.watchdog.record_trade_check_success = MagicMock()
    loop.watchdog.record_trade_check_failure = MagicMock()
    loop.watchdog.record_scan_success = MagicMock()
    loop.watchdog.record_scan_failure = MagicMock()

    loop._check_and_reconnect = MagicMock()
    loop._check_pending_orders = MagicMock()
    loop._check_weekend_protection = MagicMock()
    loop._update_positions = MagicMock(return_value=0)
    loop._check_scale_in = MagicMock()
    loop._check_news_exit = MagicMock()
    loop._check_session_close = MagicMock()
    loop._check_portfolio_heat = MagicMock()
    loop._check_spread_deterioration = MagicMock()
    loop._analyse_open_trades = MagicMock()
    loop._fetch_open_trade_market_data = MagicMock(return_value={})

    loop.drawdown = MagicMock()
    loop.drawdown.can_trade = MagicMock(return_value=(True, "ok"))

    loop.scheduler = MagicMock()
    loop.scheduler.should_scan_now = MagicMock(return_value=False)
    loop.session_engine = MagicMock()
    loop.session_engine.get_status = MagicMock(
        return_value=SimpleNamespace(
            is_tradeable=True,
            current_session="LONDON",
            session_open_minutes=120,
        ),
    )
    loop.news_guard = MagicMock()
    loop.news_guard.check = MagicMock(return_value=SimpleNamespace(is_clear=True, warning_message=""))

    loop._has_always_open_instruments = MagicMock(return_value=False)
    loop._scan_breaker = MagicMock()
    loop._scan_breaker.can_execute = MagicMock(return_value=True)
    loop._scan_breaker.record_success = MagicMock()
    loop._scan_breaker.record_failure = MagicMock()
    loop._scan_breaker.get_status = MagicMock(return_value=SimpleNamespace(cooldown_remaining_seconds=0))
    loop._scan_and_enter = MagicMock()

    loop.config = SimpleNamespace(max_consecutive_cycle_failures=3, enabled_pairs=[])

    if with_position:
        loop.managed_positions = {"OID-1": SimpleNamespace(symbol="EURUSD")}
    else:
        loop.managed_positions = {}

    return loop


def _build_analysis_loop(now: datetime):
    loop = TradingLoop.__new__(TradingLoop)
    loop.config = SimpleNamespace(
        risk=SimpleNamespace(
            continuous_analysis_enabled=True,
            invalidation_min_hold_minutes=5.0,
            conviction_monitoring_enabled=True,
            conviction_decline_cycles=5,
            htf_reassessment_enabled=True,
            htf_reassess_on_h1_close=True,
            dynamic_sl_tightening_enabled=True,
            opportunity_cost_exit_mode="active",
            scale_in_enabled=True,
        ),
    )
    loop._position_scores = {}
    loop.scanner = MagicMock()
    loop.scanner.structure = MagicMock()
    loop._check_invalidation = MagicMock()
    loop._check_conviction_collapse = MagicMock()
    loop._check_htf_candle_close = MagicMock()
    loop._apply_dynamic_sl_tightening = MagicMock()
    loop._check_opportunity_cost_exit = MagicMock()
    loop._check_scale_in_on_scan = MagicMock()
    loop.position_store = MagicMock()
    loop.trade_manager = MagicMock()

    pos = SimpleNamespace(
        symbol="EURUSD",
        direction="BUY",
        open_time=now - timedelta(minutes=30),
        tm_trade_id="tm-1",
    )
    loop.managed_positions = {"OID-1": pos}
    return loop, pos


class TestOpenTradeFreshDataFetch:
    def test_fetch_targets_open_symbols_and_full_timeframes(self):
        loop = TradingLoop.__new__(TradingLoop)
        loop.managed_positions = {
            "1": SimpleNamespace(symbol="EURUSD"),
            "2": SimpleNamespace(symbol="USDJPY"),
            "3": SimpleNamespace(symbol="EURUSD"),
        }
        now = datetime(2026, 6, 11, 12, 0, tzinfo=timezone.utc)

        base_data = {
            "EURUSD": {
                "D1": _ohlc(220, "1D", "2025-01-01"),
                "H4": _ohlc(220, "4h", "2026-01-01"),
                "H1": _ohlc(220, "1h", "2026-01-01"),
                "M15": _ohlc(220, "15min", "2026-01-01"),
                "M5": _ohlc(220, "5min", "2026-01-01"),
            },
            "USDJPY": {
                "D1": _ohlc(220, "1D", "2025-01-01"),
                "H4": _ohlc(220, "4h", "2026-01-01"),
                "H1": _ohlc(220, "1h", "2026-01-01"),
                "M15": _ohlc(220, "15min", "2026-01-01"),
                "M5": _ohlc(220, "5min", "2026-01-01"),
            },
        }
        m1_data = {
            "EURUSD": {"M1": _ohlc(420, "1min", "2026-06-11")},
            "USDJPY": {"M1": _ohlc(420, "1min", "2026-06-11")},
        }

        loop.platforms = MagicMock()
        loop.platforms.fetch_all_market_data = MagicMock(side_effect=[base_data, m1_data])

        merged = TradingLoop._fetch_open_trade_market_data(loop, now)

        assert sorted(merged.keys()) == ["EURUSD", "USDJPY"]
        assert "D1" in merged["EURUSD"]
        assert "M1" in merged["EURUSD"]

        first_call = loop.platforms.fetch_all_market_data.call_args_list[0]
        second_call = loop.platforms.fetch_all_market_data.call_args_list[1]
        assert first_call.kwargs["symbols"] == ["EURUSD", "USDJPY"]
        assert first_call.kwargs["timeframes"] == ["D1", "H4", "H1", "M15", "M5"]
        assert first_call.kwargs["count"] == 200
        assert first_call.kwargs["now_utc"] == now
        assert second_call.kwargs["symbols"] == ["EURUSD", "USDJPY"]
        assert second_call.kwargs["timeframes"] == ["M1"]
        assert second_call.kwargs["count"] == 400
        assert second_call.kwargs["now_utc"] == now


class TestRunOnceFreshOpenTradeData:
    def test_run_once_uses_fresh_open_trade_data_not_scan_cache(self):
        loop = _build_run_once_loop(with_position=True)
        now_data = {"EURUSD": {"D1": "d1", "H4": "h4", "H1": "h1", "M15": "m15", "M5": "m5", "M1": "m1"}}
        loop._fetch_open_trade_market_data.return_value = now_data

        TradingLoop._run_once_inner(loop, "cycle-1")

        loop._fetch_open_trade_market_data.assert_called_once()
        loop._analyse_open_trades.assert_called_once_with(now_data, ANY)

    def test_run_once_skips_open_trade_strategy_when_fresh_data_missing(self):
        loop = _build_run_once_loop(with_position=True)
        loop._fetch_open_trade_market_data.return_value = {}

        with patch("platforms.main_loop.logger") as mock_logger:
            TradingLoop._run_once_inner(loop, "cycle-2")

        loop._analyse_open_trades.assert_not_called()
        warning_msgs = [str(c.args[0]) for c in mock_logger.warning.call_args_list]
        assert any("strategic analysis skipped" in m for m in warning_msgs)


class TestAnalyseOpenTradesFullTimeframe:
    def test_full_timeframe_context_adjusts_management_score(self):
        now = datetime(2026, 6, 11, 12, 0, tzinfo=timezone.utc)
        loop, _pos = _build_analysis_loop(now)

        d1_analysis = SimpleNamespace(
            trend=SimpleNamespace(value="BEARISH"),
            confidence=0.9,
            last_event=SimpleNamespace(value="NONE"),
        )
        m1_analysis = SimpleNamespace(
            trend=SimpleNamespace(value="BEARISH"),
            confidence=0.8,
            last_event=SimpleNamespace(value="CHOCH_BEARISH"),
        )
        loop.scanner.structure.analyze.side_effect = [d1_analysis, m1_analysis]
        loop.scanner.scan_pair.return_value = SimpleNamespace(
            score=70,
            direction="SHORT",
            confluences=[],
        )

        bearish_m1 = _ohlc(80, "1min", "2026-06-11", drift=-0.0001)
        market_data = {
            "EURUSD": {
                "D1": _ohlc(220, "1D", "2025-01-01", drift=-0.001),
                "H4": _ohlc(220, "4h", "2026-01-01", drift=0.0003),
                "H1": _ohlc(220, "1h", "2026-01-01", drift=0.0002),
                "M15": _ohlc(220, "15min", "2026-01-01", drift=0.0001),
                "M5": _ohlc(220, "5min", "2026-01-01", drift=0.0001),
                "M1": bearish_m1,
            },
        }

        TradingLoop._analyse_open_trades(loop, market_data, now)

        loop.scanner.scan_pair.assert_called_once_with(
            "EURUSD",
            market_data["EURUSD"]["H4"],
            market_data["EURUSD"]["H1"],
            market_data["EURUSD"]["M15"],
            market_data["EURUSD"]["M5"],
            {"EURUSD": market_data["EURUSD"]["H1"]},
            now,
        )
        loop.scanner.structure.analyze.assert_any_call(market_data["EURUSD"]["D1"])
        loop.scanner.structure.analyze.assert_any_call(market_data["EURUSD"]["M1"])

        invalidation_call = loop._check_invalidation.call_args
        adjusted_scan_result = invalidation_call.args[2]
        opposing_boost = invalidation_call.kwargs["opposing_score_boost"]
        assert adjusted_scan_result.score < 70
        assert opposing_boost > 0
        assert loop._position_scores["OID-1"][-1] == adjusted_scan_result.score

    def test_missing_required_frames_logs_and_skips_pair(self):
        now = datetime(2026, 6, 11, 12, 0, tzinfo=timezone.utc)
        loop, _pos = _build_analysis_loop(now)
        loop.scanner.scan_pair.return_value = SimpleNamespace(score=70, direction="LONG", confluences=[])

        market_data = {
            "EURUSD": {
                "D1": _ohlc(220, "1D", "2025-01-01"),
                "H4": _ohlc(220, "4h", "2026-01-01"),
                "H1": _ohlc(220, "1h", "2026-01-01"),
                "M5": _ohlc(220, "5min", "2026-01-01"),
                "M1": _ohlc(420, "1min", "2026-06-11"),
            },
        }

        with patch("platforms.main_loop.logger") as mock_logger:
            TradingLoop._analyse_open_trades(loop, market_data, now)

        loop.scanner.scan_pair.assert_not_called()
        warning_msgs = [str(c.args[0]) for c in mock_logger.warning.call_args_list]
        assert any("missing frames" in m for m in warning_msgs)
