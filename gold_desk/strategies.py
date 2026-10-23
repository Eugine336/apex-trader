"""Deterministic Gold pattern detectors — evidence only, never trade decisions.

Every detector answers: "What factual price pattern is visible in completed
market data?" None outputs direction, entry, stop, target, position size, rank,
win probability, expected value or an instruction to trade. Overlapping patterns
are simply observations for Apex's single CognitiveBrain to interpret.
"""
from __future__ import annotations

from datetime import timedelta
from statistics import median
from typing import Protocol, Sequence

from gold_desk.indicators import atr, ema, median_true_range, rsi, rolling_zscore, true_ranges
from gold_desk.models import Candle, MarketSnapshot, PatternObservation


class PatternDetector(Protocol):
    pattern_id: str

    def observe(self, snapshot: MarketSnapshot) -> list[PatternObservation]: ...


def _closes(bars: Sequence[Candle]) -> list[float]:
    return [bar.close for bar in bars]


def _observation(
    snapshot: MarketSnapshot,
    *,
    pattern_id: str,
    timeframe: str,
    facts: Sequence[str],
    measurements: dict[str, float | int | str | bool],
    source_bar: Candle | None = None,
) -> PatternObservation:
    bars = snapshot.bars(timeframe)
    if source_bar is None:
        if not bars:
            raise ValueError("observation requires a source candle")
        source_bar = bars[-1]
    return PatternObservation(
        symbol="XAUUSD",
        venue=str(snapshot.venue),
        pattern_id=pattern_id,
        timeframe=timeframe,
        observed_at=snapshot.observed_at,
        source_bar_time=source_bar.timestamp,
        facts=tuple(facts),
        measurements=measurements,
    )


class TrendContinuationDetector:
    """Measure trend/pullback/continuation geometry on completed M15 candles."""
    pattern_id = "trend_continuation"

    def observe(self, snapshot: MarketSnapshot) -> list[PatternObservation]:
        bars = snapshot.bars("M15")
        if not snapshot.data_quality_ok or not snapshot.market_open or len(bars) < 55:
            return []
        values = _closes(bars)
        fast, slow, av = ema(values, 20), ema(values, 50), atr(bars)
        if fast is None or slow is None or av is None:
            return []
        last, previous = bars[-1], bars[-2]
        pullback = bars[-6:-1]
        if fast > slow and last.close > fast and last.close > previous.high and last.close > last.open:
            if min(bar.low for bar in pullback) <= fast + 0.45 * av:
                return [_observation(
                    snapshot, pattern_id=self.pattern_id, timeframe="M15",
                    facts=(
                        "EMA20 is above EMA50 on the completed M15 series",
                        "price tested the EMA20 area during the recent five-bar window",
                        "latest completed M15 candle closed above the preceding candle high",
                        "latest completed M15 candle closed above its open",
                    ),
                    measurements={
                        "ema20": fast, "ema50": slow, "atr14": av,
                        "last_close": last.close, "previous_high": previous.high,
                        "recent_pullback_low": min(bar.low for bar in pullback),
                        "last_close_above_ema20": True,
                    },
                    source_bar=last,
                )]
        if fast < slow and last.close < fast and last.close < previous.low and last.close < last.open:
            if max(bar.high for bar in pullback) >= fast - 0.45 * av:
                return [_observation(
                    snapshot, pattern_id=self.pattern_id, timeframe="M15",
                    facts=(
                        "EMA20 is below EMA50 on the completed M15 series",
                        "price tested the EMA20 area during the recent five-bar window",
                        "latest completed M15 candle closed below the preceding candle low",
                        "latest completed M15 candle closed below its open",
                    ),
                    measurements={
                        "ema20": fast, "ema50": slow, "atr14": av,
                        "last_close": last.close, "previous_low": previous.low,
                        "recent_pullback_high": max(bar.high for bar in pullback),
                        "last_close_below_ema20": True,
                    },
                    source_bar=last,
                )]
        return []


