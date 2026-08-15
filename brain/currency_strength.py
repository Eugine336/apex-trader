"""
APEX TRADER — Currency Strength Meter
Ranks all 8 major currencies in real time by relative movement.

This is an analytical instrument, not a trader: it *observes* and reports which
currencies are strengthening or weakening relative to each other. It does NOT
decide a trade direction or emit a "best pair to buy/sell" — direction is a
consequence of the Brain's cognition, not of this meter. Consumers read the
rankings and relative-strength differentials as observations.
"""

import pandas as pd
import numpy as np
from dataclasses import dataclass
from typing import Optional


MAJOR_CURRENCIES = ["USD", "EUR", "GBP", "JPY", "AUD", "NZD", "CAD", "CHF", "XAU"]

# All 28 major pairs and which currencies they contain
CURRENCY_PAIRS = {
    "EURUSD": ("EUR", "USD"), "GBPUSD": ("GBP", "USD"),
    "USDJPY": ("USD", "JPY"), "USDCHF": ("USD", "CHF"),
    "AUDUSD": ("AUD", "USD"), "NZDUSD": ("NZD", "USD"),
    "USDCAD": ("USD", "CAD"), "EURGBP": ("EUR", "GBP"),
    "EURJPY": ("EUR", "JPY"), "GBPJPY": ("GBP", "JPY"),
    "AUDJPY": ("AUD", "JPY"), "NZDJPY": ("NZD", "JPY"),
    "CADJPY": ("CAD", "JPY"), "CHFJPY": ("CHF", "JPY"),
    "EURCHF": ("EUR", "CHF"), "EURAUD": ("EUR", "AUD"),
    "EURCAD": ("EUR", "CAD"), "EURNZD": ("EUR", "NZD"),
    "GBPAUD": ("GBP", "AUD"), "GBPCAD": ("GBP", "CAD"),
    "GBPCHF": ("GBP", "CHF"), "GBPNZD": ("GBP", "NZD"),
    "AUDCAD": ("AUD", "CAD"), "AUDCHF": ("AUD", "CHF"),
    "AUDNZD": ("AUD", "NZD"), "NZDCAD": ("NZD", "CAD"),
    "NZDCHF": ("NZD", "CHF"), "CADCHF": ("CAD", "CHF"),
    # Gold vs USD — treated as a "currency pair" so XAU strength is ranked
    # alongside the majors via the RSI momentum calc (strong XAU + weak USD
    # = the ideal Gold long setup).
    "XAUUSD": ("XAU", "USD"),
}


@dataclass
class CurrencyStrength:
    currency: str
    score: float        # Raw score
    rank: int           # 1 = strongest, 8 = weakest
    label: str          # "VERY_STRONG", "STRONG", "NEUTRAL", "WEAK", "VERY_WEAK"
    momentum: str       # "RISING", "FALLING", "STABLE"
    change_1h: float    # % change in last hour
    change_4h: float    # % change in last 4 hours


@dataclass
class StrengthAnalysis:
    rankings: list[CurrencyStrength]
    strongest: str        # Observed strongest currency (rank 1) — an observation
    weakest: str          # Observed weakest currency (last rank) — an observation
    spread: float         # Observed strength differential (strongest − weakest)
    signal_quality: str   # Spread dispersion: "EXCELLENT" | "GOOD" | "POOR"


