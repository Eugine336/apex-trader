"""
APEX TRADER — PR 9 regression tests.

P7 — Exit-cause taxonomy:
  * ExitCause enum covers every exit path.
  * ExitCause.from_reason classifies the free-text reasons that genuinely
    arrive as strings (broker-side / mechanical close_reason).

P8 — Per-trade portfolio-heat awareness in the mechanical manager:
  * TradeManager._heat_trail_factor maps the heat state to a trail multiplier.
  * _update_trailing tightens the structure buffer under DEFENSIVE/REDUCING/
    EMERGENCY, uses the normal width in NORMAL / when disabled, and never
    worsens the stop (no improvement → SL unchanged).
  * RiskConfig validates the factor bounds.

P9 — Management-mode surfacing:
  * The startup summary emits a grep-friendly MANAGEMENT_CONFIG block.
"""

from types import SimpleNamespace

import pandas as pd

from management.exit_cause import ExitCause
from management.trade_manager import EntrySignal as TMEntrySignal
from management.trade_manager import TradeManager, TradeStatus

# ── Helpers ────────────────────────────────────────────────────────────────


def _signal(**overrides) -> TMEntrySignal:
    defaults = dict(
        pair="EURUSD",
        direction="BUY",
        entry_price=1.1000,
        stop_loss=1.0970,
        tp1=1.1030,
        tp2=1.1060,
        risk_reward_1=1.0,
        risk_reward_2=2.0,
        position_size_lots=0.10,
        score=80,
    )
    defaults.update(overrides)
    return TMEntrySignal(**defaults)


def _m5_df(rows: int = 14) -> pd.DataFrame:
    data = []
    for i in range(rows):
        o = 1.10000 + i * 0.00010
        data.append(
            {
                "time": pd.Timestamp("2025-01-01") + pd.Timedelta(minutes=5 * i),
                "open": o,
                "high": o + 0.0002,
                "low": o - 0.0002,
                "close": o + 0.0001,
                "volume": 100,
            }
        )
    return pd.DataFrame(data)


class _RecordingTrail:
    """Stub trailing engine that records the buffer it was asked to use and
    returns a caller-controlled candidate, mirroring calculate_trail's contract
    (returns None when there is no improving stop)."""

    def __init__(self, buffer_pips=3.0, candidate=None):
        self.buffer_pips = buffer_pips
        self.candidate = candidate
        self.last_buffer = "UNSET"

    def calculate_trail(self, direction, current_stop, df_m5, pip_size, buffer_pips=None):
        self.last_buffer = buffer_pips
        return self.candidate


# ── P7: ExitCause enum ───────────────────────────────────────────────────────