class LiquiditySweepDetector:
    """Record a recent-range extreme sweep and close back inside the range."""
    pattern_id = "liquidity_sweep"

    def observe(self, snapshot: MarketSnapshot) -> list[PatternObservation]:
        bars = snapshot.bars("M5")
        if not snapshot.data_quality_ok or not snapshot.market_open or len(bars) < 22:
            return []
        av = atr(bars)
        if av is None:
            return []
        last, prior = bars[-1], bars[-21:-1]
        high, low = max(bar.high for bar in prior), min(bar.low for bar in prior)
        measurements = {
            "atr14": av, "prior_20bar_high": high, "prior_20bar_low": low,
            "current_high": last.high, "current_low": last.low, "current_close": last.close,
            "high_sweep_distance": max(0.0, last.high - high),
            "low_sweep_distance": max(0.0, low - last.low),
        }
        if last.low < low - 0.05 * av and last.close > low:
            return [_observation(
                snapshot, pattern_id=self.pattern_id, timeframe="M5",
                facts=(
                    "latest completed M5 candle traded below the prior 20-bar low",
                    "latest completed M5 candle closed back above the prior 20-bar low",
                    "the lower range boundary was swept and reclaimed within one completed candle",
                ),
                measurements={**measurements, "boundary_swept": "prior_20bar_low",
                              "reclaimed_boundary": "prior_20bar_low"},
                source_bar=last,
            )]
        if last.high > high + 0.05 * av and last.close < high:
            return [_observation(
                snapshot, pattern_id=self.pattern_id, timeframe="M5",
                facts=(
                    "latest completed M5 candle traded above the prior 20-bar high",
                    "latest completed M5 candle closed back below the prior 20-bar high",
                    "the upper range boundary was swept and reclaimed within one completed candle",
                ),
                measurements={**measurements, "boundary_swept": "prior_20bar_high",
                              "reclaimed_boundary": "prior_20bar_high"},
                source_bar=last,
            )]
        return []


class VolatilityExpansionDetector:
    """Record range escape accompanied by elevated true range."""
    pattern_id = "volatility_expansion"

    def observe(self, snapshot: MarketSnapshot) -> list[PatternObservation]:
        bars = snapshot.bars("M5")
        if not snapshot.data_quality_ok or not snapshot.market_open or len(bars) < 30:
            return []
        av = atr(bars)
        baseline = median_true_range(bars[:-1], window=18)
        if av is None or baseline is None:
            return []
        last, prior = bars[-1], bars[-21:-1]
        high, low = max(bar.high for bar in prior), min(bar.low for bar in prior)
        current_tr = true_ranges(bars)[-1]
        if current_tr < 1.25 * baseline:
            return []
        common = {"atr14": av, "median_prior_true_range": baseline,
                  "current_true_range": current_tr, "true_range_expansion_multiple": current_tr / baseline,
                  "prior_20bar_high": high, "prior_20bar_low": low, "close": last.close}
        if last.close > high + 0.05 * av:
            return [_observation(
                snapshot, pattern_id=self.pattern_id, timeframe="M5",
                facts=(
                    "latest completed M5 close is above the prior 20-bar high",
                    "the latest M5 true range exceeds 1.25 times the prior median true range",
                    "price range expansion and upper-boundary escape occurred together",
                ),
                measurements={**common, "escaped_boundary": "prior_20bar_high"},
                source_bar=last,
            )]
        if last.close < low - 0.05 * av:
            return [_observation(
                snapshot, pattern_id=self.pattern_id, timeframe="M5",
                facts=(
                    "latest completed M5 close is below the prior 20-bar low",
                    "the latest M5 true range exceeds 1.25 times the prior median true range",
                    "price range expansion and lower-boundary escape occurred together",
                ),
                measurements={**common, "escaped_boundary": "prior_20bar_low"},
                source_bar=last,
            )]
        return []


