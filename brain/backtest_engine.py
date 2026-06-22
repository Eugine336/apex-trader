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
from typing import Optional, Union

import numpy as np
import pandas as pd

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


def _struct_trend_conf(struct_by_tf: dict, tf: str) -> tuple[str, float]:
    """Read ``(trend, confidence)`` for a timeframe from a WorldModel's
    ``structure_by_tf()`` mapping of ``StructureAnalysis`` objects.

    Mirrors ``event_driven_bootstrap._struct_trend_conf`` so the backtest reads
    the WorldModel structure layer exactly as the live plane does.
    """
    sa = struct_by_tf.get(tf)
    if sa is None:
        return "UNKNOWN", 0.0
    trend = sa.trend.value if hasattr(sa.trend, "value") else str(sa.trend)
    return trend, float(getattr(sa, "confidence", 0.0) or 0.0)


def _struct_event(struct_by_tf: dict, tf: str) -> str:
    """Read the last structural event (BOS/CHOCH) for a timeframe from a
    WorldModel's ``structure_by_tf()`` mapping of ``StructureAnalysis``.

    Mirrors ``event_driven_bootstrap._struct_event``.
    """
    sa = struct_by_tf.get(tf)
    if sa is None:
        return "NONE"
    ev = getattr(sa, "last_event", None)
    if ev is None:
        return "NONE"
    return ev.value if hasattr(ev, "value") else str(ev)


