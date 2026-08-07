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

# Per-user writeable state — resolve the learned-profile file under the owning
# user's data tree (APEX_DATA_DIR) rather than a bare cwd-relative ``data/`` path.
from runtime_paths import data_dir as _data_dir


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

    Tier 4: when ``per_symbol_enabled`` is on, the learner also keeps a
    ``SYMBOL|REGIME`` compound profile alongside the global per-regime ones.
    The compound profile is only consulted once it has ``per_symbol_min_trades``
    samples; below that the global per-regime strategy is used. Per-symbol
    profiles always accumulate — the threshold only gates their USE.
    """

    MIN_SAMPLE = 30
    CONFIDENCE_FULL = 100
    SAVE_PATH = "data/ml_regime_strategies.json"

    # Per-symbol defaults (overridable via RegimeLearnerConfig). A bare
    # RegimeLearner() with no config keeps per-symbol ON by default to match
    # production; only the read-gate (sample size) controls whether it is used.
    PER_SYMBOL_DEFAULT = True
    PER_SYMBOL_MIN_TRADES = 100

    # Compound-key separator for ``SYMBOL|REGIME`` profiles.
    _SYMBOL_SEP = "|"

    def __init__(self, config=None) -> None:
        self.per_symbol_enabled = bool(
            getattr(config, "per_symbol_enabled", self.PER_SYMBOL_DEFAULT)
        )
        self.per_symbol_min_trades = int(
            getattr(config, "per_symbol_min_trades", self.PER_SYMBOL_MIN_TRADES)
        )
        # Global per-regime strategies (regime → strategy).
        self._strategies: dict[str, RegimeStrategy] = {}
        # Per-symbol compound strategies (``SYMBOL|REGIME`` → strategy).
        self._symbol_strategies: dict[str, RegimeStrategy] = {}
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

        # Per-symbol compound profiles (always accumulate; read-gated on use).
        if self.per_symbol_enabled:
            sym_grouped: dict[str, list[dict]] = {}
            for t in trades:
                sym = str(t.get("pair", "") or "")
                if not sym:
                    continue
                regime = str(t.get("regime", "unknown"))
                key = f"{sym}{self._SYMBOL_SEP}{regime}"
                sym_grouped.setdefault(key, []).append(t)
            symbol_strategies: dict[str, RegimeStrategy] = {}
            for key, group in sym_grouped.items():
                regime = key.split(self._SYMBOL_SEP, 1)[1]
                symbol_strategies[key] = self._learn_regime(regime, group)
            self._symbol_strategies = symbol_strategies

        self._save()
        return strategies

    def get_strategy(self, regime: str, symbol: str | None = None) -> RegimeStrategy:
        # Per-symbol takes priority once it has enough samples; otherwise the
        # global per-regime strategy is used (then the hardcoded default).
        if symbol and self.per_symbol_enabled:
            sstrat = self._symbol_strategies.get(
                f"{symbol}{self._SYMBOL_SEP}{regime}"
            )
            if (
                sstrat is not None
                and sstrat.sample_size >= self.per_symbol_min_trades
                and sstrat.confidence > 0.3
            ):
                return sstrat
        if regime in self._strategies and self._strategies[regime].confidence > 0.3:
            return self._strategies[regime]
        return self._default_strategy(regime)

    def should_trade_regime(
        self, regime: str, symbol: str | None = None
    ) -> tuple[bool, str]:
        # Per-symbol verdict wins once the compound bucket is large enough.
        if symbol and self.per_symbol_enabled:
            sstrat = self._symbol_strategies.get(
                f"{symbol}{self._SYMBOL_SEP}{regime}"
            )
            if sstrat is not None and sstrat.sample_size >= self.per_symbol_min_trades:
                if sstrat.win_rate < 0.40:
                    return False, (
                        f"Win rate for {symbol} in {regime} is "
                        f"{sstrat.win_rate:.0%} over {sstrat.sample_size} "
                        f"trades — avoiding"
                    )
                return True, (
                    f"{symbol} in {regime} win rate {sstrat.win_rate:.0%} "
                    f"— tradeable"
                )
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
        from persistence.atomic_write import atomic_write_text

        p = _data_dir() / Path(self.SAVE_PATH).name
        data = {k: asdict(v) for k, v in self._strategies.items()}
        # Per-symbol compound profiles share the same file under ``SYMBOL|REGIME``
        # keys — they never collide with bare regime keys.
        for k, v in self._symbol_strategies.items():
            data[k] = asdict(v)
        atomic_write_text(p, json.dumps(data, indent=2, default=str))
        logger.info(f"Regime strategies saved to {self.SAVE_PATH}")

    def _load(self) -> None:
        p = _data_dir() / Path(self.SAVE_PATH).name
        if not p.exists():
            return
        try:
            raw = json.loads(p.read_text())
            glob: dict[str, RegimeStrategy] = {}
            sym: dict[str, RegimeStrategy] = {}
            for k, v in raw.items():
                if self._SYMBOL_SEP in k:
                    sym[k] = RegimeStrategy(**v)
                else:
                    glob[k] = RegimeStrategy(**v)
            self._strategies = glob
            self._symbol_strategies = sym
            logger.info(f"Regime strategies loaded from {self.SAVE_PATH}")
        except Exception as exc:
            logger.warning(f"RegimeLearner: could not load strategies: {exc}")

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _learn_regime(self, regime: str, trades: list[dict]) -> RegimeStrategy:
        from adaptive.recency_weight import (
            has_recency_weights,
            trade_weight,
            weighted_mean,
            weighted_win_rate,
        )

        pnls = [float(t.get("pnl", 0)) for t in trades]
        n = len(pnls)
        winners = [p for p in pnls if p > 0]
        losers = [p for p in pnls if p < 0]
        if has_recency_weights(trades):
            # Time-decayed stats: stale-regime trades count less than fresh ones.
            weights = [trade_weight(t) for t in trades]
            wr = weighted_win_rate(pnls, weights)
            win_w = [w for p, w in zip(pnls, weights) if p > 0]
            loss_w = [w for p, w in zip(pnls, weights) if p < 0]
            avg_win = weighted_mean(winners, win_w) if winners else 0.0
            avg_loss = abs(weighted_mean(losers, loss_w)) if losers else 0.0
        else:
            # Scratch trades (pnl == 0) are excluded from the win-rate denominator.
            decided = len(winners) + len(losers)
            wr = len(winners) / decided if decided else 0.0
            avg_win = float(np.mean(winners)) if winners else 0.0
            avg_loss = abs(float(np.mean(losers))) if losers else 0.0
        confidence = min(1.0, n / self.CONFIDENCE_FULL)
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