class FailedBreakoutDetector:
    """Measure an attempted range break whose close returns inside the boundary."""
    pattern_id = "failed_breakout"

    def observe(self, snapshot: MarketSnapshot) -> list[PatternObservation]:
        bars = snapshot.bars("M5")
        if not snapshot.data_quality_ok or not snapshot.market_open or len(bars) < 16:
            return []
        av = atr(bars)
        if av is None:
            return []
        last, prior = bars[-1], bars[-11:-1]
        high, low = max(bar.high for bar in prior), min(bar.low for bar in prior)
        body = max(abs(last.close - last.open), 0.02 * av)
        upper_wick = last.high - max(last.open, last.close)
        lower_wick = min(last.open, last.close) - last.low
        common = {
            "atr14": av, "prior_10bar_high": high, "prior_10bar_low": low,
            "body_size": abs(last.close - last.open), "upper_wick_size": upper_wick,
            "lower_wick_size": lower_wick, "upper_wick_to_body": upper_wick / body,
            "lower_wick_to_body": lower_wick / body, "close": last.close,
        }
        if last.high > high + 0.10 * av and last.close < high and upper_wick >= 1.2 * body:
            return [_observation(
                snapshot, pattern_id=self.pattern_id, timeframe="M5",
                facts=(
                    "latest M5 candle traded above the prior 10-bar high",
                    "the completed candle closed back below the prior 10-bar high",
                    "the rejection wick above the range is at least 1.2 times the candle body",
                ),
                measurements={**common, "failed_boundary": "prior_10bar_high"},
                source_bar=last,
            )]
        if last.low < low - 0.10 * av and last.close > low and lower_wick >= 1.2 * body:
            return [_observation(
                snapshot, pattern_id=self.pattern_id, timeframe="M5",
                facts=(
                    "latest M5 candle traded below the prior 10-bar low",
                    "the completed candle closed back above the prior 10-bar low",
                    "the rejection wick below the range is at least 1.2 times the candle body",
                ),
                measurements={**common, "failed_boundary": "prior_10bar_low"},
                source_bar=last,
            )]
        return []


class StatisticalReversionDetector:
    """Measure statistical displacement only when the EMA spread is contained."""
    pattern_id = "statistical_displacement"

    def __init__(self, z_threshold: float = 2.0, max_trend_atr: float = 1.5) -> None:
        if z_threshold <= 0 or max_trend_atr <= 0:
            raise ValueError("thresholds must be positive")
        self.z_threshold = z_threshold
        self.max_trend_atr = max_trend_atr

    def observe(self, snapshot: MarketSnapshot) -> list[PatternObservation]:
        bars = snapshot.bars("M15")
        if not snapshot.data_quality_ok or not snapshot.market_open or len(bars) < 55:
            return []
        values = _closes(bars)
        z = rolling_zscore(values, 30)
        av, fast, slow = atr(bars), ema(values, 20), ema(values, 50)
        if z is None or av is None or fast is None or slow is None:
            return []
        score, mean, deviation = z
        trend_spread = abs(fast - slow) / av
        if abs(score) < self.z_threshold or trend_spread > self.max_trend_atr:
            return []
        relation = "above_mean" if score > 0 else "below_mean"
        return [_observation(
            snapshot, pattern_id=self.pattern_id, timeframe="M15",
            facts=(
                f"latest M15 close is {abs(score):.2f} rolling standard deviations {relation}",
                "EMA20-to-EMA50 separation is within the configured ATR-normalized containment threshold",
                "the 30-close statistical window is available and has non-zero dispersion",
            ),
            measurements={
                "rolling_zscore_30": score, "rolling_mean_30": mean,
                "rolling_stddev_30": deviation, "ema20": fast, "ema50": slow,
                "ema_spread_atr": trend_spread, "atr14": av, "last_close": bars[-1].close,
                "observed_relation": relation,
            },
            source_bar=bars[-1],
        )]


