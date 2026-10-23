"""Deterministic Gold pattern detectors used as evidence instruments.

These modules are not competing trading strategies in the active Apex runtime.
They inspect completed broker candles and expose factual conditions. The
runtime strips internal candidate geometry before sending a single neutral
evidence record to the existing CognitiveBrain. No module places orders or
owns the final directional decision.
"""
from __future__ import annotations

from datetime import timedelta
from statistics import median
from typing import Protocol, Sequence

from gold_desk.indicators import atr, ema, median_true_range, rsi, rolling_zscore, true_ranges
from gold_desk.models import Candle, Direction, MarketSnapshot, TradeIdea


class PatternDetector(Protocol):
    pattern_id: str

    def evaluate(self, snapshot: MarketSnapshot) -> list[TradeIdea]: ...


def _closes(bars: Sequence[Candle]) -> list[float]:
    return [bar.close for bar in bars]


def _idea(
    snapshot: MarketSnapshot,
    *,
    strategy_id: str,
    direction: Direction,
    stop: float,
    target: float,
    atr_value: float,
    quality: float,
    rationale: str,
    evidence: tuple[str, ...],
    timeframe: str = "M5",
    ttl_minutes: int = 15,
) -> TradeIdea | None:
    bars = snapshot.bars(timeframe)
    if not bars:
        return None
    try:
        return TradeIdea(
            symbol=snapshot.symbol,
            venue=str(snapshot.venue),
            direction=direction,
            strategy_id=strategy_id,
            entry=snapshot.mid,
            stop=float(stop),
            target=float(target),
            created_at=snapshot.observed_at,
            expires_at=snapshot.observed_at + timedelta(minutes=ttl_minutes),
            source_bar_time=bars[-1].timestamp,
            atr=float(atr_value),
            quality_score=max(0.0, min(100.0, float(quality))),
            rationale=rationale,
            evidence=evidence,
        )
    except (ValueError, TypeError):
        # Invalid structure (for example, a market gap has already crossed a
        # level) is not repaired into a trade. Rejecting is safer than guessing.
        return None


def _rr_target(entry: float, stop: float, direction: Direction, multiple: float) -> float:
    risk = abs(entry - stop)
    return entry + risk * multiple if direction is Direction.LONG else entry - risk * multiple


class TrendContinuation:
    pattern_id = "trend_continuation"

    def evaluate(self, snapshot: MarketSnapshot) -> list[TradeIdea]:
        bars = snapshot.bars("M15")
        if len(bars) < 55:
            return []
        closes = _closes(bars)
        fast, slow = ema(closes, 20), ema(closes, 50)
        av = atr(bars)
        if fast is None or slow is None or av is None:
            return []
        last, previous = bars[-1], bars[-2]
        recent = bars[-6:-1]
        if fast > slow and last.close > fast and last.close > previous.high and last.close > last.open:
            if min(b.low for b in recent) <= fast + 0.45 * av:
                stop = min(b.low for b in bars[-6:]) - 0.20 * av
                target = _rr_target(snapshot.mid, stop, Direction.LONG, 1.7)
                idea = _idea(snapshot, strategy_id=self.pattern_id, direction=Direction.LONG,
                             stop=stop, target=target, atr_value=av, quality=68,
                             rationale="H1/M15 trend proxy with M15 pullback reclaim and close-confirmed continuation.",
                             evidence=("EMA20 above EMA50", "pullback into EMA20 zone", "close above prior high"),
                             timeframe="M15", ttl_minutes=30)
                return [idea] if idea else []
        if fast < slow and last.close < fast and last.close < previous.low and last.close < last.open:
            if max(b.high for b in recent) >= fast - 0.45 * av:
                stop = max(b.high for b in bars[-6:]) + 0.20 * av
                target = _rr_target(snapshot.mid, stop, Direction.SHORT, 1.7)
                idea = _idea(snapshot, strategy_id=self.pattern_id, direction=Direction.SHORT,
                             stop=stop, target=target, atr_value=av, quality=68,
                             rationale="H1/M15 trend proxy with M15 pullback rejection and close-confirmed continuation.",
                             evidence=("EMA20 below EMA50", "pullback into EMA20 zone", "close below prior low"),
                             timeframe="M15", ttl_minutes=30)
                return [idea] if idea else []
        return []


