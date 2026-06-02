"""
APEX TRADER — Score Optimizer
The scoring system starts at default weights.
Over time, I learn which factors actually predict winners.
If FVG entries win 90% but OB entries only win 60%,
I shift the weight. Adapt or die.
"""

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional

import numpy as np
from loguru import logger


FACTOR_KEYS = [
    "structure",
    "order_block",
    "fvg",
    "mtf_confluence",
    "session",
    "news",
    "currency_strength",
    "m1_trigger",
    "liquidity_sweep",
]


@dataclass
class ScoringWeights:
    structure_weight: int = 17
    order_block_weight: int = 17
    fvg_weight: int = 13
    mtf_confluence_weight: int = 13
    session_weight: int = 9
    news_weight: int = 8
    currency_strength_weight: int = 8
    m1_trigger_weight: int = 8
    liquidity_sweep_weight: int = 7

    @property
    def total(self) -> int:
        return (
            self.structure_weight
            + self.order_block_weight
            + self.fvg_weight
            + self.mtf_confluence_weight
            + self.session_weight
            + self.news_weight
            + self.currency_strength_weight
            + self.m1_trigger_weight
            + self.liquidity_sweep_weight
        )

    def as_dict(self) -> dict[str, int]:
        return {
            "structure": self.structure_weight,
            "order_block": self.order_block_weight,
            "fvg": self.fvg_weight,
            "mtf_confluence": self.mtf_confluence_weight,
            "session": self.session_weight,
            "news": self.news_weight,
            "currency_strength": self.currency_strength_weight,
            "m1_trigger": self.m1_trigger_weight,
            "liquidity_sweep": self.liquidity_sweep_weight,
        }