class TrendExhaustionDetector:
    """Measure price/RSI non-confirmation plus a completed H1 structure break."""
    pattern_id = "trend_exhaustion"

    def observe(self, snapshot: MarketSnapshot) -> list[PatternObservation]:
        bars = snapshot.bars("H1")
        if not snapshot.data_quality_ok or not snapshot.market_open or len(bars) < 32:
            return []
        av = atr(bars)
        if av is None:
            return []
        prior, recent = bars[-16:-6], bars[-6:]
        prior_rsi, recent_rsi = rsi(_closes(prior), 5), rsi(_closes(recent), 5)
        last, previous = bars[-1], bars[-2]
        if prior_rsi is None or recent_rsi is None:
            return []
        prior_high, recent_high = max(b.high for b in prior), max(b.high for b in recent)
        prior_low, recent_low = min(b.low for b in prior), min(b.low for b in recent)
        common = {
            "atr14": av, "prior_window_rsi5": prior_rsi, "recent_window_rsi5": recent_rsi,
            "prior_window_high": prior_high, "recent_window_high": recent_high,
            "prior_window_low": prior_low, "recent_window_low": recent_low,
            "last_close": last.close, "previous_high": previous.high, "previous_low": previous.low,
        }
        if recent_high > prior_high + 0.10 * av and recent_rsi < prior_rsi - 3 and last.close < previous.low:
            return [_observation(
                snapshot, pattern_id=self.pattern_id, timeframe="H1",
                facts=(
                    "the recent six H1 bars made a higher high than the preceding ten-bar window",
                    "RSI(5) in the recent window is lower than RSI(5) in the preceding window",
                    "the latest completed H1 candle closed below the preceding H1 low",
                ),
                measurements={**common, "price_extreme_relation": "recent_high_above_prior_high",
                              "momentum_relation": "recent_rsi_below_prior_rsi"},
                source_bar=last,
            )]
        if recent_low < prior_low - 0.10 * av and recent_rsi > prior_rsi + 3 and last.close > previous.high:
            return [_observation(
                snapshot, pattern_id=self.pattern_id, timeframe="H1",
                facts=(
                    "the recent six H1 bars made a lower low than the preceding ten-bar window",
                    "RSI(5) in the recent window is higher than RSI(5) in the preceding window",
                    "the latest completed H1 candle closed above the preceding H1 high",
                ),
                measurements={**common, "price_extreme_relation": "recent_low_below_prior_low",
                              "momentum_relation": "recent_rsi_above_prior_rsi"},
                source_bar=last,
            )]
        return []


class EventWindowBreakoutDetector:
    """Measure break of a verified high-impact event's pre-event range."""
    pattern_id = "event_window_breakout"

    def observe(self, snapshot: MarketSnapshot) -> list[PatternObservation]:
        event = snapshot.event_window
        bars = snapshot.bars("M5")
        if (not snapshot.data_quality_ok or not snapshot.market_open or event is None
                or not event.is_active(snapshot.observed_at) or len(bars) < 15
                or event.impact.upper() not in {"HIGH", "CRITICAL"}):
            return []
        av = atr(bars)
        if av is None:
            return []
        last = bars[-1]
        buffer = 0.12 * av
        common = {
            "event_name": event.name, "event_impact": event.impact,
            "pre_event_high": event.pre_event_high, "pre_event_low": event.pre_event_low,
            "atr14": av, "break_buffer": buffer, "last_close": last.close,
        }
        if last.close > event.pre_event_high + buffer:
            return [_observation(
                snapshot, pattern_id=self.pattern_id, timeframe="M5",
                facts=(
                    f"verified event window is active for {event.name}",
                    "latest completed M5 close is above the supplied pre-event high plus buffer",
                    "event timestamps and pre-event boundaries came from the calendar/data adapter",
                ),
                measurements={**common, "escaped_boundary": "pre_event_high"},
                source_bar=last,
            )]
        if last.close < event.pre_event_low - buffer:
            return [_observation(
                snapshot, pattern_id=self.pattern_id, timeframe="M5",
                facts=(
                    f"verified event window is active for {event.name}",
                    "latest completed M5 close is below the supplied pre-event low minus buffer",
                    "event timestamps and pre-event boundaries came from the calendar/data adapter",
                ),
                measurements={**common, "escaped_boundary": "pre_event_low"},
                source_bar=last,
            )]
        return []