class LiquiditySweepReversal:
    pattern_id = "liquidity_sweep_reversal"

    def evaluate(self, snapshot: MarketSnapshot) -> list[TradeIdea]:
        bars = snapshot.bars("M5")
        if len(bars) < 22:
            return []
        av = atr(bars)
        if av is None:
            return []
        last, prior = bars[-1], bars[-21:-1]
        range_high = max(b.high for b in prior)
        range_low = min(b.low for b in prior)
        if last.low < range_low - 0.05 * av and last.close > range_low and last.close > last.open:
            stop = last.low - 0.15 * av
            target = _rr_target(snapshot.mid, stop, Direction.LONG, 1.8)
            idea = _idea(snapshot, strategy_id=self.pattern_id, direction=Direction.LONG,
                         stop=stop, target=target, atr_value=av, quality=66,
                         rationale="Sell-side liquidity was swept and price closed back above the prior range low.",
                         evidence=("prior-range low swept", "close reclaimed range", "bullish rejection candle"))
            return [idea] if idea else []
        if last.high > range_high + 0.05 * av and last.close < range_high and last.close < last.open:
            stop = last.high + 0.15 * av
            target = _rr_target(snapshot.mid, stop, Direction.SHORT, 1.8)
            idea = _idea(snapshot, strategy_id=self.pattern_id, direction=Direction.SHORT,
                         stop=stop, target=target, atr_value=av, quality=66,
                         rationale="Buy-side liquidity was swept and price closed back below the prior range high.",
                         evidence=("prior-range high swept", "close reclaimed range", "bearish rejection candle"))
            return [idea] if idea else []
        return []


class VolatilityExpansionBreakout:
    pattern_id = "volatility_expansion_breakout"

    def evaluate(self, snapshot: MarketSnapshot) -> list[TradeIdea]:
        bars = snapshot.bars("M5")
        if len(bars) < 30:
            return []
        av = atr(bars)
        baseline = median_true_range(bars[-25:-1], window=18)
        if av is None or baseline is None:
            return []
        last, prior = bars[-1], bars[-21:-1]
        ceiling = max(b.high for b in prior)
        floor = min(b.low for b in prior)
        current_tr = true_ranges(bars)[-1]
        if current_tr < 1.25 * baseline:
            return []
        if last.close > ceiling + 0.05 * av and last.close > last.open:
            stop = ceiling - 0.25 * av
            target = _rr_target(snapshot.mid, stop, Direction.LONG, 1.6)
            idea = _idea(snapshot, strategy_id=self.pattern_id, direction=Direction.LONG,
                         stop=stop, target=target, atr_value=av, quality=65,
                         rationale="Range breakout accompanied by an expansion in true range.",
                         evidence=("close above prior 20-bar range", "true range expansion", "close-confirmed"))
            return [idea] if idea else []
        if last.close < floor - 0.05 * av and last.close < last.open:
            stop = floor + 0.25 * av
            target = _rr_target(snapshot.mid, stop, Direction.SHORT, 1.6)
            idea = _idea(snapshot, strategy_id=self.pattern_id, direction=Direction.SHORT,
                         stop=stop, target=target, atr_value=av, quality=65,
                         rationale="Range breakdown accompanied by an expansion in true range.",
                         evidence=("close below prior 20-bar range", "true range expansion", "close-confirmed"))
            return [idea] if idea else []
        return []


class FailedBreakoutTrap:
    pattern_id = "failed_breakout_trap"

    def evaluate(self, snapshot: MarketSnapshot) -> list[TradeIdea]:
        bars = snapshot.bars("M5")
        if len(bars) < 16:
            return []
        av = atr(bars)
        if av is None:
            return []
        last, prior = bars[-1], bars[-11:-1]
        ceiling, floor = max(b.high for b in prior), min(b.low for b in prior)
        body = max(abs(last.close - last.open), 0.02 * av)
        upper_wick = last.high - max(last.open, last.close)
        lower_wick = min(last.open, last.close) - last.low
        if last.high > ceiling + 0.10 * av and last.close < ceiling and last.close < last.open and upper_wick >= 1.2 * body:
            stop = last.high + 0.15 * av
            target = _rr_target(snapshot.mid, stop, Direction.SHORT, 1.7)
            idea = _idea(snapshot, strategy_id=self.pattern_id, direction=Direction.SHORT,
                         stop=stop, target=target, atr_value=av, quality=64,
                         rationale="Upside breakout attempt failed: wick outside range, close returned inside.",
                         evidence=("range-high false break", "upper rejection wick", "close back inside range"))
            return [idea] if idea else []
        if last.low < floor - 0.10 * av and last.close > floor and last.close > last.open and lower_wick >= 1.2 * body:
            stop = last.low - 0.15 * av
            target = _rr_target(snapshot.mid, stop, Direction.LONG, 1.7)
            idea = _idea(snapshot, strategy_id=self.pattern_id, direction=Direction.LONG,
                         stop=stop, target=target, atr_value=av, quality=64,
                         rationale="Downside breakout attempt failed: wick outside range, close returned inside.",
                         evidence=("range-low false break", "lower rejection wick", "close back inside range"))
            return [idea] if idea else []
        return []