def _oq_eq_from_wm(wm, direction: str) -> tuple[Optional[float], Optional[float]]:
    """Live OQ/EQ for ``direction`` from a WorldModel's ranked candidates.

    Mirrors the live management read in
    ``event_driven_bootstrap._run_decision_engine_management``: the first
    candidate matching the trade direction yields ``OQ = confidence×10`` and
    ``EQ = coherence×10`` (both clamped 0–10). Returns ``(None, None)`` when no
    matching candidate / no candidate list is available — exactly the live
    "not recomputed this cycle" semantics that apply no quality pressure.
    """
    want = "LONG" if str(direction).upper() in ("BUY", "LONG") else "SHORT"
    try:
        if hasattr(wm, "candidates_list"):
            candidates = wm.candidates_list()
        else:
            candidates = list(getattr(wm, "candidates", ()) or [])
    except Exception:
        return None, None
    for cand in candidates:
        if str(getattr(cand, "direction", "")).upper() != want:
            continue
        conf = float(getattr(cand, "confidence", 0.0) or 0.0)
        coh = float(getattr(cand, "coherence", 0.0) or 0.0)
        oq = max(0.0, min(10.0, conf * 10.0))
        eq = max(0.0, min(10.0, coh * 10.0))
        return oq, eq
    return None, None


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
    ):
        from config import AppConfig

        self.config = config or AppConfig()

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

            if open_trade is None:
                setup = self._decide_setup(pair, slices, now, balance)
                if setup:
                    open_trade = self._open_trade(setup, now)
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
            open_trade = None

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
            wm = analyze_window(
                pair,
                {"H4": h4, "H1": h1, "M15": m15, "M5": m5},
                timestamp=now,
                consensus_config=getattr(self.config, "consensus", None),
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
            )
        except Exception as exc:
            logger.warning("[backtest] form_thesis failed for {}: {}", pair, exc)
            return None

        if not thesis.trigger:
            return None
        direction = thesis.direction
        if direction not in ("LONG", "SHORT"):
            return None

        # ── Score = zone geometry (70/80/100), NOT bias confidence ──────────
        zone = self._select_zone(wm, direction)
        if zone is None:
            return None
        score = int(getattr(zone, "conviction", 0) or 0)

        # ── Entry prices from the shared EntryEngine ────────────────────────
        # EntryEngine.calculate_entry reads scan_result.score/.confluences, which
        # the WorldModel does not expose — feed it the same scan-view adapter the
        # geometry path uses, carrying the zone conviction as the entry score.
        scan_view = _world_model_to_scan_view(wm)
        scan_view.score = score
        signal = self._calculate_entry(pair, direction, slices, balance, scan_view)
        if signal is None:
            return None

        # ── EntryGate (score ≥ 85 floor, same gate as the live zone path) ───
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
            pair, direction, signal, score, zone, slices, wm, balance, votes,
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

        # ── Position sizing via PortfolioDivision → PositionSizer ───────────
        sized = self._size_trade(
            pair, direction, signal, de_size_mult, de_result.conviction, balance,
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
        )

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

        sl = signal.stop_loss
        entry_price = signal.entry_price
        risk_pips = abs(entry_price - sl) / self.pip_size if sl else 0.0
        rr2 = (
            abs(signal.tp2 - entry_price) / max(abs(entry_price - sl), 1e-8)
            if sl else 0.0
        )
        zone_type = getattr(zone, "zone_type", "")
        zone_type = str(getattr(zone_type, "value", zone_type) or "")

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
            regime=wm.regime_by_tf().get("H1", "") or "",
            confluences=list(signal.confluences),
            consensus_votes=list(votes),
        )

    def _size_trade(
        self,
        pair: str,
        direction: str,
        signal,
        de_size_mult: float,
        conviction: float,
        balance: float,
        existing: Optional[list] = None,
    ):
        """Size the trade through PortfolioDivision → PositionSizer (live path).

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
            from portfolio.models import (
                PortfolioCandidate, PortfolioAccount, SizingFactors,
            )
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
            factors = SizingFactors(
                base_risk_pct=self.risk_per_trade,
                de_size_mult=de_size_mult,
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

    def _micro_from_slice(self, m1_df, is_long: bool) -> dict:
        """M1 alignment count + micro-structure event/trend from a candle slice.

        Mirrors the live ``_management_micro_context`` (last-5 closed-candle
        alignment + ``StructureEngine`` micro read) but sources candles from the
        replay slice instead of a broker fetch.
        """
        out = {"m1_aligned_count": 0, "m1_event": "NONE", "m1_trend": "UNKNOWN"}
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
            from brain.structure_engine import StructureEngine
            engine = StructureEngine(swing_lookback=3, pip_size=self.pip_size)
            analysis = engine.analyze(m1_df.iloc[-min(len(m1_df), 100):])
            out["m1_event"] = analysis.last_event.value
            out["m1_trend"] = analysis.trend.value
        except Exception as exc:
            logger.debug("[backtest] M1 micro-structure read failed: {}", exc)
        return out

    def _open_trade(self, setup: BacktestSetup, now: datetime) -> dict:
        slippage_distance = self.slippage_pips * self.pip_size
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
            "slippage_cost": slippage_distance,
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
        return None

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
        # by the thesis-secure path). pip P&L × pip-value × open lots.
        lots = float(trade.get("lots", 0.0) or 0.0)
        remaining_fraction = float(trade.get("remaining_fraction", 1.0) or 1.0)
        pnl_dollars = pnl_pips * self.pip_value_per_lot * lots * remaining_fraction
        hold_minutes = (
            pd.Timestamp(candle["time"]).to_pydatetime() - trade["entry_time"]
        ).total_seconds() / 60.0
        lots = float(trade.get("lots", 0.0) or 0.0)
        # Dollar P&L from current open lots (matches the live broker-P&L feed).
        pnl_dollars = pnl_pips * self.pip_value_per_lot * lots

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
        try:
            zones = (
                wm.entry_zones_list() if hasattr(wm, "entry_zones_list")
                else list(getattr(wm, "entry_zones", ()) or [])
            )
            want_dir = "LONG" if is_long else "SHORT"
            for z in zones:
                if str(getattr(z, "direction", "") or "").upper() == want_dir:
                    current_score = max(current_score, int(getattr(z, "conviction", 0) or 0))
        except Exception:
            current_score = 0
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

        return TradeContext(
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
            scan_score=current_score or int(getattr(setup, "score", 0) or 0),
            scan_direction=scan_direction,
            live_oq=live_oq,
            live_eq=live_eq,
            entry_oq=entry_oq,
            entry_eq=entry_eq,
            oq_decay=oq_decay,
            eq_decay=eq_decay,
            d1_trend=d1_trend, d1_confidence=d1_conf, d1_event=d1_event,
            h4_trend=h4_trend, h4_confidence=h4_conf, h4_event=h4_event,
            h1_trend=h1_trend, h1_confidence=h1_conf, h1_event=h1_event,
            h1_last_candle_bearish=h1_candle["h1_last_candle_bearish"],
            h1_last_candle_doji=h1_candle["h1_last_candle_doji"],
            m1_trend=micro["m1_trend"],
            m1_event=micro["m1_event"],
            m1_aligned_count=micro["m1_aligned_count"],
            m5_trend=m5_trend, m5_confidence=m5_conf, m5_event=m5_event,
            fast_opposition_streak=int(trade.get("fast_opp", 0) or 0),
            score_history=list(hist[-10:]),
            consensus_votes=list(consensus_votes),
            session_tradeable=session_tradeable,
            open_trade_count=1,
            max_open_trades=getattr(self.config.risk, "max_open_trades", 5),
        )

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