class TestExitCauseEnum:
    def test_enum_covers_all_required_exit_paths(self):
        required = {
            "TP1_PARTIAL",
            "TP2_TARGET",
            "TP3_EXTENDED",
            "STOP_LOSS",
            "BREAKEVEN_STOP",
            "TRAILING_STOP",
            "STALL_EXIT",
            "STRUCTURE_EXIT",
            "THESIS_SECURE",
            "THESIS_DECAY",
            "NEWS_EXIT",
            "SESSION_CLOSE",
            "SPREAD_DETERIORATION",
            "HEAT_TRIM",
            "HEAT_EMERGENCY",
            "MARGIN_FLATTEN",
            "ACCOUNT_FLATTEN",
            "OPPORTUNITY_COST",
            "INVALIDATION",
            "CONVICTION_COLLAPSE",
            "HTF_CANDLE_CLOSE",
            "DYNAMIC_SL_TIGHTEN_EXIT",
            "BROKER_SIDE",
            "MANUAL",
            "UNKNOWN",
        }
        names = {c.name for c in ExitCause}
        missing = required - names
        assert not missing, f"ExitCause missing values: {missing}"

    def test_from_reason_none_and_empty(self):
        assert ExitCause.from_reason(None) is ExitCause.UNKNOWN
        assert ExitCause.from_reason("") is ExitCause.UNKNOWN
        assert ExitCause.from_reason("something never mapped") is ExitCause.UNKNOWN

    def test_from_reason_mechanical_paths(self):
        cases = {
            "Stop loss hit": ExitCause.STOP_LOSS,
            "Stopped at breakeven": ExitCause.BREAKEVEN_STOP,
            "TP2 hit": ExitCause.TP2_TARGET,
            "TP1_FULL_CLOSE_REOPEN": ExitCause.TP1_PARTIAL,
            "TP3 extended": ExitCause.TP3_EXTENDED,
            "Stall: no progress": ExitCause.STALL_EXIT,
            "Structure broken": ExitCause.STRUCTURE_EXIT,
            "TRAILING: SL moved": ExitCause.TRAILING_STOP,
        }
        for reason, expected in cases.items():
            assert ExitCause.from_reason(reason) is expected, reason

    def test_from_reason_strategic_and_guard_paths(self):
        cases = {
            "DECISION_ENGINE(SEVERE thesis collapse while in profit)": ExitCause.THESIS_DECAY,
            "thesis secure — profit banked": ExitCause.THESIS_SECURE,
            "INVALIDATION_OPPOSING(SELL@70)": ExitCause.INVALIDATION,
            "CONVICTION_COLLAPSE(scores:80→40)": ExitCause.CONVICTION_COLLAPSE,
            "HTF_H1_BEARISH_CLOSE": ExitCause.HTF_CANDLE_CLOSE,
            "NEWS_EXIT(CPI,pnl=0.2R)": ExitCause.NEWS_EXIT,
            "SESSION_CLOSE(US30)": ExitCause.SESSION_CLOSE,
            "OPPORTUNITY_COST(blocked=GBPUSD)": ExitCause.OPPORTUNITY_COST,
            "WEEKEND_FLATTEN": ExitCause.WEEKEND_PROTECTION,
            "EMERGENCY_CLOSE(sustained)": ExitCause.HEAT_EMERGENCY,
            "MARGIN_FLATTEN": ExitCause.MARGIN_FLATTEN,
            "DAILY_LOSS_FLATTEN": ExitCause.ACCOUNT_FLATTEN,
            "CLOSED_EXTERNALLY": ExitCause.BROKER_SIDE,
            "CLOSED_WHILE_OFFLINE": ExitCause.BROKER_SIDE,
        }
        for reason, expected in cases.items():
            assert ExitCause.from_reason(reason) is expected, reason

    def test_str_is_value(self):
        assert str(ExitCause.STALL_EXIT) == "stall_exit"


# ── P8: heat-aware trail factor ───────────────────────────────────────────────


class TestHeatTrailFactor:
    def _tm(self, **kw):
        return TradeManager(
            heat_trail_tighten_enabled=kw.get("enabled", True),
            heat_trail_factor_defensive=0.7,
            heat_trail_factor_reducing=0.5,
            heat_trail_factor_emergency=0.4,
        )

    def test_factor_per_state(self):
        tm = self._tm()
        assert tm._heat_trail_factor("DEFENSIVE") == 0.7
        assert tm._heat_trail_factor("REDUCING") == 0.5
        assert tm._heat_trail_factor("EMERGENCY") == 0.4
        # case-insensitive
        assert tm._heat_trail_factor("defensive") == 0.7

    def test_normal_and_unknown_use_full_width(self):
        tm = self._tm()
        assert tm._heat_trail_factor("NORMAL") == 1.0
        assert tm._heat_trail_factor("WHATEVER") == 1.0
        assert tm._heat_trail_factor(None) == 1.0

    def test_disabled_always_full_width(self):
        tm = self._tm(enabled=False)
        assert tm._heat_trail_factor("DEFENSIVE") == 1.0
        assert tm._heat_trail_factor("EMERGENCY") == 1.0


# ── P8: _update_trailing buffer application + never-worsen ─────────────────────


