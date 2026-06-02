"""
APEX TRADER — Regime Learner
A trending market needs different parameters than a ranging one.
I learn the optimal TP multiplier, SL buffer, and entry threshold
for each regime separately. One size does NOT fit all.
"""

import json
from dataclasses import dataclass, asdict
from pathlib import Path

import numpy as np
from loguru import logger


@dataclass
class RegimeStrategy:
    regime: str
    optimal_score_threshold: int = 85
    optimal_tp_multiplier: float = 1.0
    optimal_sl_buffer_pips: float = 2.0
    optimal_partial_close_ratio: float = 0.5
    avg_hold_candles: int = 15
    win_rate: float = 0.0
    sample_size: int = 0
    confidence: float = 0.0


class RegimeLearner:
    """
    Learns the best parameters for each market regime from historical trades.
    Only outputs high-confidence strategies once enough data exists.
    """

    MIN_SAMPLE = 30
    CONFIDENCE_FULL = 100
    SAVE_PATH = "data/ml_regime_strategies.json"

    def __init__(self) -> None:
        self._strategies: dict[str, RegimeStrategy] = {}
        self._load()

    def learn(self, trades: list[dict]) -> dict[str, RegimeStrategy]:
        grouped: dict[str, list[dict]] = {}
        for t in trades:
            regime = str(t.get("regime", "unknown"))
            grouped.setdefault(regime, []).append(t)

        strategies: dict[str, RegimeStrategy] = {}
        for regime, group in grouped.items():
            strategies[regime] = self._learn_regime(regime, group)

        self._strategies = strategies
        self._save()
        return strategies

    def get_strategy(self, regime: str) -> RegimeStrategy:
        if regime in self._strategies and self._strategies[regime].confidence > 0.3:
            return self._strategies[regime]
        return self._default_strategy(regime)

    def should_trade_regime(self, regime: str) -> tuple[bool, str]:
        strat = self._strategies.get(regime)
        if strat is None or strat.sample_size < self.MIN_SAMPLE:
            return True, f"Insufficient data for {regime} — using defaults"
        if strat.win_rate < 0.40:
            return False, (
                f"Win rate for {regime} is {strat.win_rate:.0%} "
                f"over {strat.sample_size} trades — avoiding"
            )
        return True, f"{regime} win rate {strat.win_rate:.0%} — tradeable"

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _save(self) -> None:
        p = Path(self.SAVE_PATH)
        p.parent.mkdir(parents=True, exist_ok=True)
        data = {k: asdict(v) for k, v in self._strategies.items()}
        p.write_text(json.dumps(data, indent=2, default=str))
        logger.info(f"Regime strategies saved to {self.SAVE_PATH}")

    def _load(self) -> None:
        p = Path(self.SAVE_PATH)
        if not p.exists():
            return
        try:
            raw = json.loads(p.read_text())
            self._strategies = {k: RegimeStrategy(**v) for k, v in raw.items()}
            logger.info(f"Regime strategies loaded from {self.SAVE_PATH}")
        except Exception as exc:
            logger.warning(f"RegimeLearner: could not load strategies: {exc}")

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _learn_regime(self, regime: str, trades: list[dict]) -> RegimeStrategy:
        pnls = [float(t.get("pnl", 0)) for t in trades]
        winners = [p for p in pnls if p > 0]
        losers = [p for p in pnls if p < 0]
        n = len(pnls)
        wr = len(winners) / n if n else 0.0
        confidence = min(1.0, n / self.CONFIDENCE_FULL)

        avg_win = float(np.mean(winners)) if winners else 0.0
        avg_loss = abs(float(np.mean(losers))) if losers else 0.0

        if wr >= 0.75:
            tp_mult = 1.2
            sl_buf = 1.5
            threshold = 82
            partial = 0.4
        elif wr >= 0.60:
            tp_mult = 1.0
            sl_buf = 2.0
            threshold = 85
            partial = 0.5
        else:
            tp_mult = 0.8
            sl_buf = 3.0
            threshold = 90
            partial = 0.6

        if avg_loss > 0 and avg_win / avg_loss > 2.0:
            tp_mult = min(tp_mult + 0.2, 1.5)
            partial = max(partial - 0.1, 0.3)

        hold_times = [
            float(t["time_to_exit"])
            for t in trades
            if t.get("time_to_exit") is not None
        ]
        avg_hold = int(np.mean(hold_times)) if hold_times else 15

        return RegimeStrategy(
            regime=regime,
            optimal_score_threshold=threshold,
            optimal_tp_multiplier=round(tp_mult, 2),
            optimal_sl_buffer_pips=round(sl_buf, 2),
            optimal_partial_close_ratio=round(partial, 2),
            avg_hold_candles=avg_hold,
            win_rate=round(wr, 4),
            sample_size=n,
            confidence=round(confidence, 4),
        )

    @staticmethod
    def _default_strategy(regime: str) -> RegimeStrategy:
        return RegimeStrategy(regime=regime)