class ScoreOptimizer:
    """
    Adjusts the 9-factor confluence scoring weights based on actual trade
    performance. Changes are gradual — max 3 points per optimisation cycle
    — so the system evolves, never lurches.

    An out-of-sample validation gate ensures candidate weights are adopted
    only when they do not degrade win/loss discrimination on held-out
    (later-in-time) trades, preventing in-sample overfit.
    """

    DEFAULT_PATH = "data/scoring_weights.json"
    MAX_SHIFT = 3
    MIN_WEIGHT = 3
    VALIDATION_RATIO = 0.30
    MIN_VALIDATION = 15

    def __init__(self) -> None:
        self.current_weights = ScoringWeights()
        self.load_weights()

    def optimize(
        self, trades: list[dict], min_trades: int = 50
    ) -> ScoringWeights:
        if len(trades) < min_trades:
            logger.info(
                f"Only {len(trades)} trades — need {min_trades} before optimising"
            )
            return self.current_weights

        sorted_trades = self._sort_by_time(trades)

        split_idx = int(len(sorted_trades) * (1 - self.VALIDATION_RATIO))
        train = sorted_trades[:split_idx]
        validation = sorted_trades[split_idx:]

        if len(validation) < self.MIN_VALIDATION:
            logger.info(
                "OOS gate skipped — validation set too small "
                f"({len(validation)} < {self.MIN_VALIDATION}), "
                f"fitting on all {len(trades)} trades"
            )
            return self._fit_and_adopt(sorted_trades)

        candidate = self._fit_weights(train)
        if candidate is None:
            return self.current_weights

        candidate_metric = self._compute_validation_metric(candidate, validation)
        incumbent_metric = self._compute_validation_metric(
            self.current_weights, validation
        )

        if candidate_metric >= incumbent_metric:
            self.current_weights = candidate
            self.save_weights()
            logger.info(
                "Weights ADOPTED — OOS separation: "
                f"candidate={candidate_metric:.4f} >= "
                f"incumbent={incumbent_metric:.4f} | "
                f"train={len(train)} validation={len(validation)} | "
                f"total={candidate.total}"
            )
            return candidate

        logger.info(
            "Weights REJECTED — OOS separation: "
            f"candidate={candidate_metric:.4f} < "
            f"incumbent={incumbent_metric:.4f} | "
            f"train={len(train)} validation={len(validation)} — "
            "incumbent retained"
        )
        return self.current_weights

    def _fit_weights(self, trades: list[dict]) -> Optional[ScoringWeights]:
        """Fit candidate weights from the given trades. Returns None if insufficient lift data."""
        effectiveness = self.get_factor_effectiveness(trades)
        lifts = {
            k: v["lift"]
            for k, v in effectiveness.items()
            if v["sample_present"] >= 10
        }

        if not lifts:
            return None

        current = self.current_weights.as_dict()
        raw_new: dict[str, float] = {}
        for key in FACTOR_KEYS:
            lift = lifts.get(key, 0.0)
            shift = max(-self.MAX_SHIFT, min(self.MAX_SHIFT, round(lift * 10)))
            raw_new[key] = max(self.MIN_WEIGHT, current[key] + shift)

        raw_total = sum(raw_new.values())
        if raw_total == 0:
            return None

        scaled = {
            k: max(self.MIN_WEIGHT, round(v / raw_total * 100))
            for k, v in raw_new.items()
        }
        remainder = 100 - sum(scaled.values())
        best_key = max(scaled, key=lambda k: scaled[k])
        scaled[best_key] += remainder

        return ScoringWeights(
            structure_weight=scaled["structure"],
            order_block_weight=scaled["order_block"],
            fvg_weight=scaled["fvg"],
            mtf_confluence_weight=scaled["mtf_confluence"],
            session_weight=scaled["session"],
            news_weight=scaled["news"],
            currency_strength_weight=scaled["currency_strength"],
            m1_trigger_weight=scaled["m1_trigger"],
            liquidity_sweep_weight=scaled["liquidity_sweep"],
        )

    def _fit_and_adopt(self, trades: list[dict]) -> ScoringWeights:
        """Fit on all trades and adopt without OOS validation (small-sample fallback)."""
        candidate = self._fit_weights(trades)
        if candidate is None:
            return self.current_weights
        self.current_weights = candidate
        self.save_weights()
        logger.info(f"Weights optimised (no OOS gate) — total={candidate.total}")
        return candidate

    @staticmethod
    def _compute_validation_metric(
        weights: ScoringWeights, trades: list[dict]
    ) -> float:
        """
        Measure how well weights discriminate winners from losers.

        For each trade, weighted_score = sum of weights for factors present
        in the trade's confluences_tags. Then:
            separation = mean(win_scores) - mean(loss_scores)

        Higher separation means better discrimination. Returns 0.0 when all
        trades are wins or all are losses (no separation measurable).
        """
        weight_dict = weights.as_dict()
        win_scores: list[float] = []
        loss_scores: list[float] = []

        for t in trades:
            tags = t.get("confluences_tags", [])
            score = sum(weight_dict.get(tag, 0) for tag in tags)
            if t.get("pnl", 0) > 0:
                win_scores.append(score)
            else:
                loss_scores.append(score)

        if not win_scores or not loss_scores:
            return 0.0

        return (sum(win_scores) / len(win_scores)) - (
            sum(loss_scores) / len(loss_scores)
        )

    @staticmethod
    def _sort_by_time(trades: list[dict]) -> list[dict]:
        """
        Sort trades by timestamp ascending for a temporal train/validation
        split. Falls back to preserving input order (assumed chronological
        from the journal's row-insertion order) when no timestamps present.
        """
        has_timestamps = any(t.get("timestamp") for t in trades)
        if not has_timestamps:
            return list(trades)
        return sorted(trades, key=lambda t: t.get("timestamp", ""))

    def get_factor_effectiveness(self, trades: list[dict]) -> dict[str, dict]:
        results: dict[str, dict] = {}
        for factor in FACTOR_KEYS:
            present = [t for t in trades if factor in t.get("confluences_tags", [])]
            absent = [t for t in trades if factor not in t.get("confluences_tags", [])]

            wr_present = self._win_rate(present)
            wr_absent = self._win_rate(absent)

            results[factor] = {
                "win_rate_when_present": round(wr_present, 4),
                "win_rate_when_absent": round(wr_absent, 4),
                "lift": round(wr_present - wr_absent, 4),
                "sample_present": len(present),
                "sample_absent": len(absent),
            }
        return results

    def save_weights(
        self,
        weights: Optional[ScoringWeights] = None,
        filepath: Optional[str] = None,
    ) -> None:
        filepath = filepath or self.DEFAULT_PATH
        weights = weights or self.current_weights
        p = Path(filepath)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(asdict(weights), indent=2))
        logger.info(f"Weights saved to {filepath}")

    def load_weights(self, filepath: Optional[str] = None) -> ScoringWeights:
        filepath = filepath or self.DEFAULT_PATH
        p = Path(filepath)
        if not p.exists():
            logger.info("No saved weights found — using defaults")
            return ScoringWeights()
        data = json.loads(p.read_text())
        weights = ScoringWeights(**{k: v for k, v in data.items() if k in ScoringWeights.__dataclass_fields__})
        self.current_weights = weights
        logger.info(f"Weights loaded from {filepath} — total={weights.total}")
        return weights

    @staticmethod
    def _win_rate(trades: list[dict]) -> float:
        if not trades:
            return 0.0
        wins = sum(1 for t in trades if t.get("pnl", 0) > 0)
        return wins / len(trades)