class CurrencyStrengthMeter:
    """
    Measures real-time currency strength across all 8 majors using RSI-based
    momentum across multiple timeframes.

    This is an instrument, not a trader: it reports *relative movement* — which
    currency is strengthening or weakening versus the others — as an observation.
    It does not choose a pair or a trade direction. For example, it observes
    "GBP is strongest, USD is weakest" and leaves any GBPUSD interpretation to
    the Brain.
    """

    def __init__(self, rsi_period: int = 14):
        self.rsi_period = rsi_period

    def calculate(self, price_data: dict[str, pd.DataFrame]) -> StrengthAnalysis:
        """
        Calculate strength for all currencies.
        price_data: dict of {pair: dataframe} for all available pairs
        """
        scores = {c: 0.0 for c in MAJOR_CURRENCIES}
        counts = {c: 0 for c in MAJOR_CURRENCIES}
        changes_1h = {c: [] for c in MAJOR_CURRENCIES}
        changes_4h = {c: [] for c in MAJOR_CURRENCIES}

        for pair, df in price_data.items():
            if pair not in CURRENCY_PAIRS:
                continue
            if len(df) < self.rsi_period + 10:
                continue

            base, quote = CURRENCY_PAIRS[pair]
            rsi = self._calculate_rsi(df["close"], self.rsi_period)

            if rsi is None:
                continue

            # Normalize RSI: 50 = neutral, >50 = base stronger, <50 = quote stronger
            normalized = (rsi - 50) / 50  # -1 to +1

            scores[base]  += normalized
            scores[quote] -= normalized
            counts[base]  += 1
            counts[quote] += 1

            # Calculate % changes
            ch1h = self._price_change(df, periods=12)   # ~1hr on M5
            ch4h = self._price_change(df, periods=48)   # ~4hr on M5

            changes_1h[base].append(ch1h)
            changes_1h[quote].append(-ch1h)
            changes_4h[base].append(ch4h)
            changes_4h[quote].append(-ch4h)

        # Average scores
        avg_scores = {}
        for c in MAJOR_CURRENCIES:
            if counts[c] > 0:
                avg_scores[c] = scores[c] / counts[c]
            else:
                avg_scores[c] = 0.0

        # Build rankings
        sorted_currencies = sorted(avg_scores.items(), key=lambda x: x[1], reverse=True)
        rankings = []

        for rank, (currency, score) in enumerate(sorted_currencies, 1):
            ch1h = np.mean(changes_1h[currency]) if changes_1h[currency] else 0.0
            ch4h = np.mean(changes_4h[currency]) if changes_4h[currency] else 0.0

            rankings.append(CurrencyStrength(
                currency=currency,
                score=round(score, 4),
                rank=rank,
                label=self._get_label(score),
                momentum=self._get_momentum(ch1h, ch4h),
                change_1h=round(ch1h, 4),
                change_4h=round(ch4h, 4),
            ))

        strongest = rankings[0].currency
        weakest   = rankings[-1].currency
        spread    = rankings[0].score - rankings[-1].score

        signal_quality = (
            "EXCELLENT" if spread > 0.3 else
            "GOOD"      if spread > 0.15 else
            "POOR"
        )

        return StrengthAnalysis(
            rankings=rankings,
            strongest=strongest,
            weakest=weakest,
            spread=round(spread, 4),
            signal_quality=signal_quality,
        )

    def _calculate_rsi(self, closes: pd.Series, period: int) -> Optional[float]:
        """Calculate RSI for a price series."""
        if len(closes) < period + 1:
            return None

        delta = closes.diff().dropna()
        gains = delta.clip(lower=0)
        losses = (-delta).clip(lower=0)

        avg_gain = gains.rolling(period).mean().iloc[-1]
        avg_loss = losses.rolling(period).mean().iloc[-1]

        if avg_loss == 0 or not np.isfinite(avg_loss) or not np.isfinite(avg_gain):
            # Zero (or non-finite) average loss ⇒ no downside in the window:
            # treat as maximum strength rather than dividing by zero (inf/NaN).
            return 100.0
        rs = avg_gain / avg_loss
        rsi = 100 - (100 / (1 + rs))
        if not np.isfinite(rsi):
            return 100.0
        return round(float(rsi), 2)

    def _price_change(self, df: pd.DataFrame, periods: int) -> float:
        """Calculate % price change over N periods."""
        if len(df) < periods + 1:
            return 0.0
        old = df["close"].iloc[-(periods + 1)]
        new = df["close"].iloc[-1]
        if old == 0:
            return 0.0
        return (new - old) / old

    def _get_label(self, score: float) -> str:
        if score > 0.3:   return "VERY_STRONG"
        if score > 0.1:   return "STRONG"
        if score > -0.1:  return "NEUTRAL"
        if score > -0.3:  return "WEAK"
        return "VERY_WEAK"

    def _get_momentum(self, ch1h: float, ch4h: float) -> str:
        if ch1h > 0.001:   return "RISING"
        if ch1h < -0.001:  return "FALLING"
        return "STABLE"

    def get_pair_strength_differential(
        self,
        pair: str,
        analysis: StrengthAnalysis,
    ) -> dict:
        """
        Observe the relative strength of a pair's two currencies.

        Reports which side (base vs quote) is currently stronger and by how
        much, as a pure observation. It does NOT say whether to go long or
        short — that interpretation belongs to the Brain. ``rank_differential``
        is positive when the base currency ranks stronger than the quote and
        negative when it ranks weaker (lower rank number = stronger).
        """
        if pair not in CURRENCY_PAIRS:
            return {"observed": False, "reason": "Unknown pair"}

        base, quote = CURRENCY_PAIRS[pair]
        base_strength  = next((r for r in analysis.rankings if r.currency == base), None)
        quote_strength = next((r for r in analysis.rankings if r.currency == quote), None)

        if not base_strength or not quote_strength:
            return {"observed": False, "reason": "Data missing"}

        # Lower rank number = stronger; positive differential ⇒ base is stronger.
        differential = quote_strength.rank - base_strength.rank
        stronger_side = (
            base if differential > 0 else
            quote if differential < 0 else
            "EVEN"
        )

        return {
            "observed": True,
            "base": base,
            "quote": quote,
            "base_rank": base_strength.rank,
            "quote_rank": quote_strength.rank,
            "base_label": base_strength.label,
            "quote_label": quote_strength.label,
            "stronger_side": stronger_side,
            "rank_differential": differential,
            "abs_differential": abs(differential),
            "reason": f"{base} #{base_strength.rank} vs {quote} #{quote_strength.rank}"
        }