class CandleRangeTheoryDetector:
    """Measure CRT anchor sweep/reclaim and LTF structure facts; no CRT trade vote."""
    pattern_id = "crt_range_observation"

    def __init__(self, anchor_timeframe: str = "H1", sweep_atr_buffer: float = 0.05) -> None:
        if anchor_timeframe.upper() not in {"H1", "H4", "D1"}:
            raise ValueError("CRT anchor_timeframe must be H1, H4, or D1")
        if sweep_atr_buffer < 0:
            raise ValueError("sweep_atr_buffer must be non-negative")
        self.anchor_timeframe = anchor_timeframe.upper()
        self.sweep_atr_buffer = sweep_atr_buffer

    def observe(self, snapshot: MarketSnapshot) -> list[PatternObservation]:
        anchors, bars = snapshot.bars(self.anchor_timeframe), snapshot.bars("M5")
        if not snapshot.data_quality_ok or not snapshot.market_open or not anchors or len(bars) < 16:
            return []
        anchor, last, previous = anchors[-1], bars[-1], bars[-2]
        av = atr(bars)
        if av is None or anchor.timestamp >= last.timestamp:
            return []
        midpoint = (anchor.high + anchor.low) / 2.0
        buffer = self.sweep_atr_buffer * av
        common = {
            "anchor_timeframe": self.anchor_timeframe,
            "anchor_high": anchor.high, "anchor_low": anchor.low, "anchor_midpoint": midpoint,
            "anchor_close_time": anchor.timestamp.isoformat(), "atr14_m5": av,
            "sweep_buffer": buffer, "current_high": last.high, "current_low": last.low,
            "current_close": last.close, "previous_m5_high": previous.high,
            "previous_m5_low": previous.low,
        }
        # These are factual boundary/structure relationships only. There is no
        # emitted trade side, entry, target, score or stop.
        if (last.low < anchor.low - buffer and last.close > anchor.low
                and last.close > last.open and last.close > previous.high):
            return [_observation(
                snapshot, pattern_id=self.pattern_id, timeframe="M5",
                facts=(
                    f"completed {self.anchor_timeframe} anchor low was swept by the latest M5 candle",
                    "the M5 candle closed back above the anchor low",
                    "the completed M5 close exceeded the preceding M5 high after the sweep",
                    "the anchor midpoint is a measurable reference level, not an automatic target",
                ),
                measurements={**common, "swept_boundary": "anchor_low",
                              "reclaimed_boundary": "anchor_low",
                              "structure_boundary_broken": "previous_m5_high"},
                source_bar=last,
            )]
        if (last.high > anchor.high + buffer and last.close < anchor.high
                and last.close < last.open and last.close < previous.low):
            return [_observation(
                snapshot, pattern_id=self.pattern_id, timeframe="M5",
                facts=(
                    f"completed {self.anchor_timeframe} anchor high was swept by the latest M5 candle",
                    "the M5 candle closed back below the anchor high",
                    "the completed M5 close fell below the preceding M5 low after the sweep",
                    "the anchor midpoint is a measurable reference level, not an automatic target",
                ),
                measurements={**common, "swept_boundary": "anchor_high",
                              "reclaimed_boundary": "anchor_high",
                              "structure_boundary_broken": "previous_m5_low"},
                source_bar=last,
            )]
        return []


def default_pattern_detectors() -> list[PatternDetector]:
    """Register measurements, not autonomous strategies or decision-makers."""
    return [
        TrendContinuationDetector(),
        LiquiditySweepDetector(),
        VolatilityExpansionDetector(),
        FailedBreakoutDetector(),
        StatisticalReversionDetector(),
        TrendExhaustionDetector(),
        EventWindowBreakoutDetector(),
        CandleRangeTheoryDetector(),
    ]
