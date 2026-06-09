"""
APEX TRADER — Market Regime Detector
The sniper does not just see candles; it reads the battlefield condition.
Trend, chop, volatility, accumulation, distribution — every regime demands
different aggression and different risk posture.
"""

from dataclasses import dataclass
from enum import Enum

import numpy as np
import pandas as pd
from loguru import logger


class MarketRegime(Enum):
    TRENDING_STRONG = "TRENDING_STRONG"
    TRENDING_WEAK = "TRENDING_WEAK"
    RANGING = "RANGING"
    VOLATILE = "VOLATILE"
    ACCUMULATION = "ACCUMULATION"
    DISTRIBUTION = "DISTRIBUTION"


@dataclass
class RegimeAnalysis:
    regime: MarketRegime
    atr_current: float
    atr_average: float
    volatility_ratio: float
    directional_strength: float
    confidence: float
    tradeable: bool


class RegimeDetector:
    """
    Classifies market regime using ATR, directionality, and range behavior.

    Rules:
    - RANGING caps score to 80 (20-point reduction from a 100-point model)
    - VOLATILE freezes entries until conditions normalize
    """

    def __init__(
        self,
        atr_period: int = 14,
        atr_average_period: int = 20,
        range_period: int = 20,
        range_threshold_pct: float = 0.006,
        volatile_ratio_threshold: float = 1.8,
    ):
        self.atr_period = atr_period
        self.atr_average_period = atr_average_period
        self.range_period = range_period
        self.range_threshold_pct = range_threshold_pct
        self.volatile_ratio_threshold = volatile_ratio_threshold

    def analyze(self, df: pd.DataFrame) -> RegimeAnalysis:
        if len(df) < max(
            self.atr_period + self.atr_average_period, self.range_period + 30
        ):
            logger.warning(
                "Not enough candles for regime classification, defaulting to RANGING"
            )
            return RegimeAnalysis(
                regime=MarketRegime.RANGING,
                atr_current=0.0,
                atr_average=0.0,
                volatility_ratio=0.0,
                directional_strength=0.0,
                confidence=0.0,
                tradeable=True,
            )

        atr_series = self._calculate_atr(df)
        atr_current = float(atr_series.iloc[-1])
        atr_average = float(atr_series.tail(self.atr_average_period).mean())
        volatility_ratio = atr_current / atr_average if atr_average > 0 else 1.0

        directional_strength = self._calculate_directional_strength(df, atr_series)
        range_score = self._calculate_range_score(df)
        accumulation, distribution = self._detect_wyckoff_conditions(df)

        regime, confidence = self._classify(
            volatility_ratio=volatility_ratio,
            directional_strength=directional_strength,
            range_score=range_score,
            accumulation=accumulation,
            distribution=distribution,
        )

        return RegimeAnalysis(
            regime=regime,
            atr_current=round(atr_current, 8),
            atr_average=round(atr_average, 8),
            volatility_ratio=round(volatility_ratio, 4),
            directional_strength=round(directional_strength, 4),
            confidence=round(confidence, 4),
            tradeable=regime != MarketRegime.VOLATILE,
        )

    def adjust_score(
        self, raw_score: int, analysis: RegimeAnalysis, max_score: int = 100
    ) -> int:
        if analysis.regime == MarketRegime.VOLATILE:
            return 0
        if analysis.regime == MarketRegime.RANGING:
            return int(min(raw_score, max_score - 20))
        return int(min(raw_score, max_score))

    def _calculate_atr(self, df: pd.DataFrame) -> pd.Series:
        prev_close = df["close"].shift(1)
        tr_components = pd.concat(
            [
                df["high"] - df["low"],
                (df["high"] - prev_close).abs(),
                (df["low"] - prev_close).abs(),
            ],
            axis=1,
        )
        true_range = tr_components.max(axis=1)
        return true_range.rolling(self.atr_period).mean().bfill()

    def _calculate_directional_strength(
        self, df: pd.DataFrame, atr_series: pd.Series
    ) -> float:
        up_move = df["high"].diff()
        down_move = -df["low"].diff()

        plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
        minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)

        period = self.atr_period
        atr_scaled = atr_series * period
        plus_di = pd.Series(plus_dm).rolling(period).sum() / atr_scaled
        minus_di = pd.Series(minus_dm).rolling(period).sum() / atr_scaled

        latest_plus = float(plus_di.iloc[-1]) if not np.isnan(plus_di.iloc[-1]) else 0.0
        latest_minus = (
            float(minus_di.iloc[-1]) if not np.isnan(minus_di.iloc[-1]) else 0.0
        )
        total = abs(latest_plus) + abs(latest_minus)
        if total == 0:
            return 0.0
        return min(abs(latest_plus - latest_minus) / total, 1.0)

    def _calculate_range_score(self, df: pd.DataFrame) -> float:
        ma = df["close"].rolling(self.range_period).mean()
        deviation = ((df["close"] - ma).abs() / ma).fillna(0.0)
        recent = deviation.tail(self.range_period)
        in_range = (recent <= self.range_threshold_pct).sum()
        return float(in_range / max(len(recent), 1))

    def _detect_wyckoff_conditions(self, df: pd.DataFrame) -> tuple[bool, bool]:
        lookback = 20
        context = 40
        if len(df) < lookback + context:
            return False, False

        recent = df.iloc[-lookback:]
        prior = df.iloc[-(lookback + context) : -lookback]

        recent_range = (recent["high"].max() - recent["low"].min()) / max(
            recent["close"].iloc[-1], 1e-9
        )
        prior_return = (prior["close"].iloc[-1] - prior["close"].iloc[0]) / max(
            prior["close"].iloc[0], 1e-9
        )
        is_tight_range = recent_range <= self.range_threshold_pct * 2.0

        accumulation = is_tight_range and prior_return <= -0.02
        distribution = is_tight_range and prior_return >= 0.02
        return accumulation, distribution

    def _classify(
        self,
        volatility_ratio: float,
        directional_strength: float,
        range_score: float,
        accumulation: bool,
        distribution: bool,
    ) -> tuple[MarketRegime, float]:
        if (
            volatility_ratio >= self.volatile_ratio_threshold
            and directional_strength < 0.3
        ):
            return MarketRegime.VOLATILE, min(
                1.0, 0.6 + (volatility_ratio - 1.0) * 0.25
            )

        if accumulation:
            return MarketRegime.ACCUMULATION, min(1.0, 0.65 + range_score * 0.25)

        if distribution:
            return MarketRegime.DISTRIBUTION, min(1.0, 0.65 + range_score * 0.25)

        if range_score >= 0.7 and directional_strength < 0.35:
            return MarketRegime.RANGING, min(1.0, 0.5 + range_score * 0.4)

        if directional_strength >= 0.55 and volatility_ratio >= 1.1:
            return MarketRegime.TRENDING_STRONG, min(
                1.0, 0.55 + directional_strength * 0.5
            )

        if directional_strength >= 0.35:
            return MarketRegime.TRENDING_WEAK, min(
                1.0, 0.45 + directional_strength * 0.5
            )

        return MarketRegime.RANGING, 0.5