class StatisticalMeanReversion:
    pattern_id = "statistical_mean_reversion"

    def __init__(self, z_threshold: float = 2.0, max_trend_atr: float = 1.5) -> None:
        self.z_threshold = z_threshold
        self.max_trend_atr = max_trend_atr

    def evaluate(self, snapshot: MarketSnapshot) -> list[TradeIdea]:
        bars = snapshot.bars("M15")
        if len(bars) < 55:
            return []
        closes = _closes(bars)
        z = rolling_zscore(closes, 30)
        av = atr(bars)
        fast, slow = ema(closes, 20), ema(closes, 50)
        if z is None or av is None or fast is None or slow is None:
            return []
        score, mean, _std = z
        if abs(fast - slow) / av > self.max_trend_atr:
            return []
        if score >= self.z_threshold and mean < snapshot.mid:
            stop = snapshot.mid + 1.25 * av
            idea = _idea(snapshot, strategy_id=self.pattern_id, direction=Direction.SHORT,
                         stop=stop, target=mean, atr_value=av, quality=59,
                         rationale=f"Price is {score:.2f} standard deviations above the 30-bar mean in a non-strong trend regime.",
                         evidence=("30-bar z-score extreme", "trend-strength filter passed", "mean is below entry"),
                         timeframe="M15", ttl_minutes=30)
            return [idea] if idea and idea.reward_to_risk >= 1.2 else []
        if score <= -self.z_threshold and mean > snapshot.mid:
            stop = snapshot.mid - 1.25 * av
            idea = _idea(snapshot, strategy_id=self.pattern_id, direction=Direction.LONG,
                         stop=stop, target=mean, atr_value=av, quality=59,
                         rationale=f"Price is {abs(score):.2f} standard deviations below the 30-bar mean in a non-strong trend regime.",
                         evidence=("30-bar z-score extreme", "trend-strength filter passed", "mean is above entry"),
                         timeframe="M15", ttl_minutes=30)
            return [idea] if idea and idea.reward_to_risk >= 1.2 else []
        return []


class TrendExhaustionReversal:
    pattern_id = "trend_exhaustion_reversal"

    def evaluate(self, snapshot: MarketSnapshot) -> list[TradeIdea]:
        bars = snapshot.bars("H1")
        if len(bars) < 32:
            return []
        av = atr(bars)
        if av is None:
            return []
        prior, recent = bars[-16:-6], bars[-6:]
        prior_rsi = rsi(_closes(prior), 5)
        recent_rsi = rsi(_closes(recent), 5)
        last, previous = bars[-1], bars[-2]
        if prior_rsi is None or recent_rsi is None:
            return []
        old_high = max(b.high for b in prior)
        new_high = max(b.high for b in recent)
        old_low = min(b.low for b in prior)
        new_low = min(b.low for b in recent)
        if new_high > old_high + 0.10 * av and recent_rsi < prior_rsi - 3 and last.close < previous.low:
            stop = max(b.high for b in recent) + 0.20 * av
            target = _rr_target(snapshot.mid, stop, Direction.SHORT, 1.6)
            idea = _idea(snapshot, strategy_id=self.pattern_id, direction=Direction.SHORT,
                         stop=stop, target=target, atr_value=av, quality=62,
                         rationale="Higher high with weaker RSI and a close below the prior H1 low suggests bullish momentum exhaustion.",
                         evidence=("price made a higher high", "RSI momentum failed to confirm", "H1 close broke prior low"),
                         timeframe="H1", ttl_minutes=60)
            return [idea] if idea else []
        if new_low < old_low - 0.10 * av and recent_rsi > prior_rsi + 3 and last.close > previous.high:
            stop = min(b.low for b in recent) - 0.20 * av
            target = _rr_target(snapshot.mid, stop, Direction.LONG, 1.6)
            idea = _idea(snapshot, strategy_id=self.pattern_id, direction=Direction.LONG,
                         stop=stop, target=target, atr_value=av, quality=62,
                         rationale="Lower low with stronger RSI and a close above the prior H1 high suggests bearish momentum exhaustion.",
                         evidence=("price made a lower low", "RSI momentum failed to confirm", "H1 close broke prior high"),
                         timeframe="H1", ttl_minutes=60)
            return [idea] if idea else []
        return []