class TestUpdateTrailingHeat:
    def _trade(self, tm):
        trade = tm.open_trade(_signal())
        trade.breakeven_active = True
        trade.partial_closed = True
        return trade

    def test_tightens_buffer_under_heat(self):
        tm = TradeManager(
            heat_trail_tighten_enabled=True,
            heat_trail_factor_defensive=0.7,
            heat_trail_factor_reducing=0.5,
        )
        stub = _RecordingTrail(buffer_pips=3.0, candidate=1.0985)
        tm.trailing = stub
        trade = self._trade(tm)

        tm._update_trailing(trade, _m5_df(), trail_factor=0.5)
        assert stub.last_buffer == 3.0 * 0.5  # buffer shrunk → tighter trail
        assert trade.stop_loss == 1.0985

    def test_normal_state_uses_full_width(self):
        tm = TradeManager(heat_trail_tighten_enabled=True)
        stub = _RecordingTrail(buffer_pips=3.0, candidate=1.0985)
        tm.trailing = stub
        trade = self._trade(tm)

        tm._update_trailing(trade, _m5_df(), trail_factor=1.0)
        # factor 1.0 → no override (None) so the engine uses its own buffer
        assert stub.last_buffer is None

    def test_never_worsens_sl_when_no_improvement(self):
        tm = TradeManager(heat_trail_tighten_enabled=True)
        # candidate None mimics calculate_trail finding no improving stop.
        stub = _RecordingTrail(buffer_pips=3.0, candidate=None)
        tm.trailing = stub
        trade = self._trade(tm)
        original_sl = trade.stop_loss

        tm._update_trailing(trade, _m5_df(), trail_factor=0.5)
        assert trade.stop_loss == original_sl  # SL unchanged — never worsened

    def test_update_threads_heat_state_into_trail(self):
        tm = TradeManager(
            heat_trail_tighten_enabled=True,
            heat_trail_factor_reducing=0.5,
        )
        stub = _RecordingTrail(buffer_pips=3.0, candidate=None)
        tm.trailing = stub
        trade = self._trade(tm)
        # Keep price between SL and TP so update() reaches the trailing branch
        # without triggering a stop/target exit.
        tm.update(trade, 1.1010, _m5_df(), portfolio_heat_state="REDUCING")
        assert stub.last_buffer == 3.0 * 0.5


# ── P8: config validation ─────────────────────────────────────────────────────


class TestHeatTrailConfig:
    def test_factor_bounds_rejected(self):
        import pytest

        from config import RiskConfig

        with pytest.raises(ValueError):
            RiskConfig(heat_trail_factor_defensive=0.0)
        with pytest.raises(ValueError):
            RiskConfig(heat_trail_factor_reducing=1.5)
        with pytest.raises(ValueError):
            RiskConfig(management_status_log_interval_cycles=-1)

    def test_defaults_valid(self):
        from config import RiskConfig

        cfg = RiskConfig()
        assert 0.0 < cfg.heat_trail_factor_defensive <= 1.0
        assert 0.0 < cfg.heat_trail_factor_reducing <= 1.0
        assert cfg.management_status_log_interval_cycles >= 0


# ── P9: startup management-config summary ─────────────────────────────────────


class TestManagementConfigLog:
    def test_summary_emits_management_config_marker(self):
        from loguru import logger

        from config import DecisionConfig, RiskConfig
        from platforms.main_loop import TradingLoop

        sink = []
        handle = logger.add(lambda m: sink.append(m), level="INFO")
        try:
            stub = SimpleNamespace(
                config=SimpleNamespace(risk=RiskConfig(), decision=DecisionConfig()),
                _decision_enabled=True,
                _risk_governor=object(),
                trade_manager=SimpleNamespace(
                    strategic_structure_intact_threshold=0.6,
                    strategic_structure_max_age_seconds=600.0,
                ),
            )
            TradingLoop._log_management_configuration(stub)
        finally:
            logger.remove(handle)

        text = "".join(sink)
        assert "MANAGEMENT_CONFIG" in text
        assert "strategic_engine=ENABLED" in text
        assert "re_entry=LOGGING_ONLY" in text
        assert "heat_trail_tighten" in text