# ─────────────────────────────────────────────────────────────────────────────
# System-wide Volatility State
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class SystemVolatilityState:
    """
    Aggregated cross-instrument volatility state.
    When the market is in a SPIKE, ALL position sizes are reduced regardless
    of individual pair scores — news shocks and correlated vol events can
    invalidate any single-pair analysis instantly.
    """
    state: str               # "NORMAL", "ELEVATED", "SPIKE"
    avg_volatility_ratio: float
    spike_pair_count: int
    total_pairs_checked: int
    size_multiplier: float   # apply to all position sizes when != 1.0
    note: str


class SystemVolatilityMonitor:
    """
    Consumes a list of RegimeAnalysis objects (one per scanned pair) and
    computes a system-wide volatility state.

    Integration point: call `update(analyses)` after each full scan.
    Use `get_size_multiplier()` as an additional sizing gate alongside
    OpportunityDensityTracker.

    Thresholds:
      SPIKE    — ≥30% of pairs have volatility_ratio > 1.8 → size × 0.60
      ELEVATED — ≥15% of pairs have volatility_ratio > 1.4 → size × 0.80
      NORMAL   — everything else                          → size × 1.00
    """

    SPIKE_RATIO_THRESHOLD    = 1.8
    ELEVATED_RATIO_THRESHOLD = 1.4
    SPIKE_PCT_THRESHOLD      = 0.30   # 30% of pairs
    ELEVATED_PCT_THRESHOLD   = 0.15   # 15% of pairs

    SPIKE_MULTIPLIER    = 0.60
    ELEVATED_MULTIPLIER = 0.80
    NORMAL_MULTIPLIER   = 1.00

    def __init__(self) -> None:
        self._last_state: SystemVolatilityState | None = None

    def update(self, analyses: list[RegimeAnalysis]) -> SystemVolatilityState:
        """
        Call with the list of RegimeAnalysis results from the most recent
        full scan (one analysis per pair/instrument).
        """
        if not analyses:
            state = SystemVolatilityState(
                state="NORMAL",
                avg_volatility_ratio=1.0,
                spike_pair_count=0,
                total_pairs_checked=0,
                size_multiplier=self.NORMAL_MULTIPLIER,
                note="No analyses provided — defaulting to NORMAL",
            )
            self._last_state = state
            return state

        classifiable = [a for a in analyses if a.volatility_ratio > 0.0]

        if not classifiable:
            state = SystemVolatilityState(
                state="NORMAL",
                avg_volatility_ratio=0.0,
                spike_pair_count=0,
                total_pairs_checked=0,
                size_multiplier=self.NORMAL_MULTIPLIER,
                note="No classifiable pairs (all volatility_ratio <= 0) — defaulting to NORMAL",
            )
            self._last_state = state
            return state

        n = len(classifiable)
        ratios = [a.volatility_ratio for a in classifiable]
        avg_ratio = sum(ratios) / n
        spike_count = sum(1 for r in ratios if r > self.SPIKE_RATIO_THRESHOLD)
        elevated_count = sum(1 for r in ratios if r > self.ELEVATED_RATIO_THRESHOLD)

        spike_pct    = spike_count / n
        elevated_pct = elevated_count / n

        if spike_pct >= self.SPIKE_PCT_THRESHOLD:
            state_str = "SPIKE"
            mult = self.SPIKE_MULTIPLIER
            note = (
                f"{spike_count}/{n} pairs with vol_ratio > {self.SPIKE_RATIO_THRESHOLD} "
                f"({spike_pct:.0%}) — all sizes cut to {mult:.0%}"
            )
        elif elevated_pct >= self.ELEVATED_PCT_THRESHOLD:
            state_str = "ELEVATED"
            mult = self.ELEVATED_MULTIPLIER
            note = (
                f"{elevated_count}/{n} pairs with vol_ratio > {self.ELEVATED_RATIO_THRESHOLD} "
                f"({elevated_pct:.0%}) — all sizes cut to {mult:.0%}"
            )
        else:
            state_str = "NORMAL"
            mult = self.NORMAL_MULTIPLIER
            note = f"avg vol_ratio={avg_ratio:.2f} — no adjustment"

        result = SystemVolatilityState(
            state=state_str,
            avg_volatility_ratio=round(avg_ratio, 4),
            spike_pair_count=spike_count,
            total_pairs_checked=n,
            size_multiplier=mult,
            note=note,
        )
        self._last_state = result

        import logging
        logging.getLogger("apex").debug(
            "SystemVol — %s (avg_ratio=%.2f, spike=%d/%d, mult=%.2f)",
            state_str, avg_ratio, spike_count, n, mult,
        )
        return result

    def get_size_multiplier(self) -> float:
        """Return the most recent system-wide size multiplier (1.0 if no data)."""
        if self._last_state is None:
            return self.NORMAL_MULTIPLIER
        return self._last_state.size_multiplier

    def get_state(self) -> SystemVolatilityState | None:
        return self._last_state
