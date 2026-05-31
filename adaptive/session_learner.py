"""
APEX TRADER — Session Learner
London overlap is my goldmine. Late Tokyo is a graveyard.
I learn exactly when my edge is sharpest.
"""

import json
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional

import numpy as np
from loguru import logger


@dataclass
class SessionProfile:
    session: str
    win_rate: float = 0.0
    avg_pnl: float = 0.0
    best_pairs: list[str] = field(default_factory=list)
    optimal_scan_frequency: int = 30
    total_trades: int = 0
    recommendation: str = "NORMAL"


class SessionLearner:
    """
    Learns optimal trading behaviour per session window.
    Returns an aggression level that the scanner and trigger
    use to adjust scan frequency and score thresholds.
    """

    MIN_TRADES = 15
    SAVE_PATH = "data/ml_session_profiles.json"

    def __init__(self) -> None:
        self._profiles: dict[str, SessionProfile] = {}
        self._load()

    def learn(self, trades: list[dict]) -> dict[str, SessionProfile]:
        grouped: dict[str, list[dict]] = {}
        for t in trades:
            session = str(t.get("session", "unknown"))
            grouped.setdefault(session, []).append(t)

        profiles: dict[str, SessionProfile] = {}
        for session, group in grouped.items():
            profiles[session] = self._build_profile(session, group)

        self._profiles = profiles
        self._save()
        return profiles

    def get_session_aggression(self, session: str) -> str:
        profile = self._profiles.get(session)
        if profile is None:
            return "NORMAL"
        return profile.recommendation

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _save(self) -> None:
        p = Path(self.SAVE_PATH)
        p.parent.mkdir(parents=True, exist_ok=True)
        data = {k: asdict(v) for k, v in self._profiles.items()}
        p.write_text(json.dumps(data, indent=2, default=str))
        logger.info(f"Session profiles saved to {self.SAVE_PATH}")

    def _load(self) -> None:
        p = Path(self.SAVE_PATH)
        if not p.exists():
            return
        try:
            raw = json.loads(p.read_text())
            self._profiles = {k: SessionProfile(**v) for k, v in raw.items()}
            logger.info(f"Session profiles loaded from {self.SAVE_PATH}")
        except Exception as exc:
            logger.warning(f"SessionLearner: could not load profiles: {exc}")

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _build_profile(self, session: str, trades: list[dict]) -> SessionProfile:
        pnls = [float(t.get("pnl", 0)) for t in trades]
        n = len(pnls)
        wins = [p for p in pnls if p > 0]
        wr = len(wins) / n if n else 0.0
        avg_pnl = float(np.mean(pnls)) if pnls else 0.0

        pair_pnl: dict[str, list[float]] = {}
        for t in trades:
            pair_pnl.setdefault(str(t.get("pair", "unknown")), []).append(
                float(t.get("pnl", 0))
            )
        best_pairs = sorted(
            pair_pnl.keys(), key=lambda p: np.mean(pair_pnl[p]), reverse=True
        )[:5]

        if n < self.MIN_TRADES:
            rec = "NORMAL"
            scan_freq = 30
        elif wr >= 0.75 and avg_pnl > 0:
            rec = "AGGRESSIVE"
            scan_freq = 10
        elif wr >= 0.55:
            rec = "NORMAL"
            scan_freq = 30
        elif wr >= 0.40:
            rec = "CAUTIOUS"
            scan_freq = 60
        else:
            rec = "AVOID"
            scan_freq = 300

        return SessionProfile(
            session=session,
            win_rate=round(wr, 4),
            avg_pnl=round(avg_pnl, 4),
            best_pairs=best_pairs,
            optimal_scan_frequency=scan_freq,
            total_trades=n,
            recommendation=rec,
        )
