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
]


@dataclass
class ScoringWeights:
    structure_weight: int = 20
    order_block_weight: int = 20
    fvg_weight: int = 15
    mtf_confluence_weight: int = 15
    session_weight: int = 10
    news_weight: int = 10
    currency_strength_weight: int = 10

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
        }


class ScoreOptimizer:
    """
    Adjusts the 7-factor confluence scoring weights based on actual trade
    performance. Changes are gradual — max 3 points per optimisation cycle
    — so the system evolves, never lurches.
    """

    DEFAULT_PATH = "data/scoring_weights.json"
    MAX_SHIFT = 3
    MIN_WEIGHT = 3

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

        effectiveness = self.get_factor_effectiveness(trades)
        lifts = {k: v["lift"] for k, v in effectiveness.items() if v["sample_present"] >= 10}

        if not lifts:
            return self.current_weights

        current = self.current_weights.as_dict()
        raw_new: dict[str, float] = {}
        for key in FACTOR_KEYS:
            lift = lifts.get(key, 0.0)
            shift = max(-self.MAX_SHIFT, min(self.MAX_SHIFT, round(lift * 10)))
            raw_new[key] = max(self.MIN_WEIGHT, current[key] + shift)

        raw_total = sum(raw_new.values())
        if raw_total == 0:
            return self.current_weights

        scaled = {k: max(self.MIN_WEIGHT, round(v / raw_total * 100)) for k, v in raw_new.items()}
        remainder = 100 - sum(scaled.values())
        best_key = max(scaled, key=lambda k: scaled[k])
        scaled[best_key] += remainder

        new_weights = ScoringWeights(
            structure_weight=scaled["structure"],
            order_block_weight=scaled["order_block"],
            fvg_weight=scaled["fvg"],
            mtf_confluence_weight=scaled["mtf_confluence"],
            session_weight=scaled["session"],
            news_weight=scaled["news"],
            currency_strength_weight=scaled["currency_strength"],
        )

        self.current_weights = new_weights
        self.save_weights()
        logger.info(f"Weights optimised — total={new_weights.total}")
        return new_weights

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
