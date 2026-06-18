"""BacktestRunner — orchestrates a historical replay through the real stack.

The runner walks the timeline one base-timeframe bar at a time and, for every
bar, drives the real adaptive components exactly as production would:

* the **L7 RegimeDetector** classifies the market per pair (and is credited with
  realised R on every close), and
* the **L8 RiskManager** gates every entry (drawdown / exposure / correlation /
  per-regime), with each block recorded as a risk event,

while the :class:`~backtest.broker.SimulatedBroker` handles fills with spread,
slippage and pessimistic intrabar SL/TP. Signal generation is delegated to a
pluggable :class:`~backtest.strategy.Strategy`, so the harness never reimplements
trading logic.
"""

from __future__ import annotations

import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional, Sequence

from loguru import logger

from backtest.broker import ClosedFill, SimulatedBroker, _instrument_pip_size, _instrument_pip_value
from backtest.data import Candle
from backtest.results import BacktestResults, TradeRecord
from backtest.strategy import BarContext, MovingAverageCrossStrategy, Signal, Strategy


@dataclass
class _OpenMeta:
    """Per-order bookkeeping the broker doesn't carry (for R + breakdowns)."""

    entry_sl: float
    open_price: float
    lots: float
    pair: str
    direction: str
    regime: str
    horizon: str
    profile: str
    risk_dollars: float


