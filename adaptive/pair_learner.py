"""
APEX TRADER — Pair Learner
Not all pairs are equal. I crush GBPUSD but struggle with NZDJPY.
I learn my strengths and play to them.
"""

import json
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Optional

import numpy as np
from loguru import logger


@dataclass
class PairProfile:
    pair: str
    win_rate: float = 0.0
    avg_pnl: float = 0.0
    best_session: Optional[str] = None
    best_regime: Optional[str] = None
    avg_spread_cost: float = 0.0
    total_trades: int = 0
    confidence: float = 0.0
    recommendation: str = "INSUFFICIENT_DATA"


class PairLearner:
    """
    Learns which pairs the bot trades best and produces a confidence
    multiplier that feeds into position sizing.
    """

    MIN_TRADES = 20
    CONFIDENCE_FULL = 80
    SAVE_PATH = "data/ml_pair_profiles.json"

    def __init__(self) -> None:
        self._profiles: dict[str, PairProfile] = {}
        self._load()

    def learn(self, trades: list[dict]) -> dict[str, PairProfile]:
        grouped: dict[str, list[dict]] = {}
        for t in trades:
            pair = str(t.get("pair", "unknown"))
            grouped.setdefault(pair, []).append(t)

        profiles: dict[str, PairProfile] = {}
        for pair, group in grouped.items():
            profiles[pair] = self._build_profile(pair, group)

        self._profiles = profiles
        self._save()
        return profiles

    def get_pair_multiplier(self, pair: str) -> float:
        profile = self._profiles.get(pair)
        if profile is None or profile.total_trades < self.MIN_TRADES:
            return 0.8
        if profile.recommendation == "AVOID":
            return 0.0
        if profile.recommendation == "REDUCE_SIZE":
            return 0.7
        return 1.0

    def get_recommended_pairs(self) -> list[str]:
        return sorted(
            [p for p, prof in self._profiles.items() if prof.recommendation == "TRADE"],
            key=lambda p: self._profiles[p].avg_pnl,
            reverse=True,
        )

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _save(self) -> None:
        from persistence.atomic_write import atomic_write_text

        p = Path(self.SAVE_PATH)
        data = {k: asdict(v) for k, v in self._profiles.items()}
        atomic_write_text(p, json.dumps(data, indent=2, default=str))
        logger.info(f"Pair profiles saved to {self.SAVE_PATH}")

    def _load(self) -> None:
        p = Path(self.SAVE_PATH)
        if not p.exists():
            return
        try:
            raw = json.loads(p.read_text())
            self._profiles = {k: PairProfile(**v) for k, v in raw.items()}
            logger.info(f"Pair profiles loaded from {self.SAVE_PATH}")
        except Exception as exc:
            logger.warning(f"PairLearner: could not load profiles: {exc}")

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _build_profile(self, pair: str, trades: list[dict]) -> PairProfile:
        pnls = [float(t.get("pnl", 0)) for t in trades]
        n = len(pnls)
        wins = [p for p in pnls if p > 0]
        wr = len(wins) / n if n else 0.0
        avg_pnl = float(np.mean(pnls)) if pnls else 0.0
        confidence = min(1.0, n / self.CONFIDENCE_FULL)

        spreads = [float(t.get("spread", 0)) for t in trades if t.get("spread") is not None]
        avg_spread = float(np.mean(spreads)) if spreads else 0.0

        best_session = self._best_dim(trades, "session")
        best_regime = self._best_dim(trades, "regime")

        if n < self.MIN_TRADES:
            rec = "INSUFFICIENT_DATA"
        elif wr < 0.40 and n >= 30:
            rec = "AVOID"
        elif wr < 0.55:
            rec = "REDUCE_SIZE"
        else:
            rec = "TRADE"

        return PairProfile(
            pair=pair,
            win_rate=round(wr, 4),
            avg_pnl=round(avg_pnl, 4),
            best_session=best_session,
            best_regime=best_regime,
            avg_spread_cost=round(avg_spread, 4),
            total_trades=n,
            confidence=round(confidence, 4),
            recommendation=rec,
        )

    @staticmethod
    def _best_dim(trades: list[dict], key: str) -> Optional[str]:
        grouped: dict[str, list[float]] = {}
        for t in trades:
            grouped.setdefault(str(t.get(key, "unknown")), []).append(
                float(t.get("pnl", 0))
            )
        if not grouped:
            return None
        ranked = sorted(grouped.items(), key=lambda x: np.mean(x[1]), reverse=True)
        return ranked[0][0] if ranked else None
