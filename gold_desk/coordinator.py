"""Strategy arbitration: observe every family, retain provenance, never count votes."""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
import logging
from typing import Sequence

from gold_desk.models import Direction, MarketSnapshot, TradeIdea
from gold_desk.strategies import Strategy, default_strategies

logger = logging.getLogger(__name__)


class GoldOnlyGuard:
    """Exact broker-symbol allowlist; never accept symbols by prefix alone."""
    DEFAULT_ALIASES = ("XAUUSD", "GOLD", "XAUUSDm", "XAUUSD.M", "XAUUSD.PRO", "XAUUSD.A")

    def __init__(self, allowed_symbols: Sequence[str] | None = None) -> None:
        aliases = allowed_symbols or self.DEFAULT_ALIASES
        self._aliases = {str(value).strip().upper(): "XAUUSD" for value in aliases if str(value).strip()}
        self._aliases["XAUUSD"] = "XAUUSD"

    def canonical_symbol(self, raw: str) -> str | None:
        return self._aliases.get(str(raw or "").strip().upper())

    def is_gold(self, raw: str) -> bool:
        return self.canonical_symbol(raw) == "XAUUSD"


@dataclass(frozen=True, slots=True)
class CoordinatorConfig:
    min_quality_score: float = 50.0
    min_reward_to_risk: float = 1.20
    max_spread_to_atr: float = 0.20
    conflict_score_gap: float = 12.0
    max_m5_bar_age_seconds: float = 900.0


@dataclass(frozen=True, slots=True)
class EvaluationResult:
    observed_at: datetime
    ideas: tuple[TradeIdea, ...] = ()
    diagnostics: tuple[str, ...] = ()

    @property
    def has_opportunity(self) -> bool:
        return bool(self.ideas)


class StrategyCoordinator:
    """Evaluate all strategies, retain provenance and fail closed on conflicts.

    Scores are ranking heuristics, not probabilities. Overlapping detections are
    coalesced and scores are not summed. If opposite-direction setups have similar
    scores, abstain instead of pretending a vote tally reveals market direction.
    """

    def __init__(
        self,
        strategies: Sequence[Strategy] | None = None,
        *,
        config: CoordinatorConfig | None = None,
        gold_guard: GoldOnlyGuard | None = None,
    ) -> None:
        self.strategies = list(strategies or default_strategies())
        self.config = config or CoordinatorConfig()
        self.gold_guard = gold_guard or GoldOnlyGuard()

    def evaluate(self, snapshot: MarketSnapshot) -> EvaluationResult:
        diagnostics: list[str] = []
        if not self.gold_guard.is_gold(snapshot.symbol):
            return EvaluationResult(snapshot.observed_at, (), (f"symbol rejected: {snapshot.symbol}",))
        if str(snapshot.venue).upper() not in {"MT5", "DERIV"}:
            return EvaluationResult(snapshot.observed_at, (), ("venue must be explicitly MT5 or DERIV",))
        if not snapshot.market_open:
            return EvaluationResult(snapshot.observed_at, (), ("market is closed",))
        if not snapshot.data_quality_ok:
            return EvaluationResult(snapshot.observed_at, (), ("market data quality flag is false",))
        latest = snapshot.latest_bar("M5")
        if latest is None:
            return EvaluationResult(snapshot.observed_at, (), ("no completed M5 candle",))
        bar_age = (snapshot.observed_at - latest.timestamp).total_seconds()
        if bar_age < 0 or bar_age > self.config.max_m5_bar_age_seconds:
            return EvaluationResult(snapshot.observed_at, (), (f"stale/invalid M5 candle age: {bar_age:.1f}s",))

        candidates: list[TradeIdea] = []
        for strategy in self.strategies:
            try:
                for idea in strategy.evaluate(snapshot) or ():
                    if not isinstance(idea, TradeIdea):
                        diagnostics.append(f"{strategy.strategy_id}: discarded non-TradeIdea result")
                        continue
                    if not self.gold_guard.is_gold(idea.symbol):
                        diagnostics.append(f"{strategy.strategy_id}: idea symbol rejected")
                        continue
                    if str(idea.venue).upper() != str(snapshot.venue).upper():
                        diagnostics.append(f"{strategy.strategy_id}: venue mismatch")
                        continue
                    if idea.expires_at <= snapshot.observed_at:
                        diagnostics.append(f"{strategy.strategy_id}: expired idea")
                        continue
                    if idea.quality_score < self.config.min_quality_score:
                        diagnostics.append(f"{strategy.strategy_id}: quality below threshold")
                        continue
                    if idea.reward_to_risk < self.config.min_reward_to_risk:
                        diagnostics.append(f"{strategy.strategy_id}: reward/risk below threshold")
                        continue
                    if idea.atr <= 0 or snapshot.spread / idea.atr > self.config.max_spread_to_atr:
                        diagnostics.append(f"{strategy.strategy_id}: spread too wide for current ATR")
                        continue
                    candidates.append(idea)
            except Exception as exc:
                message = f"{getattr(strategy, 'strategy_id', type(strategy).__name__)} failed: {type(exc).__name__}: {exc}"
                diagnostics.append(message)
                logger.exception("[gold-desk] %s", message)

        if not candidates:
            return EvaluationResult(snapshot.observed_at, (), tuple(diagnostics))

        # A bounded R-multiple tie-breaker is used; independent probabilities
        # are not available, and strategy scores are not added.
        ranked = sorted(
            candidates,
            key=lambda idea: (idea.quality_score, min(idea.reward_to_risk, 4.0)),
            reverse=True,
        )
        directions = {idea.direction for idea in ranked}
        if len(directions) > 1:
            best_by_direction: dict[Direction, TradeIdea] = {}
            for idea in ranked:
                best_by_direction.setdefault(idea.direction, idea)
            long_idea = best_by_direction.get(Direction.LONG)
            short_idea = best_by_direction.get(Direction.SHORT)
            assert long_idea is not None and short_idea is not None
            gap = abs(long_idea.quality_score - short_idea.quality_score)
            if gap < self.config.conflict_score_gap:
                diagnostics.append(
                    f"opposing setups conflict (quality gap {gap:.1f} < "
                    f"{self.config.conflict_score_gap:.1f}); abstaining"
                )
                return EvaluationResult(snapshot.observed_at, (), tuple(diagnostics))
            winner = long_idea if long_idea.quality_score > short_idea.quality_score else short_idea
            ranked = [idea for idea in ranked if idea.direction is winner.direction]
            diagnostics.append(f"opposing setup rejected in favour of {winner.direction.value}")

        # Current market-entry ideas all compete at one quote. Keep the best
        # individual thesis and merge labels/evidence only, never quality scores.
        winner = ranked[0]
        families: list[str] = []
        evidence: list[str] = []
        for idea in ranked:
            for family in (idea.strategy_id, *idea.supporting_strategies):
                if family not in families:
                    families.append(family)
            for item in idea.evidence:
                if item not in evidence:
                    evidence.append(item)
        merged = replace(winner, supporting_strategies=tuple(families), evidence=tuple(evidence))
        if len(families) > 1:
            diagnostics.append("coalesced overlapping ideas without summing quality scores")
        return EvaluationResult(snapshot.observed_at, (merged,), tuple(diagnostics))
