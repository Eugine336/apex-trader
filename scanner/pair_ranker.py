"""
APEX TRADER — Pair Ranker
Breaks ties between multiple READY setups and ensures the best one fires first.
"""

from dataclasses import dataclass
from typing import Optional
from scanner.pair_scanner import PairScanResult


@dataclass
class RankedSetup:
    rank: int
    result: PairScanResult
    priority_score: float
    reason: str


class PairRanker:
    """
    When multiple pairs are READY at the same time, the ranker decides
    which one deserves the trigger pull. Considers regime strength,
    session timing, sweep presence, and correlation conflicts.
    """

    REGIME_BONUS = {"BULLISH": 5, "BEARISH": 5, "RANGING": 0}
    SESSION_BONUS = {True: 3, False: 0}

    def rank(
        self,
        ready_setups: list[PairScanResult],
        open_trades: Optional[list[str]] = None,
    ) -> list[RankedSetup]:
        if not ready_setups:
            return []

        open_trades = open_trades or []
        scored: list[RankedSetup] = []

        for result in ready_setups:
            priority = float(result.score)
            reasons: list[str] = [f"Base score {result.score}"]

            regime_bonus = self.REGIME_BONUS.get(result.regime, 0)
            priority += regime_bonus
            if regime_bonus:
                reasons.append(f"+{regime_bonus} regime")

            session_bonus = self.SESSION_BONUS.get(result.session_active, 0)
            priority += session_bonus
            if session_bonus:
                reasons.append(f"+{session_bonus} session")

            if result.sweep_detected:
                priority += 5
                reasons.append("+5 sweep")

            if result.volume_confirmation:
                priority += 3
                reasons.append("+3 volume")

            if result.inducement_detected:
                priority += 3
                reasons.append("+3 inducement")

            if result.wyckoff_phase in ("PHASE_C", "SPRING"):
                priority += 5
                reasons.append("+5 Wyckoff")

            if result.pair in open_trades:
                priority -= 30
                reasons.append("-30 already open")

            if self._has_correlation_conflict(result.pair, open_trades):
                priority -= 15
                reasons.append("-15 correlated pair open")

            scored.append(RankedSetup(
                rank=0,
                result=result,
                priority_score=priority,
                reason=", ".join(reasons),
            ))

        scored.sort(key=lambda s: s.priority_score, reverse=True)
        for idx, setup in enumerate(scored, 1):
            setup.rank = idx

        return scored

    @staticmethod
    def get_top_n(ranked: list[RankedSetup], n: int = 3) -> list[RankedSetup]:
        return ranked[:n]

    # ------------------------------------------------------------------
    @staticmethod
    def _has_correlation_conflict(pair: str, open_trades: list[str]) -> bool:
        """True if a correlated pair is already open (same base or quote)."""
        if len(pair) < 6:
            return False
        base = pair[:3]
        quote = pair[3:6]
        for op in open_trades:
            if len(op) < 6:
                continue
            if op[:3] in (base, quote) or op[3:6] in (base, quote):
                return True
        return False
