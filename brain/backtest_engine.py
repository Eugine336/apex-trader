"""
APEX TRADER — Backtest Engine
Replays historical candles candle-by-candle so every decision can be stress-tested
before real capital is exposed.

Drives the shared ED decision core (``brain.decision_core.analyze_window``) and the
SAME adjudication / management / sizing engines the live event-driven plane uses
(``form_thesis`` → ``EntryGate`` → ``SituationEngine``/``DecisionEngine`` →
``RiskGovernor`` → ``PortfolioDivision``/``PositionSizer``) — so backtest results
reflect the system actually traded, not a simpler parallel implementation.

The only deliberate divergence from live is broker mechanics: fills, slippage and
SL/TP detection come from candle OHLC instead of a live broker.  Set
``legacy_mode=True`` to restore the original geometry-only behaviour (flat
R-multiple sizing, SL/TP + 180-minute time stop, no thesis re-validation) for
side-by-side comparison.
"""

import asyncio
import copy
import os
import random
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Optional

import numpy as np
import pandas as pd

from brain.market_data_utils import drop_forming_bar
from brain.session_engine import SessionEngine
from brain.trade_journal import TradeJournal, TradeRecord
from loguru import logger


# ── Live-plane management constants (single source of truth) ─────────────
# Mirror the live event-driven plane so the backtest banks/scales exactly as
# it does.  TP1 partial + breakeven are tick mechanics owned by
# ``execution.position_worker`` (``_check_tp1``/``_check_breakeven``,
# default ``partial_close_ratio = 0.5``) — the backtest models that here, it is
# NOT a DecisionEngine action.  ``_DEFAULT_PARTIAL_CLOSE_RATIO`` and
# ``_SCALE_IN_RISK_FRACTION`` mirror ``event_driven_bootstrap`` for the
# strategic PARTIAL_CLOSE / SCALE_IN verdicts the governor/DE can emit.
_TP1_PARTIAL_RATIO = 0.5          # execution.position_worker WorkerConfig.partial_close_ratio
_DEFAULT_PARTIAL_CLOSE_RATIO = 0.5  # event_driven_bootstrap._DEFAULT_PARTIAL_CLOSE_RATIO
_SCALE_IN_RISK_FRACTION = 0.5     # event_driven_bootstrap._SCALE_IN_RISK_FRACTION

# ── Developing-analysis forming-candle boundaries (Phase 3 parity) ───────
# Per-timeframe candle length (seconds) used to aggregate the still-forming
# HTF candle from M1 bars during replay — mirrors ``tick.candle_close_detector``
# ``_TF_SECONDS`` for the HTFs the confirmed backtest slices carry (D1 is not
# in the replay slice set, so it is intentionally omitted).
_DEVELOPING_TF_SECONDS: dict[str, int] = {
    "M5": 300,
    "M15": 900,
    "H1": 3600,
    "H4": 14400,
}


class _BacktestSpreadMonitor:
    """Always-safe spread source for the ComplianceDivision in replay.

    ``ComplianceDivision._check_spread`` fails CLOSED when it has no spread
    source, so the backtest binds this trivial monitor: a candle replay has no
    live spread to blow out, and the pre-parity backtest already treated the
    spread veto as satisfied.  Matches the ``is_spread_safe(symbol, spread)``
    duck-typed contract the division calls.
    """

    @staticmethod
    def is_spread_safe(symbol: str, spread_pips: float) -> tuple[bool, str]:
        return True, "backtest spread ok"


@dataclass
class ATRComparisonResult:
    """Side-by-side metrics: structure-stop vs ATR-stop on the same setups.

    ATR counterfactual is force-closed at the structure trade's exit candle
    (single-position serial model); this dampens divergence and is a known
    limitation.
    """
    total_compared: int = 0
    total_skipped: int = 0
    structure_wins: int = 0
    structure_losses: int = 0
    structure_breakevens: int = 0
    structure_win_rate: float = 0.0
    structure_mean_r: float = 0.0
    structure_expectancy: float = 0.0
    atr_wins: int = 0
    atr_losses: int = 0
    atr_breakevens: int = 0
    atr_win_rate: float = 0.0
    atr_mean_r: float = 0.0
    atr_expectancy: float = 0.0
    expectancy_delta: float = 0.0
    structure_loss_rate: float = 0.0  # LOSS outcomes / total, not verified stop-hits
    atr_loss_rate: float = 0.0  # LOSS outcomes / total, not verified stop-hits


@dataclass
class BacktestResult:
    total_trades: int
    wins: int
    losses: int
    win_rate: float
    profit_factor: float
    sharpe_ratio: float
    max_drawdown: float
    max_consecutive_losses: int
    expectancy: float
    avg_hold_time: float
    best_pair: Optional[str]
    worst_pair: Optional[str]
    best_session: Optional[str]
    equity_curve: list[float]
    total_commission: float = 0.0
    total_slippage_cost: float = 0.0
    gross_profit_factor: float = 0.0
    net_profit_factor: float = 0.0
    atr_comparison: Optional[ATRComparisonResult] = None


@dataclass
class BacktestSetup:
    """Internal bridge: holds the fields the simulation reads, populated from
    PairScanResult (direction/score/regime) + EntrySignal (prices/targets)."""
    direction: str
    entry_price: float
    stop_loss: float
    tp1: float
    tp2: float
    score: int
    confluences: list[str] = field(default_factory=list)
    regime: str = ""
    bias_strength: str = ""
    timestamp: datetime = field(default_factory=lambda: datetime.now())
    entry_type: str = "MARKET"
    opportunity_quality: float = 0.0
    entry_quality: float = 0.0
    consensus_agreement: float = 0.0
    # ── Full-parity adjudication + sizing (non-legacy mode) ──────────────
    # Carried from the live decision path so the simulation manages and sizes
    # the trade with the SAME engines the live plane uses, not flat R-multiples.
    zone_type: str = ""
    is_counter_trend: bool = False
    bias_direction: str = ""
    de_size_mult: float = 1.0
    de_conviction: float = 0.0
    lots: float = 0.0
    max_loss: float = 0.0
    risk_amount: float = 0.0
    pip_value_per_lot: float = 10.0
    # Live OQ/EQ captured at entry (conf×10 / coherence×10 of the matching
    # WorldModel candidate) so management can compute oq_decay/eq_decay against
    # the same baseline the live plane stores on the position record.
    entry_oq: Optional[float] = None
    entry_eq: Optional[float] = None
    # Entry-source attribution (live parity). "zone" when the entry fired from
    # a zone touch (TickEntryDetector zone-touch fill), "consensus" when the
    # thesis triggered without a zone touch this bar. Carried into the trade
    # journal so post-run analysis can compare zone vs consensus entries.
    source: str = ""


@dataclass
class _ScanView:
    """Minimal scan-result view derived from a WorldModel.

    Shaped for the backtest decision path (``status``/``direction``/``score``/
    ``regime``/``bias_strength``) and for ``EntryEngine.calculate_entry`` (which
    reads only ``confluences`` and ``score``).  This lets the backtest decide
    through the shared ED decision core (``brain.decision_core.analyze_window``)
    instead of the legacy ``PairScanner`` — one decision implementation for both
    live and backtest.
    """
    status: str
    direction: str
    score: int
    regime: str
    bias_strength: str
    confluences: list[str] = field(default_factory=list)
    opportunity_quality: float = 0.0
    entry_quality: float = 0.0
    consensus_agreement: float = 0.0


def _world_model_to_scan_view(wm) -> "_ScanView":
    """Adapt a WorldModel into the scan-view the backtest decision path reads."""
    bias = wm.bias_dict()
    direction = str(bias.get("direction", "") or "")
    strength = str(bias.get("strength", "NONE") or "NONE")
    tradeable = bool(bias.get("tradeable", False))
    status = "READY" if (tradeable and direction in ("LONG", "SHORT")) else "WAITING"
    score = int(bias.get("score", 0) or 0)

    regimes = wm.regime_by_tf()
    regime = regimes.get("H1") or next(iter(regimes.values()), "UNKNOWN")

    confluences: list[str] = []
    agree = 0
    total = 0
    for v in wm.votes_list():
        vdir = str(getattr(v, "direction", "") or "")
        if vdir in ("LONG", "SHORT"):
            total += 1
            if vdir == direction:
                module = str(getattr(v, "module", "") or "")
                if module:
                    confluences.append(module)
                agree += 1
    consensus_agreement = round(agree / total, 4) if total else 0.0

    opportunity_quality = 0.0
    cands = wm.candidates_list()
    if cands:
        opportunity_quality = round(float(getattr(cands[0], "confidence", 0.0) or 0.0), 4)

    return _ScanView(
        status=status,
        direction=direction,
        score=score,
        regime=regime,
        bias_strength=strength,
        confluences=confluences,
        opportunity_quality=opportunity_quality,
        consensus_agreement=consensus_agreement,
    )


# Phase K (Part XI): these structure / M1 micro readers were byte-mirrored
# copies of event_driven_bootstrap's. Both planes now share the single source
# in brain.structure_context, so the backtest reads the WorldModel structure
# layer exactly as the live plane does.
from brain.structure_context import (  # noqa: E402
    _micro_confirmation_from_event,
    _struct_event,
    _struct_swings,
    _struct_trend_conf,
)


def _oq_eq_from_wm(wm, direction: str) -> tuple[Optional[float], Optional[float]]:
    """Live OQ/EQ for ``direction`` from a WorldModel's shared quality layer.

    Mirrors the live management read in
    ``event_driven_bootstrap._run_decision_engine_management``: both planes read
    the WorldModel's ``opportunity_quality`` (direction-free) and the
    direction-aware ``entry_quality_long`` / ``entry_quality_short``, computed by
    the single shared ``brain.quality_layer.compute_quality_layer``.  Returns
    ``(None, None)`` when the layer was not computed this cycle — exactly the
    live "not recomputed" semantics that apply no quality pressure.
    """
    from brain.quality_layer import entry_quality_for

    oq = getattr(wm, "opportunity_quality", None)
    if oq is not None:
        oq = max(0.0, min(10.0, float(oq)))
    eq = entry_quality_for(wm, direction)
    if eq is not None:
        eq = max(0.0, min(10.0, float(eq)))
    return oq, eq


def _h1_candle_context(h1_df, is_long: bool) -> dict:
    """Last-closed H1 candle bearish/doji flags for structure integrity.

    Mirrors the live ``_management_micro_context`` H1 read. The replay slice
    holds only closed candles, so the last row is the last closed H1 candle.
    Returns the same safe defaults the ``TradeContext`` carries when the feed
    is unavailable.
    """
    out = {"h1_last_candle_bearish": None, "h1_last_candle_doji": False}
    if h1_df is None or len(h1_df) < 1:
        return out
    try:
        last = h1_df.iloc[-1]
        o = float(last["open"])
        c = float(last["close"])
        rng = float(last["high"]) - float(last["low"])
        body = abs(c - o)
        out["h1_last_candle_bearish"] = c < o
        out["h1_last_candle_doji"] = rng > 0 and (body / rng) < 0.1
    except Exception:
        return out
    return out


class DataLoader:
    """
    Loads historical OHLCV files from CSV and normalizes expected columns.
    """

    REQUIRED_COLUMNS = {"time", "open", "high", "low", "close"}

    def load_csv(self, file_path: str) -> pd.DataFrame:
        df = pd.read_csv(file_path)
        missing = self.REQUIRED_COLUMNS.difference(df.columns)
        if missing:
            raise ValueError(
                f"Missing required columns in {file_path}: {sorted(missing)}"
            )

        out = df.copy()
        out["time"] = pd.to_datetime(out["time"], utc=True)
        out = out.sort_values("time").reset_index(drop=True)
        return out

    def load_timeframe_map(self, files: dict[str, str]) -> dict[str, pd.DataFrame]:
        data: dict[str, pd.DataFrame] = {}
        for timeframe, file_path in files.items():
            data[timeframe] = self.load_csv(file_path)
        return data


class BrokerDataLoader:
    """
    Fetches historical OHLCV bars directly from connected broker platforms.
    Falls back to CSV if broker is unavailable.
    """

    MT5_TF_MAP = {
        "M1": "TIMEFRAME_M1", "M5": "TIMEFRAME_M5", "M15": "TIMEFRAME_M15",
        "M30": "TIMEFRAME_M30", "H1": "TIMEFRAME_H1", "H4": "TIMEFRAME_H4",
        "D1": "TIMEFRAME_D1",
    }

    DERIV_GRANULARITY = {
        "M1": 60, "M5": 300, "M15": 900, "M30": 1800,
        "H1": 3600, "H4": 14400, "D1": 86400,
    }

    def fetch_mt5(
        self,
        symbol: str,
        timeframe: str,
        bars: int = 5000,
    ) -> pd.DataFrame:
        try:
            import MetaTrader5 as mt5

            login_env = os.getenv("MT5_LOGIN", "").strip()
            password = os.getenv("MT5_PASSWORD", "")
            server = os.getenv("MT5_SERVER", "")

            login = 0
            if login_env:
                try:
                    login = int(login_env)
                except ValueError:
                    logger.warning(f"Invalid MT5_LOGIN value: {login_env}")

            initialized = False
            if login and password and server:
                initialized = mt5.initialize(
                    login=login, password=password, server=server
                )
            if not initialized:
                initialized = mt5.initialize()
            if not initialized:
                raise RuntimeError(f"MT5 initialize failed: {mt5.last_error()}")

            if login and password and server:
                account = mt5.account_info()
                if account is None or account.login != login:
                    if not mt5.login(login, password=password, server=server):
                        raise RuntimeError(f"MT5 login failed: {mt5.last_error()}")

            tf_const = getattr(mt5, self.MT5_TF_MAP[timeframe], None)
            if tf_const is None:
                raise ValueError(f"Unknown timeframe: {timeframe}")

            rates = mt5.copy_rates_from_pos(symbol, tf_const, 0, bars)
            if rates is None or len(rates) == 0:
                raise RuntimeError(
                    f"MT5 returned no data for {symbol} {timeframe}: "
                    f"{mt5.last_error()}"
                )

            df = pd.DataFrame(rates)
            df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)
            df = df.rename(columns={"tick_volume": "volume"})
            df = df[["time", "open", "high", "low", "close", "volume"]]
            df = df.sort_values("time").reset_index(drop=True)
            logger.info(f"MT5: fetched {len(df)} bars for {symbol} {timeframe}")
            return df

        except ImportError:
            raise RuntimeError("MetaTrader5 package not installed. Run: pip install MetaTrader5")

    def fetch_deriv(
        self,
        symbol: str,
        timeframe: str,
        bars: int = 5000,
        api_token: str = "",
        app_id: str = "",
    ) -> pd.DataFrame:
        import json as _json
        import websockets

        from brain.symbol_mapper import SymbolMapper
        mapper = SymbolMapper("deriv")
        deriv_symbol = mapper.to_broker(symbol)

        granularity = self.DERIV_GRANULARITY.get(timeframe)
        if granularity is None:
            raise ValueError(f"Unsupported Deriv timeframe: {timeframe}")

        async def _fetch() -> list:
            url = f"wss://ws.binaryws.com/websockets/v3?app_id={app_id or '1089'}"
            async with websockets.connect(url) as ws:
                if api_token:
                    await ws.send(_json.dumps({"authorize": api_token}))
                    auth = _json.loads(await ws.recv())
                    if "error" in auth:
                        raise RuntimeError(f"Deriv auth failed: {auth['error']}")

                req = {
                    "ticks_history": deriv_symbol,
                    "end": "latest",
                    "count": bars,
                    "granularity": granularity,
                    "style": "candles",
                }
                await ws.send(_json.dumps(req))
                resp = _json.loads(await ws.recv())
                if "error" in resp:
                    raise RuntimeError(
                        f"Deriv error for {deriv_symbol}: {resp['error'].get('message')}"
                    )
                return resp.get("candles", [])

        candles = asyncio.run(_fetch())
        if not candles:
            raise RuntimeError(f"Deriv returned no candles for {deriv_symbol} {timeframe}")

        rows = [
            {
                "time": pd.Timestamp(c["epoch"], unit="s", tz="UTC"),
                "open": float(c["open"]),
                "high": float(c["high"]),
                "low": float(c["low"]),
                "close": float(c["close"]),
                "volume": 0.0,
            }
            for c in candles
        ]
        df = pd.DataFrame(rows).sort_values("time").reset_index(drop=True)
        logger.info(f"Deriv: fetched {len(df)} bars for {deriv_symbol} {timeframe}")
        return df

    def fetch_all_timeframes(
        self,
        symbol: str,
        timeframes: list[str],
        platform: str = "mt5",
        bars: int = 5000,
        **kwargs,
    ) -> dict[str, pd.DataFrame]:
        data: dict[str, pd.DataFrame] = {}
        for tf in timeframes:
            if platform == "mt5":
                data[tf] = self.fetch_mt5(symbol, tf, bars)
            elif platform == "deriv":
                data[tf] = self.fetch_deriv(symbol, tf, bars, **kwargs)
            else:
                raise ValueError(f"Unknown platform: {platform}")
        return data