@dataclass
class BacktestRunner:
    """Drives a backtest from loaded data to a :class:`BacktestResults`.

    ``data`` maps pair → {timeframe: [Candle, ...]}. ``base_timeframe`` is the
    timeframe whose bars drive the replay clock (typically the lowest one).
    """

    data: dict[str, dict[str, list[Candle]]]
    config: object = None
    strategy: Optional[Strategy] = None
    base_timeframe: str = "M5"
    starting_balance: float = 10_000.0
    spread_pips: float = 1.0
    slippage_pips: float = 0.5
    seed: int = 42
    progress_interval: int = 100
    use_regime: bool = True
    use_risk: bool = True
    broker: Optional[SimulatedBroker] = None
    regime_detector: object = None
    risk_manager: object = None
    _tmpdir: Optional[str] = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if self.strategy is None:
            self.strategy = MovingAverageCrossStrategy()
        if self.broker is None:
            self.broker = SimulatedBroker(
                starting_balance=self.starting_balance,
                spread_pips=self.spread_pips,
                slippage_pips=self.slippage_pips,
                seed=self.seed,
            )
        for pair, frames in self.data.items():
            self.broker.load_data(pair, frames)
        self._open_meta: dict[str, _OpenMeta] = {}
        self._history: dict[str, list[Candle]] = {p: [] for p in self.data}
        self.results = BacktestResults(self.starting_balance)

    # ── Adaptive layer wiring ────────────────────────────────────────────

    def _ensure_adaptive(self) -> None:
        """Lazily build the real RegimeDetector / RiskManager against temp DBs."""
        if not (self.use_regime or self.use_risk):
            return
        self._tmpdir = self._tmpdir or tempfile.mkdtemp(prefix="apex_backtest_")
        if self.use_regime and self.regime_detector is None:
            try:
                from adaptive.regime_detector import RegimeDetector

                self.regime_detector = RegimeDetector(
                    db_path=Path(self._tmpdir) / "regime.db",
                    enabled=True,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("[backtest] regime detector unavailable: {}", exc)
                self.use_regime = False
        if self.use_risk and self.risk_manager is None:
            try:
                from adaptive.risk_manager import RiskManager

                self.risk_manager = RiskManager(
                    db_path=Path(self._tmpdir) / "risk.db",
                    enabled=True,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("[backtest] risk manager unavailable: {}", exc)
                self.use_risk = False

    # ── Timeline ─────────────────────────────────────────────────────────

    def _timeline(self) -> list[tuple[datetime, str, Candle]]:
        events: list[tuple[datetime, str, Candle]] = []
        for pair, frames in self.data.items():
            base = frames.get(self.base_timeframe)
            if not base:
                # Fall back to whatever single timeframe is present.
                if len(frames) == 1:
                    base = next(iter(frames.values()))
                else:
                    continue
            for candle in base:
                events.append((candle.time, pair, candle))
        events.sort(key=lambda e: e[0])
        return events

    # ── Run ──────────────────────────────────────────────────────────────

    def run(self) -> BacktestResults:
        timeline = self._timeline()
        if not timeline:
            raise ValueError(
                "no candles to replay — load data for the base timeframe "
                f"{self.base_timeframe!r} before running"
            )
        self._ensure_adaptive()
        total = len(timeline)
        logger.info(
            "[backtest] replaying {} bars across {} pair(s) (base={})",
            total, len(self.data), self.base_timeframe,
        )
        for i, (when, pair, candle) in enumerate(timeline):
            self._step(when, pair, candle)
            if self.progress_interval and (i + 1) % self.progress_interval == 0:
                logger.info(
                    "[backtest] {}/{} bars ({:.0f}%) — {} — equity {:.2f}",
                    i + 1, total, 100.0 * (i + 1) / total, when.date(),
                    self.broker.get_account_info().equity,
                )
        # Close anything still open at the end so P&L is fully realised.
        for fill in self.broker.force_close_all():
            self._on_fill(fill)
        self._record_equity(timeline[-1][0])
        logger.info(
            "[backtest] done — {} trades, ending balance {:.2f}",
            len(self.results.trades), self.results.ending_balance,
        )
        return self.results

    def _step(self, when: datetime, pair: str, candle: Candle) -> None:
        # 1) Advance the broker — process SL/TP fills against this bar first.
        for fill in self.broker.advance(pair, candle):
            self._on_fill(fill)
        # 2) Track history (mirrors the broker's no-lookahead window).
        hist = self._history[pair]
        hist.append(candle)
        # 3) Update regime for the pair from the closes seen so far.
        regime = "UNKNOWN"
        if self.use_regime and self.regime_detector is not None:
            try:
                state = self.regime_detector.update(pair, [c.close for c in hist])
                regime = getattr(state, "regime", "UNKNOWN")
            except Exception as exc:  # noqa: BLE001
                logger.debug("[backtest] regime update failed for {}: {}", pair, exc)
        # 4) Ask the strategy for signals (lookahead-free context).
        ctx = BarContext(
            pair=pair,
            now=when,
            candle=candle,
            history=hist,
            broker=self.broker,
            open_positions=self.broker.get_open_positions(),
            regime=regime,
            account_balance=self.broker.get_account_info().balance,
        )
        try:
            signals = self.strategy.on_bar(ctx) or []
        except Exception as exc:  # noqa: BLE001
            logger.warning("[backtest] strategy error on {} {}: {}", pair, when, exc)
            signals = []
        for sig in signals:
            self._handle_signal(sig, when, regime)
        # 5) Mark-to-market equity point.
        self._record_equity(when)

    # ── Signal → risk gate → order ───────────────────────────────────────

    def _handle_signal(self, sig: Signal, when: datetime, regime: str) -> None:
        if self.use_risk and self.risk_manager is not None:
            open_positions = [
                {
                    "pair": p.symbol,
                    "direction": p.direction,
                    "regime": self._open_meta.get(p.order_id, _OpenMeta(
                        0, 0, 0, p.symbol, p.direction, "UNKNOWN", "", "", 0
                    )).regime,
                }
                for p in self.broker.get_open_positions()
            ]
            try:
                decision = self.risk_manager.can_open_position(
                    sig.pair, sig.direction,
                    open_positions=open_positions,
                    account_balance=self.broker.get_account_info().balance,
                    regime=regime,
                )
            except Exception as exc:  # noqa: BLE001
                logger.debug("[backtest] risk gate error: {}", exc)
                decision = None
            if decision is not None and not getattr(decision, "allowed", True):
                self.results.record_risk_event(
                    sig.pair, getattr(decision, "rule", "?"),
                    getattr(decision, "reason", ""),
                )
                return
        result = self.broker.place_order(
            sig.pair, sig.direction, sig.size, sig.sl, sig.tp, comment=sig.reason,
        )
        if not result.success:
            logger.debug("[backtest] order rejected {}: {}", sig.pair, result.error)
            return
        pip = _instrument_pip_size(sig.pair)
        risk_pips = abs(result.fill_price - sig.sl) / pip if sig.sl > 0 else 0.0
        risk_dollars = risk_pips * _instrument_pip_value(sig.pair) * sig.size
        self._open_meta[result.order_id] = _OpenMeta(
            entry_sl=sig.sl,
            open_price=result.fill_price,
            lots=sig.size,
            pair=sig.pair,
            direction=sig.direction,
            regime=regime,
            horizon=sig.horizon,
            profile=sig.profile,
            risk_dollars=risk_dollars,
        )

    # ── Fill → trade record + adaptive feedback ──────────────────────────

    def _on_fill(self, fill: ClosedFill) -> None:
        meta = self._open_meta.pop(fill.order_id, None)
        risk_dollars = meta.risk_dollars if meta and meta.risk_dollars > 0 else 0.0
        pnl_r = (fill.pnl / risk_dollars) if risk_dollars > 0 else 0.0
        regime = meta.regime if meta else "UNKNOWN"
        trade = TradeRecord(
            pair=fill.symbol,
            direction=fill.direction,
            lots=fill.lots,
            open_price=fill.open_price,
            close_price=fill.close_price,
            open_time=fill.open_time,
            close_time=fill.close_time,
            pnl=round(fill.pnl, 2),
            pnl_pips=round(fill.pnl_pips, 1),
            pnl_r=round(pnl_r, 4),
            exit_reason=fill.exit_reason,
            regime=regime,
            horizon=meta.horizon if meta else "SWING",
            profile=meta.profile if meta else "default",
        )
        self.results.record_trade(trade)
        # Feed the real adaptive layers their outcome — exactly like production.
        if self.use_regime and self.regime_detector is not None and risk_dollars > 0:
            try:
                self.regime_detector.record_performance(fill.symbol, pnl_r)
            except Exception as exc:  # noqa: BLE001
                logger.debug("[backtest] regime record_performance failed: {}", exc)
        if self.use_risk and self.risk_manager is not None:
            try:
                self.risk_manager.on_trade_closed(
                    fill.pnl, self.broker.get_account_info().balance,
                )
            except Exception as exc:  # noqa: BLE001
                logger.debug("[backtest] risk on_trade_closed failed: {}", exc)

    def _record_equity(self, when: datetime) -> None:
        self.results.record_equity(when, self.broker.get_account_info().equity)

    # ── Cleanup ──────────────────────────────────────────────────────────

    def close(self) -> None:
        for obj in (self.regime_detector, self.risk_manager):
            try:
                if obj is not None and hasattr(obj, "close"):
                    obj.close()
            except Exception:  # noqa: BLE001
                pass
        if self._tmpdir:
            import shutil

            shutil.rmtree(self._tmpdir, ignore_errors=True)
            self._tmpdir = None