class EventDrivenGoldBreakout:
    pattern_id = "event_driven_gold_breakout"

    def evaluate(self, snapshot: MarketSnapshot) -> list[TradeIdea]:
        event = snapshot.event_window
        bars = snapshot.bars("M5")
        if event is None or not event.is_active(snapshot.observed_at) or len(bars) < 15:
            return []
        av = atr(bars)
        if av is None or event.impact.upper() not in {"HIGH", "CRITICAL"}:
            return []
        last = bars[-1]
        buffer = 0.12 * av
        if last.close > event.pre_event_high + buffer and last.close > last.open:
            stop = event.pre_event_high - 0.15 * av
            target = _rr_target(snapshot.mid, stop, Direction.LONG, 1.6)
            idea = _idea(snapshot, strategy_id=self.pattern_id, direction=Direction.LONG,
                         stop=stop, target=target, atr_value=av, quality=63,
                         rationale=f"{event.name}: close-confirmed break above the verified pre-event range.",
                         evidence=("verified event window active", "pre-event high broken", "true range and close confirmation"))
            return [idea] if idea else []
        if last.close < event.pre_event_low - buffer and last.close < last.open:
            stop = event.pre_event_low + 0.15 * av
            target = _rr_target(snapshot.mid, stop, Direction.SHORT, 1.6)
            idea = _idea(snapshot, strategy_id=self.pattern_id, direction=Direction.SHORT,
                         stop=stop, target=target, atr_value=av, quality=63,
                         rationale=f"{event.name}: close-confirmed break below the verified pre-event range.",
                         evidence=("verified event window active", "pre-event low broken", "true range and close confirmation"))
            return [idea] if idea else []
        return []


class CandleRangeTheory:
    """CRT: closed anchor candle, sweep-and-reclaim, then lower-TF structure shift.

    The first objective is the anchor candle's midpoint. The system will not
    force a target when that midpoint does not offer acceptable reward-to-risk.
    """
    pattern_id = "crt"

    def __init__(self, anchor_timeframe: str = "H1", sweep_atr_buffer: float = 0.05) -> None:
        if anchor_timeframe.upper() not in {"H1", "H4", "D1"}:
            raise ValueError("CRT anchor_timeframe must be H1, H4, or D1")
        if sweep_atr_buffer < 0:
            raise ValueError("sweep_atr_buffer must be non-negative")
        self.anchor_timeframe = anchor_timeframe.upper()
        self.sweep_atr_buffer = sweep_atr_buffer

    def evaluate(self, snapshot: MarketSnapshot) -> list[TradeIdea]:
        anchors, bars = snapshot.bars(self.anchor_timeframe), snapshot.bars("M5")
        if len(anchors) < 1 or len(bars) < 16:
            return []
        anchor, last, previous = anchors[-1], bars[-1], bars[-2]
        av = atr(bars)
        if av is None or anchor.timestamp >= last.timestamp:
            return []
        midpoint = (anchor.high + anchor.low) / 2.0
        buffer = self.sweep_atr_buffer * av
        # A wick alone is insufficient. The sweep must reclaim the range boundary
        # and the M5 close must break the previous M5 candle's structure.
        if (last.low < anchor.low - buffer and last.close > anchor.low
                and last.close > last.open and last.close > previous.high):
            stop = last.low - 0.15 * av
            idea = _idea(snapshot, strategy_id=self.pattern_id, direction=Direction.LONG,
                         stop=stop, target=midpoint, atr_value=av, quality=70,
                         rationale=f"{self.anchor_timeframe} CRT bullish sweep/reclaim with M5 bullish structure shift; midpoint is first objective.",
                         evidence=(f"completed {self.anchor_timeframe} anchor range", "sell-side sweep", "close reclaimed anchor low", "M5 close broke prior high"),
                         ttl_minutes=20)
            return [idea] if idea and idea.reward_to_risk >= 1.2 else []
        if (last.high > anchor.high + buffer and last.close < anchor.high
                and last.close < last.open and last.close < previous.low):
            stop = last.high + 0.15 * av
            idea = _idea(snapshot, strategy_id=self.pattern_id, direction=Direction.SHORT,
                         stop=stop, target=midpoint, atr_value=av, quality=70,
                         rationale=f"{self.anchor_timeframe} CRT bearish sweep/reclaim with M5 bearish structure shift; midpoint is first objective.",
                         evidence=(f"completed {self.anchor_timeframe} anchor range", "buy-side sweep", "close reclaimed anchor high", "M5 close broke prior low"),
                         ttl_minutes=20)
            return [idea] if idea and idea.reward_to_risk >= 1.2 else []
        return []


def default_pattern_detectors() -> list[PatternDetector]:
    """Run all evidence detectors regardless of AI/provider availability."""
    return [
        TrendContinuation(),
        LiquiditySweepReversal(),
        VolatilityExpansionBreakout(),
        FailedBreakoutTrap(),
        StatisticalMeanReversion(),
        TrendExhaustionReversal(),
        EventDrivenGoldBreakout(),
        CandleRangeTheory(),
    ]