class BacktestEngine:
    """
    Simulates setup generation and execution management over historical data.
    """

    def __init__(
        self,
        config=None,
        scanner=None,
        entry_engine=None,
        journal: Optional[TradeJournal] = None,
        starting_balance: float = 10_000.0,
        risk_per_trade: float = 0.02,
        min_history: int = 120,
        pip_size: float = 0.0001,
        slippage_pips: float = 1.0,
        commission_per_lot: float = 3.5,
        broker_loader: Optional[BrokerDataLoader] = None,
        legacy_mode: bool = False,
        pip_value_per_lot: float = 10.0,
        backtest_compression_enabled: bool = True,
        backtest_session_enabled: bool = True,
        backtest_stopout_flip_enabled: bool = True,
        backtest_prestaging_enabled: bool = True,
        backtest_dxy_enabled: bool = True,
        backtest_news_enabled: bool = True,
        backtest_flip_atr: bool = True,
        backtest_flip_tick: bool = False,
        backtest_flip_m15: bool = True,
        backtest_flip_volume: bool = True,
        backtest_flip_clean_break: bool = True,
        backtest_flip_sequence_enabled: bool = False,
    ):
        from config import AppConfig

        self.config = config or AppConfig()

        # CalibrationEngine (single writer) — opt-in via config.calibration.enabled.
        # When on, the backtest feeds the SAME candle slices it analyses into the
        # engine and registers it as the get_profile provider, so replayed
        # decisions use self-calibrating geometry exactly as live would. When off,
        # no provider is registered and the hardcoded constants are used.
        self._calibration_engine = None
        _calib_cfg = getattr(self.config, "calibration", None)
        if _calib_cfg is not None and getattr(_calib_cfg, "enabled", False):
            try:
                from brain.calibration_engine import CalibrationEngine
                from brain.instrument_profile import set_stats_provider
                self._calibration_engine = CalibrationEngine(
                    state_path=getattr(
                        _calib_cfg, "state_path", "data/calibration_state.json"
                    ),
                )
                set_stats_provider(self._calibration_engine)
            except Exception as exc:
                logger.warning("[backtest] CalibrationEngine init failed: {}", exc)
                self._calibration_engine = None

        # Decision engine: the shared ED core (brain.decision_core.analyze_window)
        # drives backtest decisions — identical to the live plane.  ``scanner``
        # is retained only as an optional injected override (default unused); the
        # legacy PairScanner is no longer constructed here.
        self.scanner = scanner

        if entry_engine is not None:
            self.entry_engine = entry_engine
        else:
            try:
                from trigger.entry_engine import EntryEngine
                risk_cfg = self.config.risk
                self.entry_engine = EntryEngine(
                    config=self.config,
                    volatility_stop_mode=risk_cfg.volatility_stop_mode,
                    atr_stop_period=risk_cfg.atr_stop_period,
                    atr_stop_mult=risk_cfg.atr_stop_mult,
                    atr_stop_ratio_min=risk_cfg.atr_stop_ratio_min,
                    atr_stop_ratio_max=risk_cfg.atr_stop_ratio_max,
                    atr_stop_max_risk_mult=risk_cfg.atr_stop_max_risk_mult,
                )
            except Exception as exc:
                logger.warning("[backtest] EntryEngine unavailable ({}), backtest disabled", exc)
                self.entry_engine = None

        self.session_engine = SessionEngine()
        # Cross-instrument volatility monitor — fed each bar from the H1 regime
        # analysis and folded into sizing as the ``vol_mult`` SizingFactors
        # component in ``_build_sizing_factors`` so the backtest mirrors the live
        # SystemVolatilityMonitor size gate.
        try:
            from brain.regime_detector import SystemVolatilityMonitor
            self.system_volatility_monitor = SystemVolatilityMonitor()
        except Exception:
            self.system_volatility_monitor = None
        self.journal = journal
        self.starting_balance = starting_balance
        self.risk_per_trade = risk_per_trade
        self.min_history = min_history
        self.pip_size = pip_size
        self.slippage_pips = slippage_pips
        self.commission_per_lot = commission_per_lot
        self.broker_loader = broker_loader or BrokerDataLoader()
        # ``legacy_mode`` restores the original geometry-only path (no live
        # adjudication, management or sizing) for side-by-side comparison.
        self.legacy_mode = bool(legacy_mode)
        self.pip_value_per_lot = float(pip_value_per_lot)

        # ── Daily realized-P&L tracking (live-parity portfolio sizing) ───
        # Reset per calendar day in ``run`` and accumulated on each close, so
        # PortfolioDivision sees the real remaining daily-loss budget instead of
        # a hardcoded 0.0 (which neutered the daily-loss throttle).
        self._bt_day = None
        self._bt_daily_pnl = 0.0

        # ── Live adjudication / management / sizing engines ──────────────
        # Constructed with the same defaults the live plane uses so the
        # backtest exercises identical decision logic.  Guarded so a missing
        # dependency degrades to legacy mode rather than crashing.
        self.situation_engine = None
        self.decision_engine = None
        self.risk_governor = None
        self.position_sizer = None
        self.portfolio = None
        self.entry_gate = None

        # ── Live stateful risk / compliance subsystems (Phase 1 full-parity) ──
        # Threaded into the non-legacy path so the backtest is governed by the
        # SAME gates the live plane enforces: the ComplianceDivision permit
        # layer, the PortfolioRisk state machine, the DrawdownGuard risk cap,
        # per-account risk silos and the stateful competing-thesis engine.
        # Each is best-effort (a construction/import fault leaves it None and
        # the corresponding gate is skipped) and constructed in
        # ``_init_live_subsystems`` — also re-run at the start of every ``run``
        # so consecutive backtests never leak state.
        self._bt_account_key = "backtest"
        self.compliance = None
        self.portfolio_risk_sm = None
        self.drawdown_guard = None
        self.account_risk = None
        self.thesis_engine = None

        # ── Live sizing / optimization subsystems (Phase 2 full-parity) ──
        # The graded-sizing chain the live ``_on_entry_decision`` runs between
        # the RiskGovernor verdict and ``PortfolioDivision.evaluate``: the
        # Orchestrator round table, the adaptive optimizer (losing-pattern /
        # AVOID vetoes + learned size), the opportunity-density and
        # execution-quality monitors, the capital allocator and the
        # opportunity-quality sizer. The system-wide volatility monitor already
        # lives on ``self.system_volatility_monitor``. Each is best-effort (a
        # construction/import fault leaves it None and its multiplier defaults
        # to 1.0) and (re)built in ``_init_live_subsystems``.
        self.bt_orchestrator = None
        self.bt_ml_adapter = None
        self.bt_opportunity_density = None
        self.bt_execution_monitor = None
        self.bt_capital_allocator = None
        self.bt_oq_sizer = None

        # ── Opportunistic-feature parity (Phase 3 Feature B) ─────────────
        # Mirror the live opportunistic modules in the replay so backtests
        # reflect live behaviour: compression detection, session-aware sizing,
        # stop-out flip, pre-staged limit fills, DXY correlation and news
        # pre-planning. Each is gated by its own flag so a feature can be
        # toggled independently, and (re)built in ``_init_live_subsystems``. DXY
        # and news degrade gracefully to a neutral 1.0 when the backtest data
        # carries no USD-strength / news-event source.
        self.backtest_compression_enabled = bool(backtest_compression_enabled)
        self.backtest_session_enabled = bool(backtest_session_enabled)
        self.backtest_stopout_flip_enabled = bool(backtest_stopout_flip_enabled)
        self.backtest_prestaging_enabled = bool(backtest_prestaging_enabled)
        self.backtest_dxy_enabled = bool(backtest_dxy_enabled)
        self.backtest_news_enabled = bool(backtest_news_enabled)
        # ── Hardened flip confirmation toggles (backtest parity) ─────────
        # Each of the five FlipConfirmer checks can be toggled independently so
        # the backtest can isolate the effect of any single hardening step. M5
        # non-opposition (the original backtest behaviour) is always on; these
        # gate the NEW checks: ATR-normalised magnitude, session-aware tick
        # efficiency, M15 non-opposition, volume confirmation and the
        # invalidation clean-break. Tick efficiency defaults OFF because the
        # replay has no sub-candle tick stream — the other four run on real bar
        # data and skip gracefully when a slice is unavailable.
        self.backtest_flip_atr = bool(backtest_flip_atr)
        self.backtest_flip_tick = bool(backtest_flip_tick)
        self.backtest_flip_m15 = bool(backtest_flip_m15)
        self.backtest_flip_volume = bool(backtest_flip_volume)
        self.backtest_flip_clean_break = bool(backtest_flip_clean_break)
        # ── Fast-then-slow flip sequencing (backtest parity, Session 16) ──
        # OFF by default: enabling it feeds a FlipSequenceTracker from the M1/M5
        # bar replay and requires the flip's fast-then-slow sequence to be
        # FULLY_CONFIRMED (flip Check 7). Off ⇒ the tracker is never fed and the
        # check skips, leaving flip behaviour identical to before.
        self.backtest_flip_sequence_enabled = bool(backtest_flip_sequence_enabled)
        self.compression_detector = None
        self.session_context = None
        # Optional injected data sources (default None → the feature no-ops).
        # ``backtest_usd_strength(now) -> float`` returns a signed USD-strength
        # trend (>0 strengthening) for the DXY penalty; ``backtest_news_events``
        # is a list of event datetimes (or objects with ``time_utc``) that the
        # news window sizing / breakout simulation reads.
        self.backtest_usd_strength: Optional[Callable[[Any], float]] = None
        self.backtest_news_events: list = []
        # Per-symbol stop-out-flip state (last flip monotonic time + per-zone
        # count) so the whipsaw guards mirror the live orchestrator.
        self._bt_flip_state: dict[str, dict[str, Any]] = {}
        # Fast-then-slow flip sequencing tracker (Session 16, flip Check 7) fed
        # from the bar replay when backtest_flip_sequence_enabled is set. Built
        # lazily so the import cost is only paid when the feature is on; the
        # per-symbol last-M5-bar timestamp drives once-per-M5-bar feeding.
        self._bt_flip_sequence_tracker: Optional[Any] = None
        self._bt_last_m5_time: dict[str, Any] = {}

        if not self.legacy_mode:
            try:
                self._build_live_engines()
            except Exception as exc:
                logger.warning(
                    "[backtest] live engines unavailable ({}); falling back to "
                    "legacy geometry mode", exc,
                )
                self.legacy_mode = True

    def _build_live_engines(self) -> None:
        """Construct the live adjudication/management/sizing engines.

        Mirrors the live plane's construction so the backtest runs the SAME
        SituationEngine → DecisionEngine → RiskGovernor adjudication and the
        SAME PortfolioDivision → PositionSizer sizing.
        """
        from decision.situation import SituationEngine
        from decision.engine import DecisionEngine
        from decision.governor import RiskGovernor
        from risk.position_sizer import PositionSizer
        from portfolio.division import PortfolioDivision
        from entry.entry_gate import EntryGate
        from entry.models import EntryConfig

        self.situation_engine = SituationEngine()
        self.decision_engine = DecisionEngine()
        self.risk_governor = RiskGovernor()
        self.position_sizer = PositionSizer()
        self.portfolio = PortfolioDivision(position_sizer=self.position_sizer)
        self.entry_gate = EntryGate(config=EntryConfig())

        # 1B — symbol-relative conviction. A NON-persistent store so the backtest
        # warms up its own per-symbol conviction distribution within the replay
        # (it must NOT read/write the live per-user state). Same normalization
        # code path as live form_thesis, so the two planes stay parity-preserving:
        # cold-start raw passthrough, then symbol-relative once each symbol warms.
        self.symbol_conviction = None
        try:
            cn_cfg = getattr(self.config, "conviction_normalization", None)
            if cn_cfg is None or bool(getattr(cn_cfg, "enabled", True)):
                from adaptive.symbol_conviction import SymbolConvictionStore
                self.symbol_conviction = SymbolConvictionStore(
                    enabled=bool(getattr(cn_cfg, "enabled", True)) if cn_cfg else True,
                    min_samples=int(getattr(cn_cfg, "min_samples", 30)) if cn_cfg else 30,
                    max_history=int(getattr(cn_cfg, "max_history", 300)) if cn_cfg else 300,
                    blend=float(getattr(cn_cfg, "blend", 0.5)) if cn_cfg else 0.5,
                    persist=False,
                )
        except Exception as exc:  # noqa: BLE001
            logger.debug("[backtest] SymbolConvictionStore unavailable: {}", exc)

        # Construct the live stateful risk / compliance subsystems too, so the
        # non-legacy replay is governed by the same gates the live plane uses.
        self._init_live_subsystems()

    def _init_live_subsystems(self) -> None:
        """Construct the live stateful risk / compliance / sizing subsystems.

        Mirrors ``SystemContext.create`` so the backtest is governed by the
        SAME permit + risk-state gates the live event-driven plane enforces:

          * :class:`ComplianceDivision` — the single authoritative permit layer
            (duplicate, daily-loss halt, per-account heat, DrawdownGuard FROZEN,
            max-positions, PortfolioRisk DEFENSIVE+). The broker/platform checks
            (market-open, broker-available, spread) are bound to always-pass
            stubs — the backtest replays historical candles, so those physical
            vetoes are trivially satisfied (as the pre-parity path assumed).
          * :class:`PortfolioRiskStateMachine` — DEFENSIVE/REDUCING/EMERGENCY
            heat management.
          * :class:`DrawdownGuard` — NORMAL/CAUTION/RECOVERY/FROZEN risk cap.
          * :class:`AccountRiskManager` — per-account daily-P&L / heat silo.
          * :class:`ThesisEngine` — stateful competing Long/Short/Flat theses.

        Phase 2 also builds the graded-sizing chain the live
        ``_on_entry_decision`` runs before ``PortfolioDivision.evaluate`` — the
        :class:`Orchestrator`, the :class:`AdaptiveOptimizer` (ml_adapter), the
        :class:`OpportunityDensityTracker`, the :class:`ExecutionMonitor`, the
        :class:`CapitalAllocator` and the :class:`OpportunityQualitySizer` — each
        folded into the ``SizingFactors`` bundle by ``_build_sizing_factors``.

        Every subsystem is best-effort: an import or construction fault leaves
        it ``None`` (logged) and its gate / multiplier is skipped (defaulting to
        a neutral 1.0), so the replay still runs in a degraded, live-parity-
        minus-one-gate mode. Re-invoked from ``run`` so consecutive backtests
        start with clean subsystem state.
        """
        risk_cfg = getattr(self.config, "risk", None)
        gcfg = getattr(self.config, "governor", None)

        # ── DrawdownGuard ────────────────────────────────────────────
        self.drawdown_guard = None
        try:
            from brain.drawdown_guard import DrawdownGuard
            self.drawdown_guard = DrawdownGuard(
                # Anchor NORMAL-mode risk to the backtest's own base risk so the
                # per-mode cap only ever REDUCES risk during drawdown (never
                # below the intended base in NORMAL), matching the guard's
                # "scales DOWN from base" contract.
                base_risk_pct=float(self.risk_per_trade),
                rolling_window_days=int(
                    getattr(risk_cfg, "drawdown_rolling_window_days", 30)
                    if risk_cfg is not None else 30
                ),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("[backtest] DrawdownGuard unavailable: {}", exc)
            self.drawdown_guard = None

        # ── PortfolioRiskStateMachine ────────────────────────────────
        self.portfolio_risk_sm = None
        try:
            if risk_cfg is None or getattr(risk_cfg, "portfolio_risk_engine_enabled", True):
                from risk.portfolio_risk_state import PortfolioRiskStateMachine
                self.portfolio_risk_sm = PortfolioRiskStateMachine(
                    heat_defensive_pct=getattr(risk_cfg, "heat_defensive_pct", 1.5) if risk_cfg else 1.5,
                    heat_recovery_pct=getattr(risk_cfg, "heat_recovery_pct", 1.0) if risk_cfg else 1.0,
                    recovery_dwell_seconds=getattr(risk_cfg, "recovery_dwell_seconds", 120.0) if risk_cfg else 120.0,
                    heat_reduction_pct=getattr(risk_cfg, "heat_reduction_pct", 2.5) if risk_cfg else 2.5,
                    reduction_persist_seconds=getattr(risk_cfg, "reduction_persist_seconds", 300.0) if risk_cfg else 300.0,
                    heat_emergency_pct=getattr(risk_cfg, "heat_emergency_pct", 4.0) if risk_cfg else 4.0,
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning("[backtest] PortfolioRiskStateMachine unavailable: {}", exc)
            self.portfolio_risk_sm = None

        # ── AccountRiskManager ───────────────────────────────────────
        self.account_risk = None
        try:
            from risk.account_risk import AccountRiskManager
            self.account_risk = AccountRiskManager(
                daily_loss_cap_pct=getattr(gcfg, "daily_loss_cap_pct", 3.0) if gcfg else 3.0,
                daily_loss_recovery_pct=getattr(gcfg, "daily_loss_recovery_pct", 1.5) if gcfg else 1.5,
                heat_block_pct=getattr(risk_cfg, "portfolio_heat_block_pct", 2.0) if risk_cfg else 2.0,
                daily_loss_flatten_pct=getattr(risk_cfg, "daily_loss_flatten_pct", 5.0) if risk_cfg else 5.0,
            )
            self.account_risk.update_balance(self._bt_account_key, float(self.starting_balance))
        except Exception as exc:  # noqa: BLE001
            logger.warning("[backtest] AccountRiskManager unavailable: {}", exc)
            self.account_risk = None

        # ── ComplianceDivision ───────────────────────────────────────
        self.compliance = None
        try:
            from compliance.division import ComplianceDivision
            max_pos = int(getattr(gcfg, "max_open_positions", 8)) if gcfg else 8
            self.compliance = ComplianceDivision(
                drawdown_guard=self.drawdown_guard,
                portfolio_risk_sm=self.portfolio_risk_sm,
                account_risk=self.account_risk,
                news_guard=None,
                max_open_positions=max_pos,
            )
            # Bind always-pass broker/platform checks: a candle replay always
            # has an open market, a live broker and a modelled spread, so those
            # physically-necessary vetoes (which fail CLOSED when unbound) are
            # satisfied here — leaving Compliance's STATEFUL gates as the ones
            # that actually bind in the backtest.
            self.compliance.bind_runtime(
                is_market_open=lambda _sym: True,
                is_broker_available=lambda _sym: True,
                get_spread_pips=lambda _sym: 0.0,
                spread_monitor=_BacktestSpreadMonitor(),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("[backtest] ComplianceDivision unavailable: {}", exc)
            self.compliance = None

        # ── ThesisEngine — RETIRED (Single Reasoner cutover, Part III.2) ──
        # The legacy competing-thesis decider is deleted; the backtest thesis
        # feed/gate are None-guarded and no-op. `self.thesis_engine` stays None.
        self.thesis_engine = None

        # ── Orchestrator round table (graded size / physics veto) ────
        self.bt_orchestrator = None
        try:
            from brain.orchestrator import Orchestrator
            self.bt_orchestrator = Orchestrator(
                config=getattr(self.config, "orchestrator", None),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("[backtest] Orchestrator unavailable: {}", exc)
            self.bt_orchestrator = None

        # ── AdaptiveOptimizer (ml_adapter) ───────────────────────────
        # Learned pair/regime/session edge + losing-pattern / AVOID vetoes,
        # constructed exactly as ``SystemContext`` does so the replay reads the
        # SAME learned edge the live plane sizes on. Read-only in the replay:
        # ``get_trade_adjustments`` / ``is_losing_pattern`` are pure reads and
        # ``register_new_trade`` only advances an in-memory counter — the live
        # learned stores are never written. May be effectively cold (no learned
        # history) — that is fine, every read then returns a neutral 1.0 /
        # "trade" verdict. Best-effort → None on any construction fault.
        self.bt_ml_adapter = None
        try:
            from adaptive.optimizer import AdaptiveOptimizer
            self.bt_ml_adapter = AdaptiveOptimizer(config=self.config)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[backtest] AdaptiveOptimizer unavailable: {}", exc)
            self.bt_ml_adapter = None

        # ── OpportunityDensityTracker ────────────────────────────────
        self.bt_opportunity_density = None
        try:
            from brain.opportunity_density import OpportunityDensityTracker
            self.bt_opportunity_density = OpportunityDensityTracker(window_minutes=60)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[backtest] OpportunityDensityTracker unavailable: {}", exc)
            self.bt_opportunity_density = None

        # ── ExecutionMonitor (execution-quality sizing) ──────────────
        self.bt_execution_monitor = None
        try:
            from brain.execution_monitor import ExecutionMonitor
            self.bt_execution_monitor = ExecutionMonitor()
        except Exception as exc:  # noqa: BLE001
            logger.warning("[backtest] ExecutionMonitor unavailable: {}", exc)
            self.bt_execution_monitor = None

        # ── CapitalAllocator ─────────────────────────────────────────
        # SQLite-backed. Isolated to an in-memory DB so the replay warms up its
        # own capital-allocation split (Phase-1 ``persist=False`` philosophy)
        # and never reads/writes the live allocation store; cold → a neutral
        # 1.0 multiplier. The prior instance's connection is closed on reset.
        try:
            prev_alloc = getattr(self, "bt_capital_allocator", None)
            if prev_alloc is not None and hasattr(prev_alloc, "close"):
                prev_alloc.close()
        except Exception:  # noqa: BLE001
            pass
        self.bt_capital_allocator = None
        try:
            from pathlib import Path
            from adaptive.capital_allocator import CapitalAllocator
            self.bt_capital_allocator = CapitalAllocator(db_path=Path(":memory:"))
        except Exception as exc:  # noqa: BLE001
            logger.warning("[backtest] CapitalAllocator unavailable: {}", exc)
            self.bt_capital_allocator = None

        # ── OpportunityQualitySizer (opportunity-proportional sizing) ─
        self.bt_oq_sizer = None
        try:
            from brain.opportunity_sizer import OpportunityQualitySizer
            ci_cfg = getattr(self.config, "cross_instrument", None)
            self.bt_oq_sizer = OpportunityQualitySizer(
                enabled=bool(
                    getattr(ci_cfg, "quality_sizing_enabled", False) if ci_cfg else False
                ),
                max_boost=float(
                    getattr(ci_cfg, "quality_sizing_max_boost", 1.3) if ci_cfg else 1.3
                ),
                min_cut=float(
                    getattr(ci_cfg, "quality_sizing_min_cut", 0.7) if ci_cfg else 0.7
                ),
                ev_ref=float(
                    getattr(ci_cfg, "quality_sizing_ev_ref", 1.0) if ci_cfg else 1.0
                ),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("[backtest] OpportunityQualitySizer unavailable: {}", exc)
            self.bt_oq_sizer = None

        # ── Opportunistic modules (Phase 3 Feature B — compression + session) ─
        # The SAME global classifiers the live entry plane reads: the
        # CompressionDetector (fed M5/M15 each bar in ``run``) and the stateless
        # SessionContext. Gated by their flags so each can be toggled off, and
        # reset here so consecutive backtests never leak compression memory.
        self.compression_detector = None
        if self.backtest_compression_enabled:
            try:
                from brain.compression_detector import CompressionDetector
                from entry.models import EntryConfig
                self.compression_detector = CompressionDetector(entry_config=EntryConfig())
            except Exception as exc:  # noqa: BLE001
                logger.warning("[backtest] CompressionDetector unavailable: {}", exc)
                self.compression_detector = None

        self.session_context = None
        if self.backtest_session_enabled:
            try:
                from brain.session_context import SessionContext
                from entry.models import EntryConfig
                self.session_context = SessionContext(entry_config=EntryConfig())
            except Exception as exc:  # noqa: BLE001
                logger.warning("[backtest] SessionContext unavailable: {}", exc)
                self.session_context = None

        logger.info(
            "[backtest] live subsystems — compliance={} portfolio_sm={} "
            "drawdown={} account_risk={} thesis={}",
            self.compliance is not None,
            self.portfolio_risk_sm is not None,
            self.drawdown_guard is not None,
            self.account_risk is not None,
            self.thesis_engine is not None,
        )
        logger.info(
            "[backtest] sizing subsystems — orchestrator={} ml_adapter={} "
            "density={} exec_monitor={} capital_allocator={} oq_sizer={}",
            self.bt_orchestrator is not None,
            self.bt_ml_adapter is not None,
            self.bt_opportunity_density is not None,
            self.bt_execution_monitor is not None,
            self.bt_capital_allocator is not None,
            self.bt_oq_sizer is not None,
        )

    def _require_decision_engine(self) -> None:
        """Raise loudly if the live decision engine is unavailable."""
        if self.entry_engine is None:
            raise RuntimeError(
                "Backtest decision engine unavailable (EntryEngine failed to "
                "construct); cannot run a representative backtest"
            )

    def run(
        self,
        pair: str,
        data_by_timeframe: dict[str, pd.DataFrame],
        start_index: Optional[int] = None,
        end_index: Optional[int] = None,
        compare_atr_stop: bool = False,
        atr_stop_period: int = 14,
        atr_stop_mult: float = 1.5,
        atr_stop_ratio_min: float = 0.5,
        atr_stop_ratio_max: float = 2.0,
        atr_stop_max_risk_mult: float = 4.0,
    ) -> BacktestResult:
        self._require_decision_engine()
        if "M1" not in data_by_timeframe:
            raise ValueError("M1 timeframe is required for replay execution")

        m1 = data_by_timeframe["M1"].sort_values("time").reset_index(drop=True)
        start_index = start_index if start_index is not None else self.min_history
        end_index = end_index if end_index is not None else len(m1) - 1

        balance = self.starting_balance
        equity_curve = [balance]
        trade_returns_r: list[float] = []
        trade_returns_gross: list[float] = []
        hold_times: list[float] = []
        sessions: dict[str, list[float]] = {}
        open_trade: Optional[dict] = None
        consecutive_losses = 0
        max_consecutive_losses = 0
        total_commission = 0.0
        total_slippage_cost = 0.0

        # Reset daily realized-P&L tracking for this run (fed into sizing).
        self._bt_day = None
        self._bt_daily_pnl = 0.0

        # Reset / build the fast-then-slow flip sequence tracker for this run so
        # consecutive backtests never leak confirmation state (Session 16).
        self._bt_last_m5_time = {}
        if self.backtest_flip_sequence_enabled:
            try:
                from entry.flip_sequence_tracker import FlipSequenceTracker
                if self._bt_flip_sequence_tracker is None:
                    self._bt_flip_sequence_tracker = FlipSequenceTracker(
                        pip_size_lookup=lambda _s: float(self.pip_size or 0.0001),
                    )
                else:
                    self._bt_flip_sequence_tracker.reset_all()
            except Exception as exc:  # noqa: BLE001
                logger.warning("[backtest] flip sequence tracker unavailable: {}", exc)
                self._bt_flip_sequence_tracker = None

        # Reset the stateful risk / compliance subsystems so consecutive
        # backtests never leak state (heat, daily-loss halts, drawdown mode,
        # standing theses). Untouched in legacy mode. Re-seeds the per-account
        # balance from the run's starting balance.
        if not self.legacy_mode:
            try:
                self._init_live_subsystems()
            except Exception as exc:  # noqa: BLE001
                logger.warning("[backtest] live-subsystem reset failed: {}", exc)

        atr_compared = 0
        atr_skipped = 0
        struct_returns: list[float] = []
        struct_outcomes: list[str] = []
        atr_returns: list[float] = []
        atr_outcomes: list[str] = []
        atr_open_trade: Optional[dict] = None

        if compare_atr_stop:
            from brain.instrument_profile import get_profile
            from brain.volatility_stop import (
                clamped_atr_stop_distance,
                latest_atr,
                atr_stop_price,
            )
            inst_profile = get_profile(pair)

        for i in range(start_index, end_index + 1):
            candle = m1.iloc[i]
            now = pd.Timestamp(candle["time"]).to_pydatetime()

            # Roll the daily realized-P&L budget at each calendar-day boundary.
            day = now.date()
            if self._bt_day != day:
                self._bt_day = day
                self._bt_daily_pnl = 0.0

            slices = self._build_slices(data_by_timeframe, now)
            if not slices:
                continue

            # Feed the opportunistic classifiers (compression detector on the
            # M5/M15 slices; the session context is stateless) so the entry
            # sizing path can read this bar's market-state / session — live
            # parity for the opportunistic modules (Phase 3 Feature B).
            self._update_opportunistic_state(pair, slices)

            # Feed the fast-then-slow flip sequence tracker from the bar replay
            # (Session 16, flip Check 7) so a stop-out flip can require the M1→M5
            # temporal ordering exactly as the live plane does. No-op unless the
            # feature is enabled.
            self._feed_flip_sequence(pair, slices, now)

            # Feed the cross-instrument volatility monitor from this bar's H1
            # regime analysis (single-symbol replay → one analysis per bar; the
            # monitor's pct thresholds collapse to "this pair spiking or not").
            self._update_system_volatility(slices)

            # Evaluate the PortfolioRisk state machine from the live open-trade
            # heat every bar (single-position serial book → heat is the open
            # trade's capital-at-risk, 0 when flat). This drives the Compliance
            # portfolio-risk / heat gates on entries and the EMERGENCY
            # force-close / DEFENSIVE breakeven responses on open trades.
            if not self.legacy_mode:
                self._update_portfolio_risk_state(open_trade, balance, now)

            if open_trade is None:
                setup = self._decide_setup(pair, slices, now, balance)
                if setup:
                    open_trade = self._open_trade(setup, now, entry_candle=candle)
                    open_trade["symbol"] = pair

                    if compare_atr_stop and open_trade is not None:
                        m1_slice = slices.get("M1")
                        atr_val = latest_atr(m1_slice, atr_stop_period) if m1_slice is not None else None
                        structure_dist = abs(open_trade["entry_price"] - open_trade["stop_loss"])
                        max_pips = atr_stop_max_risk_mult * inst_profile.min_risk_pips
                        dist, status = clamped_atr_stop_distance(
                            atr_val,
                            structure_dist,
                            mult=atr_stop_mult,
                            min_pips=inst_profile.min_risk_pips,
                            max_pips=max_pips,
                            pip_size=self.pip_size,
                            ratio_min=atr_stop_ratio_min,
                            ratio_max=atr_stop_ratio_max,
                        )
                        if status == "modeled" and dist is not None:
                            direction = open_trade["setup"].direction
                            atr_sl = atr_stop_price(open_trade["entry_price"], direction, dist)
                            atr_risk = dist
                            if atr_risk <= 0:
                                atr_risk = 8 * self.pip_size
                            # Only override stop and risk — TP prices stay identical
                            # to the structure trade (deep-copied from open_trade) so
                            # the comparison isolates the effect of stop distance alone.
                            atr_open_trade = copy.deepcopy(open_trade)
                            atr_open_trade["stop_loss"] = atr_sl
                            atr_open_trade["risk"] = atr_risk
                            atr_compared += 1
                        else:
                            atr_open_trade = None
                            atr_skipped += 1
                continue

            close_event = self._evaluate_trade(
                open_trade, candle,
                slices=slices, pair=pair, now=now, manage=True,
            )

            # PortfolioRisk EMERGENCY → force-close the open trade (live parity
            # with ``_check_portfolio_heat`` flattening on extreme heat / a
            # FROZEN DrawdownGuard). Only when geometry/management left it open.
            if close_event is None and not self.legacy_mode:
                close_event = self._maybe_emergency_close(open_trade, candle)

            if compare_atr_stop and atr_open_trade is not None:
                atr_close = self._evaluate_trade(atr_open_trade, candle, manage=False)
                if atr_close is not None:
                    atr_returns.append(atr_close["pnl_r"])
                    atr_outcomes.append(atr_close["outcome"])
                    atr_open_trade = None

            if close_event is None:
                continue

            if compare_atr_stop:
                struct_returns.append(close_event["pnl_r"])
                struct_outcomes.append(close_event["outcome"])
                if atr_open_trade is not None:
                    # ATR counterfactual force-closed at the structure trade's
                    # exit candle (single-position serial model). This dampens
                    # divergence — a wider ATR stop that would have run longer
                    # is truncated here. Known limitation.
                    atr_forced = self._force_close(atr_open_trade, candle)
                    atr_returns.append(atr_forced["pnl_r"])
                    atr_outcomes.append(atr_forced["outcome"])
                    atr_open_trade = None

            pnl_r = close_event["pnl_r"]
            trade_returns_gross.append(pnl_r)
            _bal_before = balance
            balance, commission = self._apply_pnl(balance, pnl_r, open_trade)
            # Accumulate realized $ P&L into the day's budget (net of costs) so
            # PortfolioDivision sees the real remaining daily-loss allowance.
            self._bt_daily_pnl += (balance - _bal_before)
            # Feed the realized close into the live risk silos (live parity with
            # the close-path in ``event_driven_bootstrap``) so drawdown mode and
            # the per-account daily-loss halt update across the run.
            if not self.legacy_mode:
                self._register_close_risk(balance - _bal_before, balance, now)
                self._register_close_adaptive(close_event)
            total_commission += commission
            total_slippage_cost += open_trade.get("slippage_cost", 0.0)
            equity_curve.append(balance)
            trade_returns_r.append(pnl_r)
            hold_times.append(close_event["hold_minutes"])

            session = open_trade["session"]
            sessions.setdefault(session, []).append(pnl_r)

            if pnl_r <= 0:
                consecutive_losses += 1
                max_consecutive_losses = max(max_consecutive_losses, consecutive_losses)
            else:
                consecutive_losses = 0

            self._journal_trade(pair, open_trade, close_event, now)

            # Stop-out flip (Phase 3 Feature B): when the trade was stopped out
            # (SL hit), evaluate a flip into the opposite direction and open it
            # on the SAME bar — live parity with the orchestrator's
            # evaluate_stopout_flip call from _on_trade_closed. Gated by flag /
            # non-legacy; returns None when unconfirmed or guards block it.
            flip_trade = None
            if (
                not self.legacy_mode
                and self.backtest_stopout_flip_enabled
                and close_event.get("outcome") == "LOSS"
            ):
                flip_trade = self._maybe_stopout_flip(
                    open_trade, close_event, slices, pair, now, balance,
                )
            open_trade = flip_trade

        if open_trade:
            final_candle = m1.iloc[end_index]
            forced_close = self._force_close(open_trade, final_candle)

            if compare_atr_stop:
                struct_returns.append(forced_close["pnl_r"])
                struct_outcomes.append(forced_close["outcome"])
                if atr_open_trade is not None:
                    atr_forced = self._force_close(atr_open_trade, final_candle)
                    atr_returns.append(atr_forced["pnl_r"])
                    atr_outcomes.append(atr_forced["outcome"])
                    atr_open_trade = None

            pnl_r = forced_close["pnl_r"]
            balance, _commission = self._apply_pnl(balance, pnl_r, open_trade)
            equity_curve.append(balance)
            trade_returns_r.append(pnl_r)
            hold_times.append(forced_close["hold_minutes"])
            sessions.setdefault(open_trade["session"], []).append(pnl_r)
            self._journal_trade(
                pair,
                open_trade,
                forced_close,
                pd.Timestamp(final_candle["time"]).to_pydatetime(),
            )

        wins = sum(1 for r in trade_returns_r if r > 0)
        losses = sum(1 for r in trade_returns_r if r <= 0)
        total = len(trade_returns_r)
        win_rate = (wins / total * 100.0) if total else 0.0
        profit_factor = self._profit_factor(trade_returns_r)
        gross_pf = self._profit_factor(trade_returns_gross)
        sharpe = self._sharpe_ratio(trade_returns_r)
        max_dd = self._max_drawdown_pct(equity_curve)
        expectancy = float(np.mean(trade_returns_r)) if trade_returns_r else 0.0
        avg_hold = float(np.mean(hold_times)) if hold_times else 0.0
        best_session = self._best_session(sessions)

        atr_cmp: Optional[ATRComparisonResult] = None
        if compare_atr_stop:
            atr_cmp = self._build_atr_comparison(
                atr_compared, atr_skipped,
                struct_returns, struct_outcomes,
                atr_returns, atr_outcomes,
            )

        return BacktestResult(
            total_trades=total,
            wins=wins,
            losses=losses,
            win_rate=round(win_rate, 2),
            profit_factor=round(profit_factor, 4)
            if np.isfinite(profit_factor)
            else float("inf"),
            sharpe_ratio=round(sharpe, 4),
            max_drawdown=round(max_dd, 4),
            max_consecutive_losses=max_consecutive_losses,
            expectancy=round(expectancy, 4),
            avg_hold_time=round(avg_hold, 2),
            best_pair=pair if total else None,
            worst_pair=pair if total else None,
            best_session=best_session,
            equity_curve=[round(v, 4) for v in equity_curve],
            total_commission=round(total_commission, 2),
            total_slippage_cost=round(total_slippage_cost, 6),
            gross_profit_factor=round(gross_pf, 4) if np.isfinite(gross_pf) else float("inf"),
            net_profit_factor=round(profit_factor, 4) if np.isfinite(profit_factor) else float("inf"),
            atr_comparison=atr_cmp,
        )

    def walk_forward(
        self,
        pair: str,
        data_by_timeframe: dict[str, pd.DataFrame],
        n_folds: int = 5,
        train_ratio: float = 0.7,
    ) -> dict:
        """Anchored/expanding-window walk-forward analysis.

        For *n_folds* >= 2 the M1 data from ``self.min_history`` to the
        last bar is divided into *n_folds* contiguous segments of roughly
        equal length.  For fold *k* (0-based):

        * **Train** — an *expanding* (anchored) window from
          ``self.min_history`` up to the end of segment *k*.
        * **Test** — segment *k + 1* (the next contiguous, non-
          overlapping out-of-sample block).

        This produces ``n_folds - 1`` train/test pairs because the last
        segment is always consumed as a test window (no fold uses it as
        training only).

        When *n_folds* == 1, the method falls back to the legacy single
        70/30 hold-out split controlled by *train_ratio*, preserving
        backward compatibility with the original implementation.

        Returns
        -------
        dict
            ``{"folds": [{"fold": int, "train": BacktestResult,
            "test": BacktestResult}, ...], "aggregate": BacktestResult}``

            *aggregate* is the result of running the engine across the
            **union of all OOS test segments** (concatenated, non-
            overlapping).  When *n_folds* == 1, *folds* contains one
            entry and *aggregate* equals the single test result.
        """
        self._require_decision_engine()
        m1 = data_by_timeframe["M1"].sort_values("time").reset_index(drop=True)
        total_bars = len(m1)
        usable_start = self.min_history

        if n_folds < 1:
            raise ValueError("n_folds must be >= 1")

        if n_folds == 1:
            split = int(total_bars * train_ratio)
            train_result = self.run(
                pair,
                data_by_timeframe,
                start_index=usable_start,
                end_index=max(split - 1, usable_start),
            )
            test_result = self.run(
                pair,
                data_by_timeframe,
                start_index=max(split, usable_start),
                end_index=total_bars - 1,
            )
            return {
                "folds": [{"fold": 0, "train": train_result, "test": test_result}],
                "aggregate": test_result,
            }

        usable_length = total_bars - usable_start
        if usable_length < n_folds:
            raise ValueError(
                f"Not enough bars ({usable_length} usable) for {n_folds} folds"
            )

        segment_size = usable_length // n_folds
        boundaries: list[int] = []
        for k in range(n_folds):
            boundaries.append(usable_start + k * segment_size)
        boundaries.append(total_bars)

        folds: list[dict] = []
        oos_ranges: list[tuple[int, int]] = []

        for k in range(n_folds - 1):
            train_start = usable_start
            train_end = boundaries[k + 1] - 1
            test_start = boundaries[k + 1]
            test_end = boundaries[k + 2] - 1

            train_result = self.run(
                pair, data_by_timeframe,
                start_index=train_start, end_index=train_end,
            )
            test_result = self.run(
                pair, data_by_timeframe,
                start_index=test_start, end_index=test_end,
            )
            folds.append({
                "fold": k,
                "train": train_result,
                "test": test_result,
            })
            oos_ranges.append((test_start, test_end))

        aggregate = self.run(
            pair, data_by_timeframe,
            start_index=oos_ranges[0][0],
            end_index=oos_ranges[-1][1],
        )

        return {"folds": folds, "aggregate": aggregate}

    def monte_carlo(
        self, trade_returns_r: list[float], iterations: int = 500
    ) -> dict[str, float]:
        if not trade_returns_r:
            return {
                "iterations": float(iterations),
                "worst_case_drawdown": 0.0,
                "median_total_r": 0.0,
                "p10_total_r": 0.0,
            }

        drawdowns = []
        totals = []
        for _ in range(iterations):
            sample = trade_returns_r.copy()
            random.shuffle(sample)
            equity = np.cumsum(sample)
            peaks = np.maximum.accumulate(equity)
            dd = float(np.max(peaks - equity)) if len(equity) else 0.0
            drawdowns.append(dd)
            totals.append(float(np.sum(sample)))

        return {
            "iterations": float(iterations),
            "worst_case_drawdown": round(max(drawdowns), 4),
            "median_total_r": round(float(np.median(totals)), 4),
            "p10_total_r": round(float(np.percentile(totals, 10)), 4),
        }

    def _build_slices(
        self,
        data_by_timeframe: dict[str, pd.DataFrame],
        now: datetime,
    ) -> Optional[dict[str, pd.DataFrame]]:
        required = ("H4", "H1", "M15", "M5", "M1")
        slices: dict[str, pd.DataFrame] = {}
        for tf in required:
            if tf not in data_by_timeframe:
                return None
            df = data_by_timeframe[tf]
            view = df[df["time"] <= now].copy()
            if len(view) < 25:
                return None
            slices[tf] = view.reset_index(drop=True)
        return slices

    def _decide_setup(
        self,
        pair: str,
        slices: dict[str, pd.DataFrame],
        now: datetime,
        balance: float,
    ) -> Optional[BacktestSetup]:
        """Run the shared ED decision core, then adjudicate the entry.

        Non-legacy: mirrors the live entry path — ``form_thesis`` trigger →
        zone-geometry score → ``EntryGate`` (score≥85) → ``SituationEngine`` /
        ``DecisionEngine`` adjudication → ``RiskGovernor`` → portfolio sizing.
        Legacy: the original scan-view + EntryEngine path (geometry only).
        """
        if self.entry_engine is None:
            return None

        h4 = slices.get("H4")
        h1 = slices.get("H1")
        m15 = slices.get("M15")
        m5 = slices.get("M5")
        m1 = slices.get("M1")

        if h4 is None or h1 is None or m15 is None or m5 is None or m1 is None:
            return None

        try:
            from brain.decision_core import analyze_window
            # CalibrationEngine feed — hand the analysed slices to the single
            # writer so per-symbol ATR / session / spread stats calibrate during
            # replay (no-op when calibration is disabled). Fed once per bar here
            # on the analysis path; the management path reuses the same stats.
            if self._calibration_engine is not None:
                pip = getattr(self, "pip_size", 0.0001) or 0.0001
                for _tf, _df in (("H4", h4), ("H1", h1), ("M15", m15), ("M5", m5)):
                    self._calibration_engine.update_candles(pair, _tf, _df, pip)
                try:
                    spread = float(getattr(self.config.backtest, "default_spread_pips", 0.0) or 0.0)
                    if spread > 0:
                        self._calibration_engine.update_spread(pair, spread, pip)
                except Exception:
                    pass
            # ── Developing-analysis simulation (live-parity — Phase 2) ──────
            # Mirror the live DevelopingAnalysisLoop: build the still-forming
            # HTF candle from M1 bars since the last HTF close and derive its
            # structure, then feed it to analyze_window → compute_bias as ×0.70
            # discounted evidence (same blend the confirmed live path applies in
            # scanner.candle_close_handler). Best-effort + legacy-exempt: any
            # failure returns None so the confirmed-only bias stands.
            developing_struct = None
            if not getattr(self, "legacy_mode", False):
                developing_struct = self._build_developing_struct(pair, slices, now)
            wm = analyze_window(
                pair,
                {"H4": h4, "H1": h1, "M15": m15, "M5": m5},
                timestamp=now,
                consensus_config=getattr(self.config, "consensus", None),
                developing_struct_by_tf=developing_struct,
            )
        except Exception as exc:
            logger.warning("[backtest] decision core failed for {}: {}", pair, exc)
            return None

        if getattr(self, "legacy_mode", False) or getattr(self, "decision_engine", None) is None:
            return self._decide_setup_legacy(pair, wm, slices, now, balance)
        return self._decide_setup_live(pair, wm, slices, now, balance)

    def _decide_setup_legacy(
        self,
        pair: str,
        wm,
        slices: dict[str, pd.DataFrame],
        now: datetime,
        balance: float,
    ) -> Optional[BacktestSetup]:
        """Original geometry-only path: scan-view bias gate → EntryEngine."""
        result = _world_model_to_scan_view(wm)
        if result.status != "READY" or result.direction not in ("LONG", "SHORT"):
            return None

        signal = self._calculate_entry(pair, result.direction, slices, balance, result)
        if signal is None:
            return None

        return BacktestSetup(
            direction=signal.direction,
            entry_price=signal.entry_price,
            stop_loss=signal.stop_loss,
            tp1=signal.tp1,
            tp2=signal.tp2,
            score=result.score,
            confluences=list(signal.confluences),
            regime=result.regime,
            bias_strength=result.bias_strength,
            timestamp=now,
            entry_type=getattr(signal, "entry_type", "MARKET"),
            opportunity_quality=getattr(result, "opportunity_quality", 0.0),
            entry_quality=getattr(result, "entry_quality", 0.0),
            consensus_agreement=getattr(result, "consensus_agreement", 0.0),
        )

    def _decide_setup_live(
        self,
        pair: str,
        wm,
        slices: dict[str, pd.DataFrame],
        now: datetime,
        balance: float,
    ) -> Optional[BacktestSetup]:
        """Full-parity entry adjudication — the SAME engines the live plane uses."""
        votes = wm.votes_list()
        if not votes:
            return None

        cfg = getattr(self.config, "consensus", None)
        if cfg is None:
            return None

        # ── Stateful ThesisEngine feed (mirrors live _feed_thesis_engine) ────
        # Keep competing Long/Short/Flat theses alive per symbol from this bar's
        # vote panel + probabilistic bias, so the gate below can require the
        # dominant DIRECTIONAL thesis to beat the Flat (do-nothing) baseline —
        # the same EV-over-flat opportunity-cost test the live plane applies.
        self._feed_thesis_engine(pair, votes, wm)

        # ── Consensus thesis trigger (same call as the live consensus path) ──
        try:
            from brain.directional_consensus import form_thesis
            thesis = form_thesis(
                votes,
                min_net_score=cfg.min_net_score,
                min_agreement=cfg.min_agreement,
                high_authority_modules=list(cfg.high_authority_modules),
                high_authority_oppose_confidence=cfg.high_authority_oppose_confidence,
                min_contributors=cfg.min_contributors,
                conviction_threshold=cfg.conviction_threshold,
                net_scale=cfg.net_scale,
                symbol=pair,
                conviction_store=getattr(self, "symbol_conviction", None),
                currency_strength_penalty_mode=getattr(
                    cfg, "currency_strength_penalty_mode", "penalty"
                ),
                currency_strength_penalty_amount=getattr(
                    cfg, "currency_strength_penalty_amount", 20.0
                ),
            )
        except Exception as exc:
            logger.warning("[backtest] form_thesis failed for {}: {}", pair, exc)
            return None

        if not thesis.trigger:
            return None
        direction = thesis.direction
        if direction not in ("LONG", "SHORT"):
            return None

        # ── ThesisEngine gate (Gap 1b — mirrors live _thesis_gate_allows) ────
        # The stateful engine must agree: the dominant directional thesis is
        # actionable (beats Flat by the opportunity-cost margin) AND points the
        # SAME way as this trigger. Fail-safe — a missing engine / cold-start /
        # fault never blocks (the consensus trigger + permit gates stay
        # authoritative).
        if not self._thesis_gate_allows(pair, direction):
            logger.debug(
                "[backtest] {} {} thesis gate rejected", pair, direction,
            )
            return None

        # ── Score = zone geometry (70/80/100), NOT bias confidence ──────────
        zone = self._select_zone(wm, direction)
        if zone is None:
            return None
        score = int(getattr(zone, "conviction", 0) or 0)

        # ── M1 momentum confirmation (live-parity — entry.m1_confirmation) ──
        # The live entry path requires M1 momentum confirmation after a zone
        # touch before an entry fires (``M1CandleConfirmer.on_m1_close``). The
        # backtest already carries M1 data, so it applies the SAME gate here:
        # ≥3/5 aligned closed candles, two consecutive, or a higher-low /
        # lower-high pattern. Not confirmed → skip this bar.
        m1_slice = slices.get("M1")
        if not self._m1_momentum_confirmed(m1_slice, direction):
            logger.debug(
                "[backtest] {} {} M1 momentum not confirmed — skip", pair, direction,
            )
            return None

        # ── Entry prices from the shared EntryEngine ────────────────────────
        # EntryEngine.calculate_entry reads scan_result.score/.confluences, which
        # the WorldModel does not expose — feed it the same scan-view adapter the
        # geometry path uses, carrying the zone conviction as the entry score.
        scan_view = _world_model_to_scan_view(wm)
        scan_view.score = score
        signal = self._calculate_entry(pair, direction, slices, balance, scan_view)
        if signal is None:
            return None

        # ── Zone-edge entry price + source attribution (live-parity) ────────
        # The live plane fills at the zone edge on a zone touch (TickEntryDetector
        # → M1 confirmation → entry at the zone edge), not at the candle close.
        # Approximate that timing here with the current M1 candle's high/low
        # against the selected zone: a touch adopts the zone-edge fill and marks
        # the entry ``source="zone"``; no touch keeps the consensus (engine)
        # entry and marks it ``source="consensus"``.
        entry_source = self._apply_zone_edge_entry(signal, zone, m1_slice, direction)

        # ── EntryGate (score ≥ 85 floor, same gate as the live zone path) ───
        # Signed HTF alignment from the WorldModel bias (same derivation as the
        # live entry orchestrator) so the alignment floor gate behaves
        # identically in replay — strongly counter-trend setups are rejected
        # rather than entered and instantly closed.
        gate_alignment: Optional[float] = None
        gate_long_p: float = 0.0
        gate_short_p: float = 0.0
        try:
            _bias = wm.bias_dict()
            _bdir = str(_bias.get("direction", "") or "").upper()
            _bscore = max(0.0, min(100.0, float(_bias.get("score", 0) or 0))) / 100.0
            if _bdir in ("LONG", "SHORT"):
                gate_alignment = _bscore if _bdir == direction.upper() else -_bscore
            else:
                gate_alignment = 0.0
            # Phase 3 probabilistic evidence for the EV gate (default-on),
            # so replay gates entries identically to the live zone path.
            gate_long_p = float(_bias.get("long_probability", 0.0) or 0.0)
            gate_short_p = float(_bias.get("short_probability", 0.0) or 0.0)
        except Exception:
            gate_alignment = None
        try:
            passed, results = self.entry_gate.validate_all(
                symbol=pair,
                direction=direction,
                entry_price=signal.entry_price,
                stop_loss=signal.stop_loss,
                tp1=signal.tp1,
                tp2=signal.tp2,
                score=score,
                current_spread_pips=0.0,
                zone=zone,
                alignment=gate_alignment,
                long_probability=gate_long_p,
                short_probability=gate_short_p,
                is_instrument_known=True,
                is_market_open=True,
                is_session_active=True,
                is_news_clear=True,
                is_drawdown_ok=True,
                utc_now=now,
            )
        except Exception as exc:
            logger.warning("[backtest] entry gate failed for {}: {}", pair, exc)
            return None
        if not passed:
            failed = [r.gate_name for r in results if not r.passed]
            logger.debug("[backtest] {} {} gate rejected: {}", pair, direction, failed)
            return None

        # ── DecisionEngine adjudication (consensus-opposition SKIP) ─────────
        entry_ctx = self._build_entry_context(
            pair, direction, signal, score, zone, slices, wm, balance, votes, now,
        )
        sa = self.situation_engine.assess_entry(entry_ctx)
        de_result = self.decision_engine.decide_entry(entry_ctx, sa)
        if not de_result.should_enter:
            logger.debug("[backtest] {} {} DE SKIP: {}", pair, direction, de_result.reason)
            return None
        de_size_mult = de_result.size_multiplier

        # ── RiskGovernor entry review (graded/legacy veto) ──────────────────
        try:
            gov = self.risk_governor.review_entry(de_result, entry_ctx, sa)
            if not gov.should_enter:
                logger.debug("[backtest] {} {} governor veto: {}", pair, direction,
                             getattr(gov, "governor_reason", ""))
                return None
            if gov is not de_result:
                de_size_mult = gov.size_multiplier
                risk_mult = getattr(gov, "risk_multiplier", 1.0)
                if risk_mult < 1.0:
                    de_size_mult = round(de_size_mult * risk_mult, 3)
        except Exception as exc:
            logger.debug("[backtest] governor review failed for {}: {}", pair, exc)

        # ── Compliance Division: single authoritative permit (live parity) ───
        # Department 3 — the ONE pure permit layer. In the single-position
        # serial replay the book is empty at entry time (sizing only happens
        # with no open trade), so duplicate / max-positions trivially pass; the
        # gates that actually bind here are the daily-loss halt, per-account
        # heat, DrawdownGuard FROZEN and the PortfolioRisk DEFENSIVE+ state —
        # the same fail-CLOSED vetoes the live ``_on_entry_decision`` runs.
        if not self._compliance_permits(pair, direction, balance):
            return None

        # ── PortfolioRisk EMERGENCY block (live parity) ──────────────────────
        # A hard block on new entries while the portfolio is in EMERGENCY heat,
        # mirroring the live plane (Compliance also blocks DEFENSIVE/REDUCING;
        # this is the explicit survival-state fail-safe when Compliance is
        # degraded to None).
        if self.portfolio_risk_sm is not None:
            try:
                from risk.portfolio_risk_state import PortfolioRiskState
                if self.portfolio_risk_sm.state == PortfolioRiskState.EMERGENCY:
                    logger.debug(
                        "[backtest] {} {} entry blocked — PortfolioRisk EMERGENCY",
                        pair, direction,
                    )
                    return None
            except Exception as exc:  # noqa: BLE001
                logger.debug("[backtest] portfolio-risk EMERGENCY check failed: {}", exc)

        # ── DrawdownGuard risk cap (live parity — event_driven ~8535) ────────
        # ``current_risk_pct`` already encodes the per-mode reduction
        # (NORMAL/CAUTION/RECOVERY/FROZEN); use it as a ceiling so a drawdown
        # never lets the sized risk exceed the guard's recommendation.
        base_risk_pct = float(self.risk_per_trade)
        if self.drawdown_guard is not None:
            try:
                dd_risk = float(
                    getattr(self.drawdown_guard.get_status(), "current_risk_pct", 0.0) or 0.0
                )
                if 0.0 < dd_risk < base_risk_pct:
                    base_risk_pct = dd_risk
            except Exception as exc:  # noqa: BLE001
                logger.debug("[backtest] DrawdownGuard risk-cap failed for {}: {}", pair, exc)

        # ── Full-parity sizing chain (Phase 2 — mirror live _on_entry_decision) ─
        # Orchestrator round table → adaptive optimizer → volatility / density /
        # execution / capital multipliers → per-instrument ATR → opportunity-
        # quality sizer, folded into the SAME SizingFactors bundle the live plane
        # feeds PortfolioDivision. Vetoes (Orchestrator physics / uniformly-weak,
        # a confirmed losing pattern, an optimizer AVOID) reject the entry.
        factors = self._build_sizing_factors(
            pair, direction, wm, slices, signal, zone, score,
            de_result, de_size_mult, base_risk_pct, now,
        )
        if factors is None:
            return None

        # ── Opportunistic risk shaping (Phase 3 Feature B) ──────────────────
        # Fold the session-aware / compression / DXY / news multiplier onto the
        # base risk exactly as the live opportunistic plane scales risk_pct,
        # AFTER the pure sizing-factor composition (kept unchanged for parity).
        opp_mult = self._opportunistic_risk_mult(pair, direction, slices, now)
        if opp_mult != 1.0:
            new_risk = round(float(getattr(factors, "base_risk_pct", base_risk_pct)) * opp_mult, 6)
            try:
                factors.base_risk_pct = new_risk
            except Exception:  # noqa: BLE001 — frozen SizingFactors → rebuild via replace
                from dataclasses import replace as _dc_replace
                factors = _dc_replace(factors, base_risk_pct=new_risk)

        # ── Position sizing via PortfolioDivision → PositionSizer ───────────
        sized = self._size_trade(
            pair, direction, signal, factors, de_result.conviction, balance,
        )
        if sized is None:
            return None
        lots, max_loss, risk_amount = sized

        zone_type = getattr(zone, "zone_type", "")
        zone_type = str(getattr(zone_type, "value", zone_type) or "")
        regimes = wm.regime_by_tf()
        # Capture entry-time OQ/EQ (same formula the live plane stores on the
        # position) so management can score oq_decay/eq_decay against it.
        entry_oq, entry_eq = _oq_eq_from_wm(wm, direction)
        return BacktestSetup(
            direction=signal.direction,
            entry_price=signal.entry_price,
            stop_loss=signal.stop_loss,
            tp1=signal.tp1,
            tp2=signal.tp2,
            score=score,
            confluences=list(signal.confluences),
            regime=regimes.get("H1") or next(iter(regimes.values()), "UNKNOWN"),
            bias_strength=str(wm.bias_dict().get("strength", "") or ""),
            timestamp=now,
            entry_type=getattr(signal, "entry_type", "MARKET"),
            consensus_agreement=float(getattr(thesis, "conviction", 0.0) or 0.0),
            zone_type=zone_type,
            is_counter_trend=bool(getattr(zone, "is_counter_trend", False)),
            bias_direction=str(getattr(zone, "bias_direction", "") or ""),
            de_size_mult=de_size_mult,
            de_conviction=float(de_result.conviction),
            lots=lots,
            max_loss=max_loss,
            risk_amount=risk_amount,
            pip_value_per_lot=self.pip_value_per_lot,
            entry_oq=entry_oq,
            entry_eq=entry_eq,
            source=entry_source,
        )

    # ── Stateful ThesisEngine (Gap 1a/1b — live-parity entry gate) ───────────

    def _feed_thesis_engine(self, symbol: str, votes: list, wm) -> None:
        """Feed the stateful ThesisEngine from this bar's evidence.

        Mirrors the live ``_feed_thesis_engine``: build the competing
        Long/Short/Flat theses from the vote panel + probabilistic bias so the
        entry gate can require the dominant directional thesis to beat the Flat
        baseline. Best-effort — a tracking fault never affects the entry path.
        """
        engine = getattr(self, "thesis_engine", None)
        if engine is None:
            return
        try:
            bias = wm.bias_dict()
            long_p = float(bias.get("long_probability", 0.0) or 0.0)
            short_p = float(bias.get("short_probability", 0.0) or 0.0)
            avg_rr = float(
                getattr(getattr(self.config, "risk", None), "tp1_rr", 1.5) or 1.5
            )
            ev_long = long_p * avg_rr - short_p
            ev_short = short_p * avg_rr - long_p
            engine.update(
                symbol=symbol,
                votes=list(votes or []),
                long_probability=long_p,
                short_probability=short_p,
                entry_ev_long=ev_long,
                entry_ev_short=ev_short,
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("[backtest] thesis-engine feed failed for {}: {}", symbol, exc)

    def _thesis_gate_allows(self, symbol: str, direction: str) -> bool:
        """ThesisEngine entry gate — fail-safe (mirrors live _thesis_gate_allows).

        Returns True (allow) when the engine is absent, the gate is disabled, no
        thesis is tracked yet, or anything errors. Blocks only when a thesis IS
        tracked and the dominant read either says "do nothing" or disagrees with
        this entry's direction.
        """
        engine = getattr(self, "thesis_engine", None)
        if engine is None:
            return True
        tcfg = getattr(self.config, "thesis", None)
        if tcfg is not None and not getattr(tcfg, "gate_enabled", True):
            return True
        try:
            if engine.get(symbol) is None:
                return True
            should_trade, thesis_dir, ev_adv = engine.should_act(symbol)
            want = str(direction or "").upper()
            if not should_trade:
                logger.debug(
                    "[backtest] {} thesis not actionable (EV over flat {:.3f}R "
                    "below margin)", symbol, ev_adv,
                )
                return False
            if str(thesis_dir or "").upper() != want:
                logger.debug(
                    "[backtest] {} thesis direction {} disagrees with entry {}",
                    symbol, thesis_dir, want,
                )
                return False
            return True
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "[backtest] {} thesis-gate evaluation errored — allowing "
                "(fail-safe): {}", symbol, exc,
            )
            return True

    # ── Compliance + portfolio-risk state (live-parity permit / heat) ────────

    def _compliance_permits(self, symbol: str, direction: str, balance: float) -> bool:
        """Run the ComplianceDivision permit for a prospective entry.

        Returns True when the trade is permitted (or Compliance is degraded to
        None). The book is empty in the single-position serial replay, so the
        binding gates are daily-loss halt, per-account heat, DrawdownGuard
        FROZEN and the PortfolioRisk state. Fail-CLOSED inside the division.
        """
        compliance = getattr(self, "compliance", None)
        if compliance is None:
            return True
        try:
            from compliance.models import (
                ComplianceAccount, ComplianceBook, ComplianceCandidate,
            )
            if self.account_risk is not None and balance and balance > 0:
                self.account_risk.update_balance(self._bt_account_key, float(balance))
            verdict = compliance.permit(
                ComplianceCandidate(symbol=symbol, direction=direction),
                ComplianceBook(open_positions=[]),
                ComplianceAccount(
                    account_key=self._bt_account_key, balance=float(balance or 0.0),
                ),
            )
            if verdict.rejected:
                logger.debug(
                    "[backtest] {} {} compliance REJECTED — {}",
                    symbol, direction, "; ".join(verdict.reasons),
                )
                return False
            return True
        except Exception as exc:  # noqa: BLE001
            # Fail-CLOSED: a permit-layer fault must not admit an ungated trade.
            logger.debug(
                "[backtest] {} compliance permit errored — blocking (fail-closed): {}",
                symbol, exc,
            )
            return False

    def _update_portfolio_risk_state(
        self, open_trade: Optional[dict], balance: float, now: Optional[datetime],
    ) -> None:
        """Evaluate the PortfolioRiskStateMachine from the live open-trade heat.

        Single-position serial book: heat is the open trade's capital-at-risk
        over the account balance (0 when flat). Also mirrors live by publishing
        the per-account heat onto the AccountRiskManager so the Compliance heat
        gate sees a real reading. Ladder + hard emergency triggers mirror the
        live ``_update_portfolio_risk_state``. Best-effort.
        """
        sm = getattr(self, "portfolio_risk_sm", None)
        if sm is None:
            return
        try:
            from risk.portfolio_risk_state import (
                PortfolioRiskSnapshot,
                PositionRisk,
                compute_live_heat_pct,
                compute_position_risk_dollars,
            )
        except Exception:  # noqa: BLE001
            return
        try:
            position_risks: list = []
            if open_trade is not None:
                direction = open_trade["setup"].direction
                risk_d, is_fallback = compute_position_risk_dollars(
                    direction=direction,
                    entry_price=float(open_trade["entry_price"]),
                    sl=float(open_trade["stop_loss"]),
                    lots=float(open_trade.get("lots", 0.0) or 0.0),
                    pip_size=self.pip_size,
                    pip_value_per_lot=self.pip_value_per_lot,
                    at_breakeven=bool(open_trade.get("at_breakeven", False)),
                )
                position_risks.append(PositionRisk(
                    order_id=str(open_trade.get("order_id", "")),
                    symbol=str(open_trade.get("symbol", "")),
                    direction=direction,
                    risk_dollars=risk_d,
                    is_at_breakeven=bool(open_trade.get("at_breakeven", False)),
                    is_fallback=is_fallback,
                ))
            heat_pct = (
                compute_live_heat_pct(position_risks, balance)
                if balance and balance > 0 else 0.0
            )
            if self.account_risk is not None:
                try:
                    self.account_risk.set_heat(self._bt_account_key, heat_pct)
                except Exception:  # noqa: BLE001
                    pass
            snapshot = PortfolioRiskSnapshot(
                live_heat_pct=heat_pct,
                position_risks=position_risks,
                correlation_safe=True,
                max_currency_exposure=0.0,
            )
            sm.evaluate(snapshot)

            # Hard emergency triggers force-escalate from any state (extreme
            # heat or a FROZEN DrawdownGuard), mirroring the live plane.
            from risk.portfolio_risk_state import EmergencyTriggerResult
            trig = EmergencyTriggerResult()
            emerg_pct = getattr(sm, "heat_emergency_pct", 4.0)
            if heat_pct >= emerg_pct:
                trig.extreme_heat = True
            if self.drawdown_guard is not None:
                try:
                    from brain.drawdown_guard import DrawdownMode
                    if self.drawdown_guard.get_status().mode == DrawdownMode.FROZEN.value:
                        trig.drawdown_frozen = True
                except Exception:  # noqa: BLE001
                    pass
            if trig.any_fired:
                sm.escalate_to_emergency(trig, heat_pct, True)
        except Exception as exc:  # noqa: BLE001
            logger.debug("[backtest] portfolio-risk SM evaluate failed: {}", exc)

    def _maybe_emergency_close(
        self, trade: dict, candle: pd.Series,
    ) -> Optional[dict]:
        """Force-close the open trade when PortfolioRisk is in EMERGENCY.

        Live parity: ``_check_portfolio_heat`` EMERGENCY flattens open
        positions. In the single-position serial replay that is the one open
        trade. Returns a close-event dict or None. Best-effort.
        """
        sm = getattr(self, "portfolio_risk_sm", None)
        if sm is None:
            return None
        try:
            from risk.portfolio_risk_state import PortfolioRiskState
            if sm.state != PortfolioRiskState.EMERGENCY:
                return None
            close_event = self._force_close(trade, candle)
            close_event["exit_reason"] = "portfolio_heat_emergency"
            logger.info(
                "[backtest] {} force-closed — PortfolioRisk EMERGENCY (heat flatten)",
                trade.get("symbol", ""),
            )
            return close_event
        except Exception as exc:  # noqa: BLE001
            logger.debug("[backtest] emergency force-close failed: {}", exc)
            return None

    def _register_close_risk(
        self, pnl_dollars: float, balance: float, now: Optional[datetime],
    ) -> None:
        """Feed a realized close into the live risk silos (live-parity close path).

        Mirrors ``event_driven_bootstrap``'s close-path risk layer:
          * DrawdownGuard.register_trade_result — P&L as a fraction of balance,
            timestamped with the bar so day-rolls use simulated (not wall)
            time.
          * AccountRiskManager.update_balance + register_realized — the
            per-account daily-P&L silo (drives the daily-loss halt).
        Best-effort; a fault never breaks the replay.
        """
        if self.drawdown_guard is not None:
            try:
                pnl_pct = pnl_dollars / balance if balance and balance > 0 else 0.0
                self.drawdown_guard.register_trade_result(pnl_pct, timestamp=now)
            except Exception as exc:  # noqa: BLE001
                logger.debug("[backtest] DrawdownGuard close update failed: {}", exc)

        if self.account_risk is not None:
            try:
                if balance and balance > 0:
                    self.account_risk.update_balance(self._bt_account_key, float(balance))
                self.account_risk.register_realized(self._bt_account_key, float(pnl_dollars))
            except Exception as exc:  # noqa: BLE001
                logger.debug("[backtest] AccountRisk close update failed: {}", exc)

    def _register_close_adaptive(self, close_event: Optional[dict]) -> None:
        """Feed a realized close into the adaptive optimizer (Phase 2 parity).

        Mirrors the live close-path ``ml_adapter.register_new_trade`` count/edge
        signal so the optimizer's retrain cadence + exit-cause learning advance
        across the replay. Best-effort; a fault never breaks the replay. The
        Orchestrator dashboard proposal event is intentionally omitted — the
        backtest has no event store to emit onto.
        """
        adapter = getattr(self, "bt_ml_adapter", None)
        if adapter is None:
            return
        try:
            exit_cause = None
            if isinstance(close_event, dict):
                exit_cause = close_event.get("exit_reason") or close_event.get("outcome")
            adapter.register_new_trade(exit_cause=exit_cause)
        except Exception as exc:  # noqa: BLE001
            logger.debug("[backtest] adaptive close register failed: {}", exc)

    def _calculate_entry(
        self,
        pair: str,
        direction: str,
        slices: dict[str, pd.DataFrame],
        balance: float,
        scan_result,
    ):
        """Shared EntryEngine call (prices/targets). Returns an EntrySignal or None."""
        h4 = slices.get("H4")
        h1 = slices.get("H1")
        m15 = slices.get("M15")
        m5 = slices.get("M5")
        m1 = slices.get("M1")
        try:
            from trigger.entry_engine import EntryRejection
            signal = self.entry_engine.calculate_entry(
                pair=pair,
                direction=direction,
                m5_df=m5,
                m1_df=m1,
                h1_df=h1,
                scan_result=scan_result,
                account_balance=balance,
                h4_df=h4,
                m15_df=m15,
            )
        except Exception as exc:
            logger.warning("[backtest] calculate_entry failed for {}: {}", pair, exc)
            return None
        if isinstance(signal, EntryRejection):
            return None
        return signal

    def _select_zone(self, wm, direction: str):
        """Highest-conviction WorldModel entry zone matching the thesis direction.

        Zone conviction (70/80/100 geometry prior) is the live entry score —
        the same metric ``EntryGate`` gates at ≥ 85 in the live zone path.
        """
        best = None
        best_score = -1
        for zone in wm.entry_zones_list():
            if str(getattr(zone, "direction", "") or "") != direction:
                continue
            conv = int(getattr(zone, "conviction", 0) or 0)
            if conv > best_score:
                best_score = conv
                best = zone
        return best

    @staticmethod
    def _m1_momentum_confirmed(m1_df, direction: str) -> bool:
        """M1 momentum confirmation gate — mirrors ``entry.m1_confirmation``
        ``M1CandleConfirmer._check_momentum`` pattern logic.

        Uses the last 5 CLOSED M1 candles from the replay slice (the current
        bar is already a closed candle in replay, so no forming bar is dropped).
        Confirms when any of the same patterns the live confirmer accepts hold:

        * ≥ 3/5 candles aligned with the trade direction, OR
        * the last two candles are consecutive aligned closes, OR
        * a higher-low + higher-close (LONG) / lower-high + lower-close (SHORT)
          reversal pattern.

        Returns ``False`` (skip the bar) when there are fewer than 5 M1 bars or
        no pattern is present — the same conservative behaviour as live.
        """
        if m1_df is None or len(m1_df) < 5:
            return False
        last5 = m1_df.iloc[-5:]
        closes = last5["close"].values
        opens = last5["open"].values
        highs = last5["high"].values
        lows = last5["low"].values

        if direction == "LONG":
            bullish = sum(1 for c, o in zip(closes, opens) if c > o)
            if bullish >= 3:
                return True
            if closes[-1] > opens[-1] and closes[-2] > opens[-2] and closes[-1] > closes[-2]:
                return True
            if lows[-1] > lows[-3] and closes[-1] > closes[-3]:
                return True
        else:
            bearish = sum(1 for c, o in zip(closes, opens) if c < o)
            if bearish >= 3:
                return True
            if closes[-1] < opens[-1] and closes[-2] < opens[-2] and closes[-1] < closes[-2]:
                return True
            if highs[-1] < highs[-3] and closes[-1] < closes[-3]:
                return True
        return False

    def _apply_zone_edge_entry(self, signal, zone, m1_df, direction: str) -> str:
        """Set the entry price to the zone edge on a zone touch (live parity).

        The live plane enters at the zone edge when price touches the zone
        (``entry.tick_entry_detector`` zone-touch fill), not at the candle close.
        This approximates that using the current M1 candle's high/low against the
        selected zone (M1 OHLC standing in for the live tick stream):

        * LONG  (demand zone): fill at ``zone.top``, or deeper (candle low,
          clamped to ``zone.bottom``) when price pushed further into the zone.
        * SHORT (supply zone): fill at ``zone.bottom``, or deeper (candle high,
          clamped to ``zone.top``) when price pushed further into the zone.

        Mutates ``signal.entry_price`` in place when a touch fill is adopted and
        returns the entry source (``"zone"`` on a touch, ``"consensus"`` when the
        candle did not touch the zone and the engine's consensus entry stands).
        Best-effort — any fault falls back to the consensus entry.
        """
        try:
            top = float(getattr(zone, "top", 0.0) or 0.0)
            bottom = float(getattr(zone, "bottom", 0.0) or 0.0)
            if (
                m1_df is None
                or len(m1_df) == 0
                or top <= 0.0
                or bottom <= 0.0
                or top < bottom
            ):
                return "consensus"

            last = m1_df.iloc[-1]
            hi = float(last["high"])
            lo = float(last["low"])

            # Touch = the current M1 candle's range overlaps the zone band.
            if hi < bottom or lo > top:
                return "consensus"

            if direction == "LONG":
                edge = min(top, max(lo, bottom))
            else:
                edge = max(bottom, min(hi, top))

            # Adopt the zone-edge fill only when it keeps the entry on the
            # correct side of the stop and TP1 (a degenerate zone must not flip
            # the trade geometry); otherwise keep the engine price but still
            # attribute the touch as a zone entry.
            sl = float(getattr(signal, "stop_loss", 0.0) or 0.0)
            tp1 = float(getattr(signal, "tp1", 0.0) or 0.0)
            valid = (
                (edge > sl and edge < tp1) if direction == "LONG"
                else (edge < sl and edge > tp1)
            )
            if valid:
                signal.entry_price = round(edge, 5)
            return "zone"
        except Exception as exc:  # noqa: BLE001 — entry attribution is best-effort
            logger.debug("[backtest] zone-edge entry failed: {}", exc)
            return "consensus"

    def _build_developing_struct(self, pair: str, slices: dict, now):
        """Simulate the live ``DevelopingAnalysisLoop`` for one replay bar.

        For each HTF the confirmed slice carries, builds the still-forming candle
        from the M1 bars since the last HTF close (the backtest analogue of
        ``tick.live_candle_aggregator``), runs ``run_tf_modules`` with
        ``include_forming=True`` (treating the forming bar as if it had just
        closed — the same contract the developing loop uses), and returns the
        developing ``StructureAnalysis`` per timeframe. That dict is fed to
        ``compute_bias`` as ×0.70-discounted evidence, matching the confirmed
        live path (``scanner.candle_close_handler``). Uses its own engine
        instances so it never shares state with the confirmed pipeline.

        Best-effort: returns ``None`` on any fault so the confirmed-only bias
        stands (the current behaviour).
        """
        m1 = slices.get("M1")
        if m1 is None or getattr(m1, "empty", True):
            return None
        try:
            from brain.decision_core import run_tf_modules

            if getattr(self, "_dev_structure", None) is None:
                from brain.liquidity_mapper import LiquidityMapper
                from brain.structure_engine import StructureEngine
                from brain.volume_analyzer import VolumeAnalyzer

                self._dev_structure = StructureEngine()
                self._dev_liquidity = LiquidityMapper()
                self._dev_volume = VolumeAnalyzer()
        except Exception as exc:  # noqa: BLE001
            logger.debug("[backtest] developing engines init failed: {}", exc)
            return None

        developing: dict = {}
        for tf, tf_secs in _DEVELOPING_TF_SECONDS.items():
            base = slices.get(tf)
            if base is None or getattr(base, "empty", True):
                continue
            try:
                forming = self._forming_htf_candle(m1, base, now, tf_secs)
                if forming is None:
                    continue
                dev_slice = pd.concat(
                    [drop_forming_bar(base), forming], ignore_index=True,
                )
                results = run_tf_modules(
                    pair, tf, dev_slice,
                    structure=self._dev_structure,
                    liquidity=self._dev_liquidity,
                    volume=self._dev_volume,
                    include_forming=True,
                )
                sa = results.get("structure") if results else None
                if sa is not None:
                    developing[tf] = sa
            except Exception as exc:  # noqa: BLE001
                logger.debug(
                    "[backtest] developing {} {} analysis failed: {}", pair, tf, exc,
                )
        return developing or None

    @staticmethod
    def _forming_htf_candle(m1_df, base_df, now, tf_secs: int):
        """Aggregate the M1 bars of the current, still-forming HTF candle.

        Selects the M1 bars from the current HTF boundary (``now`` floored to the
        timeframe length) up to ``now`` and folds them into a single OHLC row —
        the backtest analogue of ``LiveCandleAggregator``'s forming candle
        (open = first, high = max, low = min, close = last, volume = sum). Built
        purely from M1 bars up to ``now`` so it never leaks future data (the
        fully-formed HTF candle in the replay frame would). Returns a 1-row
        DataFrame aligned to ``base_df``'s columns, or ``None`` when the window
        is empty.
        """
        now_ts = pd.Timestamp(now)
        boundary_ts = now_ts.floor(f"{tf_secs}s")
        window = m1_df[(m1_df["time"] >= boundary_ts) & (m1_df["time"] <= now_ts)]
        if getattr(window, "empty", True):
            return None

        last = window.iloc[-1]
        data: dict = {}
        for col in base_df.columns:
            if col == "time":
                data[col] = last["time"]
            elif col == "open":
                data[col] = float(window["open"].iloc[0])
            elif col == "high":
                data[col] = float(window["high"].max())
            elif col == "low":
                data[col] = float(window["low"].min())
            elif col == "close":
                data[col] = float(window["close"].iloc[-1])
            elif col in ("volume", "tick_volume"):
                data[col] = (
                    float(window[col].sum()) if col in window.columns else 0.0
                )
            else:
                data[col] = last[col] if col in window.columns else 0.0
        return pd.DataFrame([data], columns=list(base_df.columns))

    def _build_entry_context(
        self,
        pair: str,
        direction: str,
        signal,
        score: int,
        zone,
        slices: dict[str, pd.DataFrame],
        wm,
        balance: float,
        votes: list,
        now: Optional[datetime] = None,
    ):
        """Build the EntryContext the live SituationEngine/DecisionEngine read."""
        from decision.context import EntryContext

        structure = wm.structure_by_tf()
        d1_trend, d1_conf = _struct_trend_conf(structure, "D1")
        h4_trend, h4_conf = _struct_trend_conf(structure, "H4")
        h1_trend, h1_conf = _struct_trend_conf(structure, "H1")
        d1_event = _struct_event(structure, "D1")
        h4_event = _struct_event(structure, "H4")
        h1_event = _struct_event(structure, "H1")

        is_long = direction == "LONG"
        micro = self._micro_from_slice(slices.get("M1"), is_long)
        micro_conf, entry_mode_v = _micro_confirmation_from_event(
            micro["m1_event"], direction, micro.get("m1_pattern", ""),
        )

        sl = signal.stop_loss
        entry_price = signal.entry_price
        risk_pips = abs(entry_price - sl) / self.pip_size if sl else 0.0
        rr2 = (
            abs(signal.tp2 - entry_price) / max(abs(entry_price - sl), 1e-8)
            if sl else 0.0
        )
        zone_type = getattr(zone, "zone_type", "")
        zone_type = str(getattr(zone_type, "value", zone_type) or "")

        # Simulated spread (pips) so the RiskGovernor's graded spread dimension
        # engages instead of being skipped (typical_spread>0 guard). The replay
        # has no live tick spread, so current ≈ typical (a neutral ratio) using
        # the configured backtest default. News timing has no historical feed →
        # minutes_to_high_impact_news keeps its safe 999 default (accepted).
        typical_spread = float(
            getattr(self.config.backtest, "default_spread_pips", 0.0) or 0.0
        )

        # Session tradeable from the candle timestamp (same SessionEngine the
        # management plane uses), so entry urgency can engage in replay.
        session_tradeable = True
        if now is not None and self.session_engine is not None:
            try:
                ss = self.session_engine.get_status(now)
                session_tradeable = bool(getattr(ss, "is_tradeable", True))
            except Exception:
                session_tradeable = True

        # Setup-quality (OQ/EQ) and ranker horizon from the SAME WorldModel
        # layers the live entry plane reads — keeps the planes in parity. OQ/EQ
        # default to 0.0 (= not computed); horizon to "" (full HTF authority)
        # when no ranked candidate matches this direction.
        bt_oq, bt_eq = _oq_eq_from_wm(wm, direction)
        entry_oq_v = float(bt_oq) if bt_oq is not None else 0.0
        entry_eq_v = float(bt_eq) if bt_eq is not None else 0.0
        entry_horizon = ""
        try:
            best = None
            for opp in (getattr(wm, "candidates", ()) or ()):
                if str(getattr(opp, "direction", "")).upper() != direction.upper():
                    continue
                if best is None or float(
                    getattr(opp, "expected_value", 0.0) or 0.0
                ) > float(getattr(best, "expected_value", 0.0) or 0.0):
                    best = opp
            if best is not None:
                entry_horizon = str(getattr(best, "timeframe_class", "") or "")
        except Exception:
            entry_horizon = ""

        return EntryContext(
            symbol=pair,
            direction=direction,
            scan_score=score,
            scan_direction=direction,
            entry_type=zone_type,
            entry_price=entry_price,
            stop_loss=sl,
            tp1=signal.tp1,
            tp2=signal.tp2,
            risk_reward_2=rr2,
            risk_pips=risk_pips,
            entry_mode=entry_mode_v,
            micro_confirmation=micro_conf,
            oq=entry_oq_v,
            eq=entry_eq_v,
            account_balance=balance,
            risk_pct=self.risk_per_trade,
            d1_trend=d1_trend, d1_confidence=d1_conf, d1_event=d1_event,
            h4_trend=h4_trend, h4_confidence=h4_conf, h4_event=h4_event,
            h1_trend=h1_trend, h1_confidence=h1_conf, h1_event=h1_event,
            m1_trend=micro["m1_trend"],
            m1_event=micro["m1_event"],
            m1_aligned_count=micro["m1_aligned_count"],
            is_counter_trend=bool(getattr(zone, "is_counter_trend", False)),
            bias_direction=str(getattr(zone, "bias_direction", "") or ""),
            open_trade_count=0,
            max_open_trades=getattr(self.config.risk, "max_open_trades", 5),
            current_spread=typical_spread,
            typical_spread=typical_spread,
            session_tradeable=session_tradeable,
            regime=wm.regime_by_tf().get("H1", "") or "",
            horizon=entry_horizon,
            confluences=list(signal.confluences),
            consensus_votes=list(votes),
        )

    # ── Full-parity sizing chain (Phase 2 — live _on_entry_decision) ─────────

    def _build_sizing_factors(
        self,
        pair: str,
        direction: str,
        wm,
        slices: dict[str, pd.DataFrame],
        signal,
        zone,
        score: int,
        de_result,
        de_size_mult: float,
        base_risk_pct: float,
        now: Optional[datetime],
    ):
        """Compose the live-parity :class:`SizingFactors` bundle.

        Mirrors the live ``_on_entry_decision`` sizing chain
        (``event_driven_bootstrap`` ~8182-8678): Orchestrator round table →
        adaptive optimizer → system-volatility → opportunity-density →
        execution-quality → capital-allocation → per-instrument ATR, plus the
        opportunity-quality sizer folded onto the base risk. Every subsystem is
        best-effort: a missing/None subsystem contributes a neutral ``1.0``.

        Returns ``None`` when a veto/block subsystem rejects the entry (an
        Orchestrator veto, a confirmed losing pattern, or an optimizer AVOID) —
        the same hard "no" the live plane returns on.
        """
        from portfolio.models import SizingFactors

        zone_type = getattr(zone, "zone_type", "")
        zone_type = str(getattr(zone_type, "value", zone_type) or "")
        de_conviction = float(getattr(de_result, "conviction", 0.0) or 0.0)

        # 1. Orchestrator round table — graded size or physics veto.
        orch_mult, orch_vetoed = self._orchestrator_size_mult(
            pair, direction, wm, float(score), de_conviction,
        )
        if orch_vetoed:
            return None

        # 2. Adaptive optimizer — losing-pattern block + AVOID veto + size adjust.
        adapt_mult, adapt_blocked = self._adaptive_size_mult(
            pair, wm, now, zone_type,
        )
        if adapt_blocked:
            return None

        # 3. System-wide volatility monitor.
        vol_mult = 1.0
        if self.system_volatility_monitor is not None:
            try:
                vol_mult = float(self.system_volatility_monitor.get_size_multiplier())
            except Exception as exc:  # noqa: BLE001
                logger.debug("[backtest] system-vol size-mult read failed: {}", exc)

        # 4. Opportunity-density tracker (1.0 until fed in a multi-opp cycle).
        density_mult = 1.0
        if self.bt_opportunity_density is not None:
            try:
                density_mult = float(self.bt_opportunity_density.get_size_multiplier())
            except Exception as exc:  # noqa: BLE001
                logger.debug("[backtest] density size-mult read failed: {}", exc)

        # 5. Execution-quality monitor (1.0 until execution samples accrue).
        exec_mult = 1.0
        if self.bt_execution_monitor is not None:
            try:
                exec_mult = float(self.bt_execution_monitor.get_size_multiplier(pair))
            except Exception as exc:  # noqa: BLE001
                logger.debug("[backtest] exec size-mult read failed: {}", exc)

        # 6. Capital-allocation multiplier (by strategy fingerprint).
        cap_mult = self._capital_alloc_mult(wm)

        # 7. Per-instrument volatility (current vs average M5 ATR).
        inst_vol_mult = self._instrument_vol_mult(slices)

        # 8. Opportunity-quality-proportional sizer — folded onto the base risk
        #    (a boost > 1.0 is allowed here, exactly as the live plane multiplies
        #    ``risk_pct``), unlike the de-risking factors above which
        #    PortfolioDivision clamps to <= 1.0.
        risk_pct = float(base_risk_pct)
        oq_mult = self._opportunity_quality_mult(wm, direction, float(score))
        if oq_mult != 1.0:
            risk_pct = round(risk_pct * oq_mult, 6)

        return SizingFactors(
            base_risk_pct=risk_pct,
            de_size_mult=de_size_mult,
            orch_mult=orch_mult,
            vol_mult=vol_mult,
            inst_vol_mult=inst_vol_mult,
            density_mult=density_mult,
            exec_mult=exec_mult,
            cap_mult=cap_mult,
            adapt_mult=adapt_mult,
        )

    # ── Opportunistic-feature parity (Phase 3 Feature B) ─────────────────────

    def _update_opportunistic_state(
        self, pair: str, slices: dict[str, pd.DataFrame],
    ) -> None:
        """Feed the compression detector this bar's M5/M15 slices (live parity).

        The session context is stateless (a pure function of the bar timestamp)
        so it needs no feed. Best-effort — a detector fault never breaks replay.
        """
        det = self.compression_detector
        if det is None:
            return
        for tf in ("M5", "M15"):
            df = slices.get(tf)
            if df is not None and len(df) > 0:
                try:
                    det.update(pair, tf, df)
                except Exception as exc:  # noqa: BLE001
                    logger.debug(
                        "[backtest] compression update failed {}/{}: {}", pair, tf, exc,
                    )

    def get_market_state(self, pair: str):
        """Current backtest market state for ``pair`` (RANGING when unknown).

        Mirrors the live ``CompressionDetector.get_market_state`` read the entry
        logic consumes; returns ``MarketState.RANGING`` when the detector is off.
        """
        from brain.compression_detector import MarketState
        if self.compression_detector is None:
            return MarketState.RANGING
        try:
            return self.compression_detector.get_market_state(pair)
        except Exception:  # noqa: BLE001
            return MarketState.RANGING

    def _opportunistic_risk_mult(self, pair, direction, slices, now) -> float:
        """Combined opportunistic risk multiplier (session × compression × DXY × news).

        Mirrors the live opportunistic plane's risk shaping. Each term is gated
        by its flag and degrades to a neutral 1.0 when its subsystem/data is
        absent, so a single-pair backtest with no USD-strength or news source
        still runs. Never raises — a faulty read contributes 1.0.
        """
        mult = 1.0
        # Session-aware sizing.
        if self.backtest_session_enabled and self.session_context is not None:
            try:
                session = self.session_context.get_session(now)
                s = float(self.session_context.get_session_multiplier(pair, session))
                if s > 0:
                    mult *= s
            except Exception as exc:  # noqa: BLE001
                logger.debug("[backtest] session size-mult failed for {}: {}", pair, exc)
        # Compression / expansion conviction boosts (applied as risk multipliers).
        if self.backtest_compression_enabled and self.compression_detector is not None:
            mult *= self._compression_risk_mult(pair)
        # DXY correlation penalty (skips gracefully when no USD-strength source).
        if self.backtest_dxy_enabled:
            mult *= self._backtest_dxy_mult(pair, direction, now)
        # News-window size-down (skips gracefully when no news events wired).
        if self.backtest_news_enabled:
            mult *= self._backtest_news_mult(pair, now)
        return mult

    def _compression_risk_mult(self, pair) -> float:
        """COMPRESSING → compression_conviction_boost, EXPANDING → expansion boost."""
        from brain.compression_detector import MarketState
        try:
            state = self.compression_detector.get_market_state(pair)
            from brain.instrument_profile import get_profile
            prof = get_profile(pair)
            if state == MarketState.COMPRESSING:
                return float(getattr(prof, "compression_conviction_boost", 1.2) or 1.0)
            if state == MarketState.EXPANDING:
                return float(getattr(prof, "expansion_conviction_boost", 1.5) or 1.0)
        except Exception as exc:  # noqa: BLE001
            logger.debug("[backtest] compression risk-mult failed for {}: {}", pair, exc)
        return 1.0

    def _backtest_dxy_mult(self, pair, direction, now) -> float:
        """DXY opposition penalty when a USD-strength source is available.

        Reads ``self.backtest_usd_strength(now) -> float`` (a signed USD trend;
        >0 = USD strengthening). For a USD-quoted symbol a strengthening USD
        pushes the pair down — opposing a LONG (and a weakening USD opposing a
        SHORT) — and applies ``dxy_opposition_penalty`` as a (1 - penalty) risk
        haircut. Returns 1.0 (skip gracefully) when no source is wired or the
        symbol is not USD-quoted.
        """
        src = getattr(self, "backtest_usd_strength", None)
        if src is None:
            return 1.0
        if "USD" not in str(pair).upper():
            return 1.0
        try:
            usd = float(src(now))
        except Exception as exc:  # noqa: BLE001
            logger.debug("[backtest] usd-strength read failed for {}: {}", pair, exc)
            return 1.0
        if usd == 0.0:
            return 1.0
        opposes = (usd > 0 and direction == "LONG") or (usd < 0 and direction == "SHORT")
        if not opposes:
            return 1.0
        from brain.instrument_profile import get_profile
        try:
            penalty = float(getattr(get_profile(pair), "dxy_opposition_penalty", 0.15) or 0.0)
        except Exception:  # noqa: BLE001
            penalty = 0.15
        return max(0.0, 1.0 - penalty)

    def _backtest_news_mult(self, pair, now) -> float:
        """News-window size-down when the backtest carries news-event timestamps.

        Returns ``news_risk_multiplier`` when ``now`` falls within
        ``news_pre_stage_minutes`` before (through ``news_post_event_cooldown_s``
        after) any configured event; 1.0 otherwise or when no events are wired
        (skip gracefully) — auto-size-down for the news window, live parity.
        """
        events = getattr(self, "backtest_news_events", None)
        if not events or now is None:
            return 1.0
        from brain.instrument_profile import get_profile
        try:
            prof = get_profile(pair)
        except Exception:  # noqa: BLE001
            prof = None
        pre = float(getattr(prof, "news_pre_stage_minutes", 5) if prof else 5)
        cooldown = float(getattr(prof, "news_post_event_cooldown_s", 300.0) if prof else 300.0)
        risk_mult = float(getattr(prof, "news_risk_multiplier", 0.5) if prof else 0.5)
        now_ts = self._as_utc(now)
        if now_ts is None:
            return 1.0
        for ev in events:
            et = self._as_utc(getattr(ev, "time_utc", ev))
            if et is None:
                continue
            secs = (now_ts - et).total_seconds()
            if -pre * 60.0 <= secs <= cooldown:
                return risk_mult
        return 1.0

    @staticmethod
    def _as_utc(value):
        from datetime import timezone as _tz
        if isinstance(value, datetime):
            return value if value.tzinfo is not None else value.replace(tzinfo=_tz.utc)
        try:
            ts = pd.Timestamp(value).to_pydatetime()
            return ts if ts.tzinfo is not None else ts.replace(tzinfo=_tz.utc)
        except Exception:  # noqa: BLE001
            return None

    @staticmethod
    def simulate_prestage_fill(direction, boundary, candle):
        """Simulate a pre-staged LIMIT fill at a zone boundary within a bar.

        Live pre-staging rests a LIMIT at the zone boundary, so when a bar's
        WICK reaches the boundary the order fills AT the boundary price — even if
        the bar CLOSES away from the zone (the "wick touches zone and bounces"
        setup the old M1-close backtest misses). Returns the boundary fill price
        when the candle's [low, high] straddles ``boundary``, else ``None``.
        """
        try:
            low = float(candle["low"])
            high = float(candle["high"])
            b = float(boundary)
        except Exception:  # noqa: BLE001
            return None
        if low <= b <= high:
            return b
        return None

    def _flip_param(self, pair, name, default):
        """Resolve a stop-out-flip tuning knob: profile → EntryConfig → default."""
        try:
            from brain.instrument_profile import get_profile
            val = getattr(get_profile(pair), name, None)
            if val is not None:
                return val
        except Exception:  # noqa: BLE001
            pass
        try:
            from entry.models import EntryConfig
            val = getattr(EntryConfig(), name, None)
            if val is not None:
                return val
        except Exception:  # noqa: BLE001
            pass
        return default

    @staticmethod
    def _m5_trend_dir(m5_df, lookback: int = 5) -> str:
        """Coarse M5 trend from the recent close delta ("LONG"/"SHORT"/"")."""
        try:
            closes = m5_df["close"]
            if len(closes) <= lookback:
                return ""
            delta = float(closes.iloc[-1]) - float(closes.iloc[-1 - lookback])
        except Exception:  # noqa: BLE001
            return ""
        if delta > 0:
            return "LONG"
        if delta < 0:
            return "SHORT"
        return ""

    def _bt_trend_str(self, df) -> str:
        """Map the coarse close-delta trend to a structure trend string.

        Returns "BULLISH"/"BEARISH" (or "UNKNOWN" when flat / unavailable) so
        the FlipConfirmer's non-opposition check reads the same vocabulary the
        live WorldModel structure reader produces.
        """
        d = self._m5_trend_dir(df) if df is not None else ""
        if d == "LONG":
            return "BULLISH"
        if d == "SHORT":
            return "BEARISH"
        return "UNKNOWN"

    @staticmethod
    def _bt_series_move(closes, lookback: int = 20) -> tuple[float, float]:
        """Return ``(net_price, efficiency)`` over the last ``lookback`` closes.

        ``net_price`` is the first→last displacement; ``efficiency`` is the
        net÷path directional-efficiency ratio in [-1, +1] — the bar-based analog
        of the live sub-candle tick_momentum, used only when the tick check is
        explicitly enabled in the replay.
        """
        try:
            vals = [float(c) for c in list(closes)[-(lookback + 1):]]
        except Exception:  # noqa: BLE001
            return 0.0, 0.0
        if len(vals) < 5:
            return 0.0, 0.0
        net = vals[-1] - vals[0]
        path = sum(abs(vals[i] - vals[i - 1]) for i in range(1, len(vals)))
        eff = 0.0 if path <= 0 else max(-1.0, min(1.0, net / path))
        return net, eff

    def _feed_flip_sequence(self, pair, slices, now) -> None:
        """Feed the M1/M5 bar replay into the flip sequence tracker.

        Called every replay bar (an M1 step). Arms the fast confirmation from
        the M1 slice for both directions on every bar; provides the slow
        confirmation from the M5 slice ONCE per new M5 bar (detected by the M5
        slice's last-bar time changing), advancing the tracker's M5 clock in
        lockstep with the live plane. No-op when the feature is off or the
        tracker failed to build. Never raises.
        """
        if not self.backtest_flip_sequence_enabled or self._bt_flip_sequence_tracker is None:
            return
        tracker = self._bt_flip_sequence_tracker
        try:
            m1_slice = slices.get("M1")
            if m1_slice is not None and len(m1_slice) >= 5:
                for direction in ("LONG", "SHORT"):
                    tracker.on_m1_close(pair, direction, m1_slice)

            m5_slice = slices.get("M5")
            if m5_slice is not None and len(m5_slice) >= 1:
                last_m5_time = m5_slice.iloc[-1].get("time")
                if last_m5_time != self._bt_last_m5_time.get(pair):
                    # A fresh M5 bar has closed — advance the slow timeframe once.
                    self._bt_last_m5_time[pair] = last_m5_time
                    m5_trend = self._bt_trend_str(m5_slice)
                    tracker.on_m5_close(pair, "", m5_trend)

            tracker.check_timeouts(pair)
        except Exception as exc:  # noqa: BLE001 — feeding never breaks the replay
            logger.debug("[backtest] flip sequence feed failed for {}: {}", pair, exc)

    def _bt_flip_confirmed(self, pair, new_dir, slices, now, setup) -> bool:
        """Run the hardened FlipConfirmer over the backtest bar slices.

        Feeds the stateless :class:`~entry.flip_confirmer.FlipConfirmer` the data
        it needs from the replay's bar slices — ATR (pips) from the M1 slice,
        the recent net move + efficiency from M1 closes, the M5/M15 structure
        trend from their slices, the M1 volume column, the original position's
        invalidation (its stop-loss) and the session at the bar's timestamp —
        and toggles each check via the ``backtest_flip_*`` flags. Returns True
        when the flip is confirmed. Never raises — any fault degrades to the
        original M5-only verdict via the confirmer's own graceful skips.
        """
        from types import SimpleNamespace

        from entry.flip_confirmer import FlipConfirmer

        m1_slice = slices.get("M1")
        pip_size = float(self.pip_size or 0.0)
        atr_pips = 0.0
        move_pips = 0.0
        tick_eff = 0.0
        if m1_slice is not None and "close" in getattr(m1_slice, "columns", []):
            try:
                from brain.volatility_stop import latest_atr

                atr_price = latest_atr(m1_slice, 14)
                if atr_price and pip_size > 0:
                    atr_pips = float(atr_price) / pip_size
                net, eff = self._bt_series_move(m1_slice["close"])
                sign = 1.0 if new_dir == "LONG" else -1.0
                move_pips = (net / pip_size) * sign if pip_size > 0 else 0.0
                tick_eff = eff * sign
            except Exception:  # noqa: BLE001
                atr_pips = move_pips = tick_eff = 0.0

        m5_trend = self._bt_trend_str(slices.get("M5"))
        m15_trend = self._bt_trend_str(slices.get("M15"))
        inv = float(getattr(setup, "stop_loss", 0.0) or 0.0)

        sess = None
        if self.session_context is not None and self.backtest_session_enabled:
            when = self._as_utc(now)
            sess = SimpleNamespace(
                get_session=lambda w=when: self.session_context.get_session(w)
            )

        checks_enabled = {
            "atr": self.backtest_flip_atr,
            "tick": self.backtest_flip_tick,
            "m5": True,
            "m15": self.backtest_flip_m15,
            "volume": self.backtest_flip_volume,
            "clean_break": self.backtest_flip_clean_break,
            # Tick-rule delta (Check 6) has no live tick stream in replay — leave
            # it disabled so it always skips. Sequence (Check 7) engages only
            # when the feature flag is set AND the tracker was fed.
            "delta": False,
            "sequence": self.backtest_flip_sequence_enabled,
        }

        # When the sequence check is enabled, force ``flip_require_sequence`` on
        # regardless of the profile default so the fed tracker actually gates the
        # flip; every other tuning knob still resolves via ``_flip_param``.
        def _flip_confirm_param(sym, name, default, _self=self):
            if name == "flip_require_sequence" and _self.backtest_flip_sequence_enabled:
                return True
            return _self._flip_param(sym, name, default)

        confirmer = FlipConfirmer(
            get_atr_pips=lambda s, tf, _a=atr_pips: _a,
            get_tick_momentum=lambda s, d, p, _t=tick_eff: _t,
            get_tick_move_pips=lambda s, d, p, _m=move_pips: _m,
            get_structure_trend=(
                lambda s, tf, _m5=m5_trend, _m15=m15_trend: (
                    {"M5": _m5, "M15": _m15}.get(tf, "UNKNOWN")
                )
            ),
            get_m1_dataframe=lambda s, _df=m1_slice: _df,
            sequence_tracker=self._bt_flip_sequence_tracker,
            session_context=sess,
            pip_size_lookup=lambda s, _p=pip_size: _p,
            profile_param=_flip_confirm_param,
        )
        result = confirmer.confirm(
            pair, new_dir, mode="flip",
            invalidation_level=inv, checks_enabled=checks_enabled,
        )
        if not result.confirmed:
            logger.debug(
                "[backtest] flip {}→{} rejected ({}) checks={}",
                pair, new_dir, result.reason, result.checks,
            )
        return result.confirmed

    def _maybe_stopout_flip(
        self, closed_trade, close_event, slices, pair, now, balance,
    ):
        """Evaluate + open an opposite-direction flip after a stop-out.

        Mirrors the live orchestrator's ``evaluate_stopout_flip`` (called from
        ``_on_trade_closed`` on a stop-loss fill): flip into the opposite
        direction, mirror the SL/TP around the flip entry, and open it on the
        SAME bar — subject to the same guards (per-symbol cooldown, per-zone
        whipsaw cap) and confirmation (the M5 trend must not oppose the flip).
        Returns the new open-trade dict, or ``None`` when a guard blocks it or
        the flip is unconfirmed. Never raises.
        """
        try:
            if not bool(self._flip_param(pair, "stopout_flip_enabled", True)):
                return None

            setup = closed_trade.get("setup")
            closed_dir = str(getattr(setup, "direction", "") or "").upper()
            if closed_dir not in ("LONG", "SHORT"):
                return None
            new_dir = "SHORT" if closed_dir == "LONG" else "LONG"

            # Hardened flip confirmation (entry/flip_confirmer.py) — live
            # parity. Replaces the bare "M5 trend must not oppose" check with
            # the full five-check FlipConfirmer fed from the bar slices. M5
            # non-opposition is always on (the original behaviour); the ATR
            # magnitude, M15 non-opposition, volume and clean-break checks each
            # engage only when their backtest_flip_* flag is set AND the data is
            # present, degrading to a graceful skip otherwise.
            if not self._bt_flip_confirmed(pair, new_dir, slices, now, setup):
                return None

            now_utc = self._as_utc(now)
            state = self._bt_flip_state.setdefault(
                pair, {"last_ts": None, "zone_counts": {}},
            )
            # Cooldown guard.
            cooldown_s = float(self._flip_param(pair, "stopout_flip_cooldown_s", 30.0))
            last_ts = state.get("last_ts")
            if last_ts is not None and now_utc is not None:
                if (now_utc - last_ts).total_seconds() < cooldown_s:
                    return None

            # Per-zone whipsaw cap.
            entry = float(closed_trade.get("entry_price", 0.0) or 0.0)
            risk = float(closed_trade.get("risk", 0.0) or 0.0)
            if entry <= 0 or risk <= 0:
                return None
            zone_type = str(getattr(setup, "zone_type", "") or "")
            zkey = f"{zone_type}|{round(entry, 5)}"
            max_per_zone = int(self._flip_param(pair, "stopout_flip_max_per_zone", 2))
            counts = state["zone_counts"]
            if counts.get(zkey, 0) >= max_per_zone:
                return None

            # Flip enters at the stopped-out level (≈ current price); SL/TP
            # mirror around the flip entry preserving the risk distance.
            flip_entry = float(closed_trade.get("stop_loss", entry) or entry)
            if new_dir == "LONG":
                flip_sl = flip_entry - risk
                tp1 = flip_entry + risk * 1.5
                tp2 = flip_entry + risk * 3.0
            else:
                flip_sl = flip_entry + risk
                tp1 = flip_entry - risk * 1.5
                tp2 = flip_entry - risk * 3.0

            flip_setup = BacktestSetup(
                direction=new_dir,
                entry_price=flip_entry,
                stop_loss=flip_sl,
                tp1=tp1,
                tp2=tp2,
                score=int(getattr(setup, "score", 0) or 0),
                regime=str(getattr(setup, "regime", "") or ""),
                timestamp=now,
                entry_type="STOPOUT_FLIP",
                zone_type=zone_type,
                lots=float(closed_trade.get("lots", 0.0) or 0.0),
                pip_value_per_lot=self.pip_value_per_lot,
                source="stopout_flip",
            )
            flip_trade = self._open_trade(flip_setup, now)
            flip_trade["symbol"] = pair

            state["last_ts"] = now_utc
            counts[zkey] = counts.get(zkey, 0) + 1
            logger.info(
                "[backtest] stop-out flip {}→{} on {} @ {:.5f} (risk {:.5f})",
                closed_dir, new_dir, pair, flip_entry, risk,
            )
            return flip_trade
        except Exception as exc:  # noqa: BLE001
            logger.debug("[backtest] stop-out flip failed for {}: {}", pair, exc)
            return None

    def _orchestrator_size_mult(
        self, pair: str, direction: str, wm, scan_score: float, de_conviction: float,
    ) -> tuple[float, bool]:
        """Grade the entry through the Orchestrator round table.

        Returns ``(size_multiplier, vetoed)``. Fail-safe — a missing engine or
        any fault is a neutral ``1.0``, never a veto. Mirrors
        ``event_driven_bootstrap`` ~8182-8286.
        """
        engine = getattr(self, "bt_orchestrator", None)
        if engine is None:
            return 1.0, False
        try:
            from brain.orchestrator import TradeProposal

            want_dir = "LONG" if str(direction).upper() in ("BUY", "LONG") else "SHORT"
            structure = wm.structure_by_tf() if wm is not None else {}
            h4_sa = structure.get("H4")
            h4_alignment = (
                float(getattr(h4_sa, "confidence", 0.0) or 0.0)
                if h4_sa is not None else None
            )

            # Ranker-EV dimension is gated by ``orchestrator.use_ranker_ev``
            # (default off), mirroring live so sizing is unchanged until opted in.
            ranker_ev = ranker_coherence = ranker_confidence = None
            candidate_count = 0
            try:
                _orch_cfg = getattr(self.config, "orchestrator", None)
                _use_ranker_ev = bool(getattr(_orch_cfg, "use_ranker_ev", False))
            except Exception:  # noqa: BLE001
                _use_ranker_ev = False
            if _use_ranker_ev and wm is not None:
                try:
                    cands = (
                        wm.candidates_list()
                        if hasattr(wm, "candidates_list")
                        else list(getattr(wm, "candidates", ()) or [])
                    )
                    candidate_count = len(cands)
                    matching = [
                        c for c in cands
                        if str(getattr(c, "direction", "")).upper() == want_dir
                    ]
                    if matching:
                        best = max(
                            matching,
                            key=lambda c: float(getattr(c, "expected_value", 0.0) or 0.0),
                        )
                        ranker_ev = float(getattr(best, "expected_value", 0.0) or 0.0)
                        ranker_coherence = float(getattr(best, "coherence", 0.0) or 0.0)
                        ranker_confidence = float(getattr(best, "confidence", 0.0) or 0.0)
                except Exception as exc:  # noqa: BLE001
                    logger.debug("[backtest] orch candidate EV select failed: {}", exc)

            proposal = TradeProposal(
                pair=pair,
                direction=want_dir,
                scan_score=float(scan_score),
                de_conviction=de_conviction if de_conviction > 0 else None,
                de_margin=None,
                tf_alignment=h4_alignment,
                ranker_ev=ranker_ev,
                ranker_coherence=ranker_coherence,
                ranker_confidence=ranker_confidence,
                candidate_count=candidate_count,
            )
            verdict = engine.evaluate(proposal)
            if getattr(verdict, "vetoed", False):
                logger.debug(
                    "[backtest] {} {} orchestrator veto: {}",
                    pair, want_dir, getattr(verdict, "veto_reason", ""),
                )
                return 0.0, True
            return float(getattr(verdict, "size_multiplier", 1.0) or 1.0), False
        except Exception as exc:  # noqa: BLE001
            logger.debug("[backtest] orchestrator eval failed for {}: {}", pair, exc)
            return 1.0, False

    def _adaptive_size_mult(
        self, pair: str, wm, now: Optional[datetime], zone_type: str,
    ) -> tuple[float, bool]:
        """Apply the AdaptiveOptimizer learned edge.

        Returns ``(size_multiplier, blocked)``. Blocks (``True``) on a confirmed
        losing pattern or an optimizer AVOID veto; otherwise returns the learned
        position-size multiplier. Fail-safe — a missing adapter or any fault is a
        neutral ``1.0``, never blocked. The live governance approval step is a
        no-op in the backtest (auto-approved, matching live's Phase-7 default).
        Mirrors ``event_driven_bootstrap`` ~8288-8389.
        """
        adapter = getattr(self, "bt_ml_adapter", None)
        if adapter is None:
            return 1.0, False
        try:
            regime = "UNKNOWN"
            if wm is not None:
                try:
                    rbtf = wm.regime_by_tf()
                    regime = str(
                        rbtf.get("H1") or rbtf.get("H4")
                        or next(iter(rbtf.values()), "UNKNOWN")
                    )
                except Exception:  # noqa: BLE001
                    regime = "UNKNOWN"
            session = "UNKNOWN"
            if self.session_engine is not None and now is not None:
                try:
                    ss = self.session_engine.get_status(now)
                    session = str(
                        getattr(ss, "name", "")
                        or getattr(ss, "current_session", "")
                        or "UNKNOWN"
                    )
                except Exception:  # noqa: BLE001
                    session = "UNKNOWN"

            block_enabled = getattr(
                getattr(self.config, "risk", None),
                "losing_pattern_block_enabled", True,
            )
            if block_enabled:
                is_loser, loser_reason = adapter.is_losing_pattern(
                    pair, regime, session, zone_type,
                )
                if is_loser:
                    logger.debug(
                        "[backtest] {} entry blocked — losing pattern: {}",
                        pair, loser_reason,
                    )
                    return 1.0, True

            adj = adapter.get_trade_adjustments(pair, regime, session)
            if not getattr(adj, "should_trade", True):
                logger.debug(
                    "[backtest] {} entry blocked — optimizer AVOID: {}",
                    pair, getattr(adj, "reason", ""),
                )
                return 1.0, True
            return float(getattr(adj, "position_size_multiplier", 1.0) or 1.0), False
        except Exception as exc:  # noqa: BLE001
            logger.debug("[backtest] adaptive optimizer adjust failed for {}: {}", pair, exc)
            return 1.0, False

    def _capital_alloc_mult(self, wm) -> float:
        """Capital-allocation sizing multiplier for this trade's fingerprint.

        Mirrors ``event_driven_bootstrap`` ~8422-8454. Best-effort neutral 1.0.
        """
        allocator = getattr(self, "bt_capital_allocator", None)
        if allocator is None:
            return 1.0
        try:
            from adaptive.capital_allocator import compute_fingerprint

            regime_str = ""
            if wm is not None:
                try:
                    rbtf = wm.regime_by_tf()
                    regime_str = str(
                        rbtf.get("H1") or rbtf.get("H4")
                        or next(iter(rbtf.values()), "") or ""
                    )
                except Exception:  # noqa: BLE001
                    regime_str = ""
            fp = compute_fingerprint(horizon="SWING", extra=regime_str)
            return float(allocator.get_sizing_multiplier(fp) or 1.0)
        except Exception as exc:  # noqa: BLE001
            logger.debug("[backtest] capital-alloc size-mult failed: {}", exc)
            return 1.0

    def _instrument_vol_mult(self, slices: dict[str, pd.DataFrame]) -> float:
        """Per-instrument volatility multiplier from current vs average M5 ATR.

        Mirrors ``event_driven_bootstrap`` ~8616-8631 — reuses the shared
        PositionSizer volatility ramp so the same [0.5, 1.5] mapping applies.
        Best-effort neutral 1.0.
        """
        sizer = getattr(self, "position_sizer", None)
        if sizer is None:
            return 1.0
        try:
            vdf = slices.get("M5") if slices else None
            if vdf is not None and len(vdf) >= 20:
                tr = (vdf["high"] - vdf["low"]).abs()
                cur_atr = float(tr.tail(14).mean())
                avg_atr = float(tr.tail(50).mean())
                return float(sizer.adjust_for_volatility(1.0, cur_atr, avg_atr))
        except Exception as exc:  # noqa: BLE001
            logger.debug("[backtest] per-instrument vol-mult failed: {}", exc)
        return 1.0

    def _opportunity_quality_mult(
        self, wm, direction: str, score: float,
    ) -> float:
        """Opportunity-quality-proportional sizing multiplier (GAP 3 parity).

        Mirrors ``event_driven_bootstrap`` ~8551-8582. Identity ``1.0`` when the
        sizer is disabled/absent (the default). Sources EV/confidence from the
        best matching WorldModel candidate; falls back to the zone conviction as
        confidence. Best-effort.
        """
        sizer = getattr(self, "bt_oq_sizer", None)
        if sizer is None or not getattr(sizer, "enabled", False):
            return 1.0
        try:
            q_ev = 0.0
            q_conf = 0.0
            if wm is not None:
                try:
                    want = str(direction).upper()
                    cands = (
                        wm.candidates_list()
                        if hasattr(wm, "candidates_list")
                        else list(getattr(wm, "candidates", ()) or [])
                    )
                    best = None
                    for opp in cands:
                        if str(getattr(opp, "direction", "")).upper() != want:
                            continue
                        if best is None or float(
                            getattr(opp, "expected_value", 0.0) or 0.0
                        ) > float(getattr(best, "expected_value", 0.0) or 0.0):
                            best = opp
                    if best is not None:
                        q_ev = float(getattr(best, "expected_value", 0.0) or 0.0)
                        q_conf = float(getattr(best, "confidence", 0.0) or 0.0)
                except Exception:  # noqa: BLE001
                    q_ev, q_conf = 0.0, 0.0
            if q_conf <= 0.0:
                q_conf = float(score or 0.0) / 100.0
            return float(
                sizer.multiplier(ev=q_ev, confidence=q_conf, rank=None, rank_total=None)
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("[backtest] opportunity-quality sizer failed: {}", exc)
            return 1.0

    def _size_trade(
        self,
        pair: str,
        direction: str,
        signal,
        factors,
        conviction: float,
        balance: float,
        existing: Optional[list] = None,
    ):
        """Size the trade through PortfolioDivision → PositionSizer (live path).

        ``factors`` is the fully-composed :class:`SizingFactors` bundle (base
        risk + the entire graded multiplier chain) built by
        ``_build_sizing_factors`` — mirroring the live plane, which folds the
        whole chain into a single ``PortfolioDivision.evaluate`` call.

        Returns ``(lots, max_loss, risk_amount)`` or ``None`` when the portfolio
        rejects the trade or sizes it to zero.

        ``daily_pnl`` (realized $ so far today) and ``daily_loss_cap_pct`` (the
        live governor cap) are fed in so PortfolioDivision's daily-loss throttle
        actually binds — previously both arrived as 0.0, neutering it.
        ``existing`` is the list of currently-open positions (empty in the
        single-position serial model, since sizing only happens with no open
        trade — matching reality).
        """
        try:
            from portfolio.models import PortfolioCandidate, PortfolioAccount
            candidate = PortfolioCandidate(
                symbol=pair,
                direction=direction,
                entry_price=signal.entry_price,
                stop_loss=signal.stop_loss,
                conviction=float(conviction or 0.0),
                context=None,
                pip_size=self.pip_size,
                pip_value_per_lot=self.pip_value_per_lot,
            )
            daily_cap = 0.0
            gov = getattr(self.config, "governor", None)
            if gov is not None:
                daily_cap = float(getattr(gov, "daily_loss_cap_pct", 0.0) or 0.0)
            account = PortfolioAccount(
                balance=balance or 0.0,
                account_key="backtest",
                daily_pnl=float(getattr(self, "_bt_daily_pnl", 0.0) or 0.0),
                daily_loss_cap_pct=daily_cap,
            )
            verdict = self.portfolio.evaluate(
                candidate, list(existing or []), account, factors,
            )
        except Exception as exc:
            logger.warning("[backtest] portfolio sizing failed for {}: {}", pair, exc)
            return None

        if not getattr(verdict, "approved", False):
            logger.debug("[backtest] {} {} portfolio rejected: {}", pair, direction,
                         getattr(verdict, "reason", ""))
            return None
        lots = float(getattr(verdict, "lots", 0.0) or 0.0)
        if lots <= 0:
            return None
        max_loss = float(getattr(verdict, "max_loss", 0.0) or 0.0)
        risk_amount = round((balance or 0.0) * float(getattr(verdict, "risk_pct", 0.0) or 0.0), 2)
        if max_loss <= 0:
            max_loss = round(lots * (abs(signal.entry_price - signal.stop_loss) / self.pip_size)
                             * self.pip_value_per_lot, 2)
        return lots, max_loss, risk_amount

    def _update_system_volatility(self, slices: dict[str, pd.DataFrame]) -> None:
        """Feed the SystemVolatilityMonitor from this bar's H1 regime analysis.

        Single-symbol replay supplies one :class:`RegimeAnalysis` per bar — the
        monitor's percent-of-pairs thresholds therefore collapse to "this pair
        spiking or not", which is the best available cross-instrument proxy in a
        per-symbol backtest. Best-effort; never raises.
        """
        if self.system_volatility_monitor is None:
            return
        try:
            from brain.quality_layer import compute_regime_analysis

            ra = compute_regime_analysis(slices.get("H1"))
            self.system_volatility_monitor.update([ra] if ra is not None else [])
        except Exception as exc:
            logger.debug("[backtest] system volatility update failed: {}", exc)

    def _micro_from_slice(self, m1_df, is_long: bool) -> dict:
        """M1 alignment count + micro-structure event/trend from a candle slice.

        Mirrors the live ``_management_micro_context`` (last-5 closed-candle
        alignment + ``StructureEngine`` micro read) but sources candles from the
        replay slice instead of a broker fetch.
        """
        out = {"m1_aligned_count": 0, "m1_event": "NONE", "m1_trend": "UNKNOWN", "m1_pattern": ""}
        if m1_df is None or len(m1_df) < 5:
            return out
        try:
            last5 = m1_df.iloc[-5:]
            closes = last5["close"].values
            opens = last5["open"].values
            if is_long:
                aligned = sum(1 for c, o in zip(closes, opens) if c > o)
            else:
                aligned = sum(1 for c, o in zip(closes, opens) if c < o)
            out["m1_aligned_count"] = int(aligned)
        except Exception:
            return out
        try:
            from entry.m1_patterns import detect_m1_pattern
            out["m1_pattern"] = detect_m1_pattern(m1_df, is_long)
        except Exception:
            out["m1_pattern"] = ""
        try:
            from brain.structure_engine import StructureEngine
            engine = StructureEngine(swing_lookback=3, pip_size=self.pip_size)
            analysis = engine.analyze(m1_df.iloc[-min(len(m1_df), 100):])
            out["m1_event"] = analysis.last_event.value
            out["m1_trend"] = analysis.trend.value
        except Exception as exc:
            logger.debug("[backtest] M1 micro-structure read failed: {}", exc)
        return out

    def _open_trade(
        self, setup: BacktestSetup, now: datetime, entry_candle=None,
    ) -> dict:
        slippage_distance = self.slippage_pips * self.pip_size

        # Pre-staged LIMIT fill (Phase 3 Feature B): a zone entry that rested a
        # LIMIT at the boundary fills AT the boundary with NO adverse slippage
        # when the bar's wick reached it — even if the bar closed away. Only for
        # zone-sourced entries with the entry candle available; every other
        # entry keeps the market-fill slippage model.
        actual_entry = None
        prestage_filled = False
        if (
            self.backtest_prestaging_enabled
            and entry_candle is not None
            and str(getattr(setup, "source", "")) == "zone"
        ):
            fill = self.simulate_prestage_fill(
                setup.direction, setup.entry_price, entry_candle,
            )
            if fill is not None:
                actual_entry = float(fill)
                prestage_filled = True

        if actual_entry is None:
            if setup.direction == "LONG":
                actual_entry = setup.entry_price + slippage_distance
            else:
                actual_entry = setup.entry_price - slippage_distance

        risk = abs(actual_entry - setup.stop_loss)
        if risk <= 0:
            risk = 8 * self.pip_size

        return {
            "setup": setup,
            "symbol": "",
            "order_id": f"bt-{int(pd.Timestamp(now).value)}",
            "entry_time": now,
            "entry_price": actual_entry,
            "stop_loss": setup.stop_loss,
            "tp1": setup.tp1,
            "tp2": setup.tp2,
            "risk": risk,
            "tp1_hit": False,
            "at_breakeven": False,
            "realized_r": 0.0,
            "session": self.session_engine.get_status(now).current_session,
            "entry_type": self._resolve_entry_type(setup),
            "slippage_cost": 0.0 if prestage_filled else slippage_distance,
            # ── Live-parity sizing + management state ────────────────────
            "lots": float(getattr(setup, "lots", 0.0) or 0.0),
            "max_loss": float(getattr(setup, "max_loss", 0.0) or 0.0),
            "risk_amount": float(getattr(setup, "risk_amount", 0.0) or 0.0),
            "fast_opp": 0,
            # Rolling per-trade score history (zone conviction each management
            # cycle) — feeds the DecisionEngine momentum-trajectory + confidence
            # + thesis-deterioration reads, exactly as live's mgmt.score_history.
            "score_history": [],
            # Entry-time OQ/EQ baseline for live oq_decay/eq_decay scoring.
            "entry_oq": getattr(setup, "entry_oq", None),
            "entry_eq": getattr(setup, "entry_eq", None),
            # Management lifecycle flags the live TradeContext carries.
            "partial_closed": False,
            "trailing": False,
            # Fraction of the position still open (1.0 = full). A management
            # PARTIAL_CLOSE banks part of it; geometry TP1/TP2 fractions compose
            # with this so total banked R never exceeds the realised position.
            "remaining_fraction": 1.0,
        }

    def _evaluate_trade(
        self,
        trade: dict,
        candle: pd.Series,
        *,
        slices: Optional[dict[str, pd.DataFrame]] = None,
        pair: str = "",
        now: Optional[datetime] = None,
        manage: bool = False,
    ) -> Optional[dict]:
        """Evaluate one open trade against a candle.

        Broker mechanics first (SL/TP detection from OHLC — the acceptable
        divergence). When ``manage`` and not legacy, the live management engines
        (SituationEngine → DecisionEngine → RiskGovernor) run on a fresh
        WorldModel each bar and may CLOSE / SECURE / tighten — exactly as the
        live plane re-validates the thesis on every cycle.
        """
        geom = self._evaluate_geometry(trade, candle)
        if geom is not None:
            return geom

        legacy = getattr(self, "legacy_mode", False)
        managed = (
            manage
            and not legacy
            and getattr(self, "decision_engine", None) is not None
            and slices is not None
        )
        if managed:
            de_close = self._run_management(trade, candle, slices, pair, now)
            if de_close is not None:
                return de_close

        # Legacy / unmanaged time stop (live management replaces this when on).
        if legacy or not manage:
            hold_minutes = (
                pd.Timestamp(candle["time"]).to_pydatetime() - trade["entry_time"]
            ).total_seconds() / 60.0
            if hold_minutes >= 180:
                return self._time_close(trade, candle, hold_minutes)

        return None

    def _evaluate_geometry(self, trade: dict, candle: pd.Series) -> Optional[dict]:
        """Broker/tick-mechanics SL/TP detection from candle OHLC (no time stop).

        The TP1 partial (``_TP1_PARTIAL_RATIO`` off, stop→breakeven) models the
        live ``execution.position_worker`` tick mechanics (``_check_tp1`` /
        ``_check_breakeven``) — it is a faithful broker-mechanics simulation, not
        a parallel strategy. Banking uses ``remaining_fraction`` so any prior
        management PARTIAL_CLOSE composes correctly with the TP1 partial.
        """
        direction = trade["setup"].direction
        entry = trade["entry_price"]
        stop = trade["stop_loss"]
        tp1 = trade["tp1"]
        tp2 = trade["tp2"]
        risk = trade["risk"]
        hold_minutes = (
            pd.Timestamp(candle["time"]).to_pydatetime() - trade["entry_time"]
        ).total_seconds() / 60.0
        remaining = float(trade.get("remaining_fraction", 1.0) or 0.0)

        if direction == "LONG":
            low_hit = candle["low"] <= stop
            tp1_hit = candle["high"] >= tp1
            tp2_hit = candle["high"] >= tp2

            if not trade["tp1_hit"]:
                if low_hit:
                    return {
                        "pnl_r": remaining * -1.0,
                        "hold_minutes": hold_minutes,
                        "outcome": "LOSS",
                    }
                if tp1_hit:
                    self._bank_tp1_partial(trade, (tp1 - entry) / risk, entry)
                    return None

            if trade["tp1_hit"]:
                stop_be_hit = candle["low"] <= trade["stop_loss"]
                if stop_be_hit:
                    pnl = trade["realized_r"] + remaining * ((trade["stop_loss"] - entry) / risk)
                    return {
                        "pnl_r": pnl,
                        "hold_minutes": hold_minutes,
                        "outcome": "BREAKEVEN" if abs(pnl) < 1e-9 or pnl <= trade["realized_r"] else "WIN",
                    }
                if tp2_hit:
                    pnl = trade["realized_r"] + remaining * ((tp2 - entry) / risk)
                    return {
                        "pnl_r": pnl,
                        "hold_minutes": hold_minutes,
                        "outcome": "WIN",
                    }

        else:
            high_hit = candle["high"] >= stop
            tp1_hit = candle["low"] <= tp1
            tp2_hit = candle["low"] <= tp2

            if not trade["tp1_hit"]:
                if high_hit:
                    return {
                        "pnl_r": remaining * -1.0,
                        "hold_minutes": hold_minutes,
                        "outcome": "LOSS",
                    }
                if tp1_hit:
                    self._bank_tp1_partial(trade, (entry - tp1) / risk, entry)
                    return None

            if trade["tp1_hit"]:
                stop_be_hit = candle["high"] >= trade["stop_loss"]
                if stop_be_hit:
                    pnl = trade["realized_r"] + remaining * ((entry - trade["stop_loss"]) / risk)
                    return {
                        "pnl_r": pnl,
                        "hold_minutes": hold_minutes,
                        "outcome": "BREAKEVEN" if abs(pnl) < 1e-9 or pnl <= trade["realized_r"] else "WIN",
                    }
                if tp2_hit:
                    pnl = trade["realized_r"] + remaining * ((entry - tp2) / risk)
                    return {
                        "pnl_r": pnl,
                        "hold_minutes": hold_minutes,
                        "outcome": "WIN",
                    }

        return None

    def _bank_tp1_partial(self, trade: dict, tp1_r: float, entry: float) -> None:
        """Bank the TP1 partial and move the stop to breakeven.

        Mirrors ``execution.position_worker`` tick mechanics: take
        ``_TP1_PARTIAL_RATIO`` of the still-open fraction at TP1, credit the
        realized R, reduce open lots, and snap the stop to entry (breakeven).
        """
        remaining = float(trade.get("remaining_fraction", 1.0) or 0.0)
        banked = _TP1_PARTIAL_RATIO * remaining
        trade["tp1_hit"] = True
        trade["stop_loss"] = entry
        trade["at_breakeven"] = True
        trade["partial_closed"] = True
        trade["realized_r"] += banked * tp1_r
        trade["remaining_fraction"] = max(0.0, remaining - banked)
        trade["lots"] = float(trade.get("lots", 0.0) or 0.0) * (1.0 - _TP1_PARTIAL_RATIO)

    def _run_management(
        self,
        trade: dict,
        candle: pd.Series,
        slices: dict[str, pd.DataFrame],
        pair: str,
        now: Optional[datetime],
    ) -> Optional[dict]:
        """Live thesis re-validation for one open trade on one bar.

        Rebuilds a fresh WorldModel (so HTF structure + live bias update on the
        trade's timescale), builds a TradeContext, runs the SAME
        SituationEngine → DecisionEngine → RiskGovernor path the live plane
        uses, and acts on the verdict (CLOSE / SECURE / tighten). Returns a
        close-event dict when the engine closes the trade, else None.
        """
        h4 = slices.get("H4")
        h1 = slices.get("H1")
        m15 = slices.get("M15")
        m5 = slices.get("M5")
        if h4 is None or h1 is None or m15 is None or m5 is None:
            return None
        try:
            from brain.decision_core import analyze_window
            wm = analyze_window(
                pair,
                {"H4": h4, "H1": h1, "M15": m15, "M5": m5},
                timestamp=now,
                consensus_config=getattr(self.config, "consensus", None),
            )
        except Exception as exc:
            logger.debug("[backtest] management analyze_window failed for {}: {}", pair, exc)
            return None

        try:
            ctx = self._build_trade_context(trade, candle, wm, slices, pair)
            sa = self.situation_engine.assess_open_trade(ctx)
            de = self.decision_engine.decide_management(ctx, sa)
            try:
                de = self.risk_governor.review(de, ctx, sa)
            except Exception as exc:
                logger.debug("[backtest] governor management review failed: {}", exc)
        except Exception as exc:
            logger.debug("[backtest] management decision failed for {}: {}", pair, exc)
            return None

        # Update the fast-opposition streak for the NEXT cycle (corrected sign).
        self._update_fast_opposition(trade, sa)

        from decision.actions import Action

        action = de.action
        if action == Action.CLOSE:
            return self._de_close(trade, candle, de)
        if action in (Action.TIGHTEN_SL, Action.SET_PROTECTIVE_STOP, Action.MOVE_TO_BREAKEVEN):
            new_sl = de.new_sl
            if action == Action.MOVE_TO_BREAKEVEN and (new_sl is None or new_sl <= 0):
                new_sl = trade["entry_price"]
            if new_sl and new_sl > 0:
                trade["stop_loss"] = float(new_sl)
                if action == Action.MOVE_TO_BREAKEVEN:
                    trade["at_breakeven"] = True
        elif action == Action.PARTIAL_CLOSE:
            # Bank part of the open remainder at the current close — mirrors the
            # live ``_partial_close_position`` (same default ratio / clamps). The
            # banked R is added to realized_r and the open fraction shrinks so the
            # geometry TP1/TP2 fractions only ever bank what is still open.
            self._de_partial_close(trade, candle, de)
        elif action == Action.SCALE_IN:
            # Accepted simplification: the replay is single-position-serial, so it
            # cannot pyramid an add-on the way the live plane does (live routes a
            # scale-in through Compliance → Portfolio as a duplicate). Log and
            # hold the base position rather than silently dropping the verdict.
            logger.debug(
                "[backtest] {} SCALE_IN verdict not modelled (single-position serial replay)",
                pair,
            )

        # ── PortfolioRisk DEFENSIVE → advance eligible position to breakeven ──
        # Live parity with ``_check_portfolio_heat`` DEFENSIVE: when the
        # portfolio is defensive, a position that has proven favorable excursion
        # (or already banked TP1 / a partial) has its stop advanced to
        # breakeven — but only once price has cleared the BE level, so the stop
        # never lands on the wrong side of market. Runs after the DE verdict so
        # an explicit management BE/tighten still wins.
        self._apply_defensive_breakeven(trade, candle)
        return None

    def _apply_defensive_breakeven(self, trade: dict, candle: pd.Series) -> None:
        """Advance an eligible position to breakeven while PortfolioRisk DEFENSIVE.

        Mirrors the live heat-monitor DEFENSIVE arm. No-op unless the state
        machine is DEFENSIVE, the position is eligible (favorable excursion ≥ 1R
        or already TP1/partial-banked) and price has cleared the BE level. Only
        ever tightens the stop toward breakeven — never loosens it. Best-effort.
        """
        sm = getattr(self, "portfolio_risk_sm", None)
        if sm is None:
            return
        try:
            from risk.portfolio_risk_state import (
                PortfolioRiskState,
                is_eligible_for_defensive_breakeven,
            )
            if sm.state != PortfolioRiskState.DEFENSIVE:
                return
            if trade.get("at_breakeven", False):
                return
            direction = trade["setup"].direction
            is_long = str(direction).upper() in ("BUY", "LONG")
            entry = float(trade["entry_price"])
            sl = float(trade["stop_loss"])
            price = float(candle["close"])
            if not is_eligible_for_defensive_breakeven(
                direction=direction,
                entry_price=entry,
                sl=sl,
                current_price=price,
                tp1_hit=bool(trade.get("tp1_hit", False)),
                partial_closed=bool(trade.get("partial_closed", False)),
                at_breakeven=bool(trade.get("at_breakeven", False)),
            ):
                return
            be_price = (
                entry + (2 * self.pip_size) if is_long else entry - (2 * self.pip_size)
            )
            # Only move once price has cleared BE (else the stop lands the wrong
            # side of market → instant stop-out).
            can_be = (price > be_price) if is_long else (price < be_price)
            if not can_be:
                return
            improves = (be_price > sl) if is_long else (be_price < sl)
            if not improves:
                # Stop already at/beyond breakeven — nothing to tighten.
                trade["at_breakeven"] = True
                return
            trade["stop_loss"] = be_price
            trade["at_breakeven"] = True
            logger.debug(
                "[backtest] {} DEFENSIVE breakeven advance → SL {:.5f}",
                trade.get("symbol", ""), be_price,
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("[backtest] defensive breakeven failed: {}", exc)

    def _de_partial_close(self, trade: dict, candle: pd.Series, de) -> None:
        """Bank a fraction of the open remainder on a PARTIAL_CLOSE verdict."""
        ratio = float(getattr(de, "partial_ratio", 0.0) or 0.0)
        if ratio <= 0.0:
            ratio = 0.5  # mirrors live _DEFAULT_PARTIAL_CLOSE_RATIO
        ratio = max(0.05, min(0.95, ratio))
        remaining = float(trade.get("remaining_fraction", 1.0) or 1.0)
        if remaining <= 0.0:
            return
        direction = trade["setup"].direction
        entry = trade["entry_price"]
        close_price = float(candle["close"])
        risk = trade["risk"]
        if direction == "LONG":
            r_now = (close_price - entry) / risk
        else:
            r_now = (entry - close_price) / risk
        banked_fraction = remaining * ratio
        trade["realized_r"] += banked_fraction * r_now
        trade["remaining_fraction"] = remaining - banked_fraction
        trade["partial_closed"] = True

    def _build_trade_context(
        self,
        trade: dict,
        candle: pd.Series,
        wm,
        slices: dict[str, pd.DataFrame],
        pair: str,
    ):
        """Build the TradeContext the live management DecisionEngine reads.

        Mirrors ``event_driven_bootstrap._run_decision_engine_management`` field
        for field, sourced from candle/WorldModel data instead of live ticks so
        the SituationEngine/DecisionEngine see the SAME inputs they do live:
        M5 fast structure, H1 last-candle context, the live consensus panel,
        live OQ/EQ decay, rolling score history and session state. The only
        accepted simplifications are the broker-tick-only feeds the replay has
        no source for (news timing, intra-bar P&L precision); each is flagged.
        ``scan_direction`` comes from the live WorldModel bias (not the trade's
        own direction) so the opposing-bias CLOSE term can actually fire.
        """
        from decision.context import TradeContext

        setup = trade["setup"]
        direction = setup.direction
        norm_dir = "BUY" if direction.upper() in ("BUY", "LONG") else "SELL"
        is_long = norm_dir == "BUY"

        entry = trade["entry_price"]
        price = float(candle["close"])
        sl = trade["stop_loss"]
        if is_long:
            pnl_pips = (price - entry) / self.pip_size
        else:
            pnl_pips = (entry - price) / self.pip_size
        risk_pips = trade["risk"] / self.pip_size if self.pip_size else 0.0
        # Dollar P&L on the OPEN remainder — mirrors live ``pnl_dollars`` (read
        # by the thesis-secure path). pip P&L × pip-value × open lots × the
        # still-open fraction, so a half-closed position reports the P&L of the
        # remaining half rather than the full position.
        lots = float(trade.get("lots", 0.0) or 0.0)
        remaining_fraction = float(trade.get("remaining_fraction", 1.0) or 1.0)
        pnl_dollars = pnl_pips * self.pip_value_per_lot * lots * remaining_fraction
        hold_minutes = (
            pd.Timestamp(candle["time"]).to_pydatetime() - trade["entry_time"]
        ).total_seconds() / 60.0

        structure = wm.structure_by_tf()
        d1_trend, d1_conf = _struct_trend_conf(structure, "D1")
        h4_trend, h4_conf = _struct_trend_conf(structure, "H4")
        h1_trend, h1_conf = _struct_trend_conf(structure, "H1")
        # M5 fast structural feed — refreshes every 5 min (in TF_MODULE_MAP),
        # un-freezing tf_alignment / structure_integrity between the slower
        # H1/H4/D1 closes. This is the dimension the live management fix added.
        m5_trend, m5_conf = _struct_trend_conf(structure, "M5")
        d1_event = _struct_event(structure, "D1")
        h4_event = _struct_event(structure, "H4")
        h1_event = _struct_event(structure, "H1")
        m5_event = _struct_event(structure, "M5")
        # Structural swing levels — feed the DecisionEngine's protective-stop
        # placement for adopted trades (candidate lists were always empty in
        # replay), mirroring the live management builder.
        d1_swing_high, d1_swing_low = _struct_swings(structure, "D1")
        h4_swing_high, h4_swing_low = _struct_swings(structure, "H4")
        h1_swing_high, h1_swing_low = _struct_swings(structure, "H1")

        micro = self._micro_from_slice(slices.get("M1"), is_long)
        h1_candle = _h1_candle_context(slices.get("H1"), is_long)
        bias = wm.bias_dict()
        scan_direction = str(bias.get("direction", "") or "").upper()

        # Live directional consensus panel (unbiased module votes) — re-voted on
        # fresh data and folded into the in-trade thesis check, exactly as live.
        try:
            consensus_votes = wm.votes_list() if hasattr(wm, "votes_list") else []
        except Exception:
            consensus_votes = []

        # Current zone conviction → rolling score_history (trajectory/confidence
        # /thesis-deterioration). Mirrors live's per-cycle score append.
        current_score = 0
        opposing_score = 0
        try:
            zones = (
                wm.entry_zones_list() if hasattr(wm, "entry_zones_list")
                else list(getattr(wm, "entry_zones", ()) or [])
            )
            want_dir = "LONG" if is_long else "SHORT"
            for z in zones:
                zdir = str(getattr(z, "direction", "") or "").upper()
                conv = int(getattr(z, "conviction", 0) or 0)
                # Own-direction zone → drives score_history (trade's own thesis
                # trajectory). Scan/bias-direction zone → drives scan_score, the
                # OPPOSING signal strength the engine's CLOSE term reads. These
                # are different directions; conflating them mis-fed the trade's
                # own conviction as opposing strength (see live Bug #2 fix).
                if zdir == want_dir:
                    current_score = max(current_score, conv)
                if scan_direction in ("LONG", "SHORT") and zdir == scan_direction:
                    opposing_score = max(opposing_score, conv)
        except Exception:
            current_score = 0
            opposing_score = 0
        hist = trade.setdefault("score_history", [])
        hist.append(current_score)

        # Live OQ/EQ decay — recompute on this cycle's WorldModel candidates and
        # diff against the entry baseline (positive decay = deterioration).
        entry_oq = trade.get("entry_oq")
        entry_eq = trade.get("entry_eq")
        live_oq, live_eq = _oq_eq_from_wm(wm, norm_dir)
        oq_decay = (entry_oq - live_oq) if (entry_oq is not None and live_oq is not None) else None
        eq_decay = (entry_eq - live_eq) if (entry_eq is not None and live_eq is not None) else None

        # Session tradeable state (urgency). Backtest has no news feed, so
        # minutes_to_high_impact_news keeps the safe 999 default (accepted
        # simplification — no historical news calendar in replay).
        session_tradeable = True
        try:
            if self.session_engine is not None:
                ss = self.session_engine.get_status(
                    pd.Timestamp(candle["time"]).to_pydatetime()
                )
                session_tradeable = bool(getattr(ss, "is_tradeable", True))
        except Exception:
            session_tradeable = True

        # Live directional consensus panel from this bar's WorldModel — the same
        # unbiased module votes the entry used, so management revalidates the
        # thesis against the panel instead of a structure-only re-derivation.
        consensus_votes = wm.votes_list() if hasattr(wm, "votes_list") else []

        # Current setup score from the live WorldModel zones (matching direction)
        # appended to the rolling history that feeds the conviction-collapse term.
        want_dir = norm_dir.replace("BUY", "LONG").replace("SELL", "SHORT")
        current_score = 0
        try:
            for z in wm.entry_zones_list():
                if str(getattr(z, "direction", "") or "").upper() == want_dir:
                    current_score = max(current_score, int(getattr(z, "conviction", 0) or 0))
        except Exception:
            current_score = 0
        hist = trade.setdefault("score_history", [])
        hist.append(current_score if current_score > 0 else int(getattr(setup, "score", 0) or 0))

        # Live OQ/EQ + decay (entry_* − live_*) — same derivation as the live
        # management path; None when no matching candidate this bar.
        live_oq, live_eq = _oq_eq_from_wm(wm, norm_dir)
        entry_oq = trade.get("entry_oq")
        entry_eq = trade.get("entry_eq")
        oq_decay = (entry_oq - live_oq) if (entry_oq is not None and live_oq is not None) else None
        eq_decay = (entry_eq - live_eq) if (entry_eq is not None and live_eq is not None) else None

        # Session from the candle timestamp (replaces the live SessionEngine
        # status feed). No news calendar in backtest → news is always clear.
        session_name = "UNKNOWN"
        session_tradeable = True
        try:
            ss = self.session_engine.get_status(
                pd.Timestamp(candle["time"]).to_pydatetime()
            )
            session_name = getattr(ss, "current_session", "UNKNOWN")
            session_tradeable = bool(getattr(ss, "is_tradeable", True))
        except Exception:
            pass

        tc = TradeContext(
            symbol=pair,
            order_id=trade.get("order_id", ""),
            direction=norm_dir,
            entry_type=trade.get("entry_type", "") or getattr(setup, "zone_type", ""),
            entry_price=entry,
            current_price=price,
            current_sl=sl,
            pnl_pips=pnl_pips,
            pnl_dollars=pnl_dollars,
            hold_minutes=hold_minutes,
            at_breakeven=bool(trade.get("at_breakeven", False)),
            tp1_hit=bool(trade.get("tp1_hit", False)),
            trailing=bool(trade.get("trailing", False)),
            partial_closed=bool(trade.get("partial_closed", False)),
            lots=lots,
            original_risk_pips=risk_pips,
            scan_score=opposing_score,
            scan_direction=scan_direction,
            live_oq=live_oq,
            live_eq=live_eq,
            entry_oq=entry_oq,
            entry_eq=entry_eq,
            oq_decay=oq_decay,
            eq_decay=eq_decay,
            d1_trend=d1_trend, d1_confidence=d1_conf, d1_event=d1_event,
            d1_swing_high=d1_swing_high, d1_swing_low=d1_swing_low,
            h4_trend=h4_trend, h4_confidence=h4_conf, h4_event=h4_event,
            h4_swing_high=h4_swing_high, h4_swing_low=h4_swing_low,
            h1_trend=h1_trend, h1_confidence=h1_conf, h1_event=h1_event,
            h1_swing_high=h1_swing_high, h1_swing_low=h1_swing_low,
            h1_last_candle_bearish=h1_candle["h1_last_candle_bearish"],
            h1_last_candle_doji=h1_candle["h1_last_candle_doji"],
            m1_trend=micro["m1_trend"],
            m1_event=micro["m1_event"],
            m1_aligned_count=micro["m1_aligned_count"],
            m5_trend=m5_trend, m5_confidence=m5_conf, m5_event=m5_event,
            fast_opposition_streak=int(trade.get("fast_opp", 0) or 0),
            score_history=list(hist[-10:]),
            consensus_votes=list(consensus_votes),
            session_name=session_name,
            session_tradeable=session_tradeable,
            # Single-position serial replay → no concurrent book, so portfolio
            # heat is genuinely 0 (accepted simplification; the heat-aware
            # governor path has nothing to bind against in this model).
            open_trade_count=1,
            max_open_trades=getattr(self.config.risk, "max_open_trades", 5),
        )
        # Populate the context-pressure diagnostic fields the same way live does
        # (opposing-signal summary) so both planes carry identical state.
        try:
            from decision.situation import compute_in_trade_context_pressure
            cp, ob, details = compute_in_trade_context_pressure(tc)
            tc.context_pressure = cp
            tc.opposing_boost = ob
            tc.pressure_details = details
        except Exception:
            pass
        return tc

    def _update_fast_opposition(self, trade: dict, sa) -> None:
        """Maintain the per-trade fast-opposition streak (corrected sign).

        ``tf_alignment`` from ``assess_open_trade`` is direction-normalized
        (positive = supports the open trade), so opposition is the SAME test for
        longs and shorts — this fixes the live short-side sign inversion where
        HTF *support* was counted as opposition.
        """
        tf_align = float(getattr(sa, "tf_alignment", 0.0) or 0.0)
        momentum = float(getattr(sa, "momentum", 0.0) or 0.0)
        if tf_align < -0.2 or momentum < -0.3:
            trade["fast_opp"] = int(trade.get("fast_opp", 0) or 0) + 1
        else:
            trade["fast_opp"] = 0

    def _de_close(self, trade: dict, candle: pd.Series, de) -> dict:
        """Close at the current candle close on a DecisionEngine CLOSE verdict."""
        direction = trade["setup"].direction
        entry = trade["entry_price"]
        close_price = float(candle["close"])
        risk = trade["risk"]
        if direction == "LONG":
            remaining_r = (close_price - entry) / risk
        else:
            remaining_r = (entry - close_price) / risk
        frac = float(trade.get("remaining_fraction", 1.0) or 0.0)
        pnl = trade["realized_r"] + frac * remaining_r
        outcome = "WIN" if pnl > 0 else ("LOSS" if pnl < 0 else "BREAKEVEN")
        hold_minutes = (
            pd.Timestamp(candle["time"]).to_pydatetime() - trade["entry_time"]
        ).total_seconds() / 60.0
        return {
            "pnl_r": pnl,
            "hold_minutes": hold_minutes,
            "outcome": outcome,
            "exit_reason": getattr(de, "reason", "decision_engine"),
            "exit_cause": getattr(de, "exit_cause", None),
        }

    def _time_close(self, trade: dict, candle: pd.Series, hold_minutes: float) -> dict:
        direction = trade["setup"].direction
        entry = trade["entry_price"]
        close_price = float(candle["close"])
        risk = trade["risk"]
        if direction == "LONG":
            remaining_r = (close_price - entry) / risk
        else:
            remaining_r = (entry - close_price) / risk
        frac = float(trade.get("remaining_fraction", 1.0) or 0.0)
        pnl = trade["realized_r"] + frac * remaining_r
        outcome = "WIN" if pnl > 0 else ("LOSS" if pnl < 0 else "BREAKEVEN")
        return {"pnl_r": pnl, "hold_minutes": hold_minutes, "outcome": outcome}

    def _force_close(self, trade: dict, candle: pd.Series) -> dict:
        hold_minutes = (
            pd.Timestamp(candle["time"]).to_pydatetime() - trade["entry_time"]
        ).total_seconds() / 60.0
        return self._time_close(trade, candle, hold_minutes)

    def _apply_pnl(
        self, balance: float, pnl_r: float, trade: dict,
    ) -> tuple[float, float]:
        """Apply a closed trade's P&L to the running balance.

        Non-legacy: realistic lot-based accounting — dollar P&L is
        ``pnl_r × max_loss`` (the live dollar risk at the sized lots) and
        commission scales with lots. Legacy: the original flat R-multiple
        compounding (``pnl_r × risk_per_trade``). Returns ``(new_balance,
        commission)``.
        """
        if getattr(self, "legacy_mode", False):
            commission = self.commission_per_lot
            commission_cost = commission / balance if balance > 0 else 0.0
            pnl_pct = pnl_r * self.risk_per_trade - commission_cost
            return balance * max(1.0 + pnl_pct, 0.01), commission

        lots = float(trade.get("lots", 0.0) or 0.0)
        max_loss = float(trade.get("max_loss", 0.0) or 0.0)
        if max_loss <= 0:
            max_loss = float(trade.get("risk_amount", 0.0) or 0.0)
        if max_loss <= 0:
            max_loss = balance * self.risk_per_trade
        gross_pnl = pnl_r * max_loss
        commission = self.commission_per_lot * max(lots, 0.0)
        new_balance = max(balance + gross_pnl - commission, balance * 0.01)
        return new_balance, commission

    def _journal_trade(
        self, pair: str, trade: dict, close_event: dict, now: datetime
    ) -> None:
        if not self.journal:
            return

        setup = trade["setup"]
        record = TradeRecord(
            pair=pair,
            direction=setup.direction,
            entry=trade["entry_price"],
            exit=trade["entry_price"] + (close_event["pnl_r"] * trade["risk"]),
            pnl=close_event["pnl_r"],
            score=setup.score,
            confluences=list(setup.confluences),
            regime=setup.regime,
            session=trade["session"],
            spread=0.0,
            slippage=0.0,
            entry_type=trade["entry_type"],
            time_to_tp1=None,
            time_to_exit=close_event["hold_minutes"],
            outcome=close_event["outcome"],
            timestamp=now,
            swap_modeled=None,
            swap_status="unavailable",  # backtest has no lot basis for financing in phase 1
            source=getattr(setup, "source", ""),
        )

        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self.journal.log_trade(record))
        except RuntimeError:
            asyncio.run(self.journal.log_trade(record))

    def _resolve_entry_type(self, setup) -> str:
        et = getattr(setup, "entry_type", "")
        if "SWEEP" in et:
            return "SWEEP"
        if "FVG" in et:
            return "FVG"
        if "OB" in et:
            return "OB"
        return "MARKET"

    def _build_atr_comparison(
        self,
        compared: int,
        skipped: int,
        struct_returns: list[float],
        struct_outcomes: list[str],
        atr_returns: list[float],
        atr_outcomes: list[str],
    ) -> ATRComparisonResult:
        s_wins = sum(1 for o in struct_outcomes if o == "WIN")
        s_losses = sum(1 for o in struct_outcomes if o == "LOSS")
        s_be = sum(1 for o in struct_outcomes if o == "BREAKEVEN")
        s_total = len(struct_returns)
        a_wins = sum(1 for o in atr_outcomes if o == "WIN")
        a_losses = sum(1 for o in atr_outcomes if o == "LOSS")
        a_be = sum(1 for o in atr_outcomes if o == "BREAKEVEN")
        a_total = len(atr_returns)

        s_wr = (s_wins / s_total * 100.0) if s_total else 0.0
        a_wr = (a_wins / a_total * 100.0) if a_total else 0.0
        s_mean = float(np.mean(struct_returns)) if struct_returns else 0.0
        a_mean = float(np.mean(atr_returns)) if atr_returns else 0.0
        s_exp = s_mean
        a_exp = a_mean
        s_loss_rate = (s_losses / s_total) if s_total else 0.0
        a_loss_rate = (a_losses / a_total) if a_total else 0.0

        return ATRComparisonResult(
            total_compared=compared,
            total_skipped=skipped,
            structure_wins=s_wins,
            structure_losses=s_losses,
            structure_breakevens=s_be,
            structure_win_rate=round(s_wr, 2),
            structure_mean_r=round(s_mean, 4),
            structure_expectancy=round(s_exp, 4),
            atr_wins=a_wins,
            atr_losses=a_losses,
            atr_breakevens=a_be,
            atr_win_rate=round(a_wr, 2),
            atr_mean_r=round(a_mean, 4),
            atr_expectancy=round(a_exp, 4),
            expectancy_delta=round(a_exp - s_exp, 4),
            structure_loss_rate=round(s_loss_rate, 4),
            atr_loss_rate=round(a_loss_rate, 4),
        )

    def _profit_factor(self, returns: list[float]) -> float:
        gains = sum(r for r in returns if r > 0)
        losses = sum(r for r in returns if r < 0)
        if losses == 0:
            return float("inf") if gains > 0 else 0.0
        return gains / abs(losses)

    def _sharpe_ratio(self, returns: list[float]) -> float:
        if len(returns) < 2:
            return 0.0
        std = float(np.std(returns, ddof=1))
        if std == 0:
            return 0.0
        return float(np.mean(returns) / std * np.sqrt(len(returns)))

    def _max_drawdown_pct(self, equity_curve: list[float]) -> float:
        if not equity_curve:
            return 0.0
        eq = np.array(equity_curve, dtype=float)
        peaks = np.maximum.accumulate(eq)
        dd = (peaks - eq) / np.maximum(peaks, 1e-9)
        return float(np.max(dd))

    def _best_session(self, sessions: dict[str, list[float]]) -> Optional[str]:
        if not sessions:
            return None
        ranked = sorted(
            sessions.items(), key=lambda item: np.mean(item[1]), reverse=True
        )
        return ranked[0][0]
