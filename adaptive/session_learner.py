"""
APEX TRADER — Session Learner
London overlap is my goldmine. Late Tokyo is a graveyard.
I learn exactly when my edge is sharpest.
"""

import json
from dataclasses import dataclass, field, asdict
from pathlib import Path

import numpy as np
from loguru import logger

# Per-user writeable state — resolve the learned-profile file under the owning
# user's data tree (APEX_DATA_DIR) rather than a bare cwd-relative ``data/`` path.
from runtime_paths import data_dir as _data_dir


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

    Tier 4: when ``per_symbol_enabled`` is on, the learner also keeps a
    ``SYMBOL|SESSION`` compound profile alongside the global per-session ones.
    The compound profile is only consulted once it has ``per_symbol_min_trades``
    samples; below that the global per-session profile is used. Per-symbol
    profiles always accumulate — the threshold only gates their USE.
    """

    MIN_TRADES = 15
    SAVE_PATH = "data/ml_session_profiles.json"

    # Per-symbol defaults (overridable via SessionLearnerConfig). A bare
    # SessionLearner() with no config keeps per-symbol ON by default to match
    # production; only the read-gate (sample size) controls whether it is used.
    PER_SYMBOL_DEFAULT = True
    PER_SYMBOL_MIN_TRADES = 100

    # Compound-key separator for ``SYMBOL|SESSION`` profiles.
    _SYMBOL_SEP = "|"

    def __init__(self, config=None) -> None:
        self.per_symbol_enabled = bool(
            getattr(config, "per_symbol_enabled", self.PER_SYMBOL_DEFAULT)
        )
        self.per_symbol_min_trades = int(
            getattr(config, "per_symbol_min_trades", self.PER_SYMBOL_MIN_TRADES)
        )
        # Global per-session profiles (session → profile).
        self._profiles: dict[str, SessionProfile] = {}
        # Per-symbol compound profiles (``SYMBOL|SESSION`` → profile).
        self._symbol_profiles: dict[str, SessionProfile] = {}
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

        # Per-symbol compound profiles (always accumulate; read-gated on use).
        if self.per_symbol_enabled:
            sym_grouped: dict[str, list[dict]] = {}
            for t in trades:
                sym = str(t.get("pair", "") or "")
                if not sym:
                    continue
                session = str(t.get("session", "unknown"))
                key = f"{sym}{self._SYMBOL_SEP}{session}"
                sym_grouped.setdefault(key, []).append(t)
            symbol_profiles: dict[str, SessionProfile] = {}
            for key, group in sym_grouped.items():
                session = key.split(self._SYMBOL_SEP, 1)[1]
                symbol_profiles[key] = self._build_profile(session, group)
            self._symbol_profiles = symbol_profiles

        self._save()
        return profiles

    def get_session_aggression(self, session: str, symbol: str | None = None) -> str:
        # Per-symbol takes priority once the compound bucket is large enough.
        if symbol and self.per_symbol_enabled:
            prof = self._symbol_profiles.get(
                f"{symbol}{self._SYMBOL_SEP}{session}"
            )
            if prof is not None and prof.total_trades >= self.per_symbol_min_trades:
                return prof.recommendation
        profile = self._profiles.get(session)
        if profile is None:
            return "NORMAL"
        return profile.recommendation

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _save(self) -> None:
        from persistence.atomic_write import atomic_write_text

        p = _data_dir() / Path(self.SAVE_PATH).name
        data = {k: asdict(v) for k, v in self._profiles.items()}
        # Per-symbol compound profiles share the same file under
        # ``SYMBOL|SESSION`` keys — they never collide with bare session keys.
        for k, v in self._symbol_profiles.items():
            data[k] = asdict(v)
        atomic_write_text(p, json.dumps(data, indent=2, default=str))
        logger.info(f"Session profiles saved to {self.SAVE_PATH}")

    def _load(self) -> None:
        p = _data_dir() / Path(self.SAVE_PATH).name
        if not p.exists():
            return
        try:
            raw = json.loads(p.read_text())
            glob: dict[str, SessionProfile] = {}
            sym: dict[str, SessionProfile] = {}
            for k, v in raw.items():
                if self._SYMBOL_SEP in k:
                    sym[k] = SessionProfile(**v)
                else:
                    glob[k] = SessionProfile(**v)
            self._profiles = glob
            self._symbol_profiles = sym
            logger.info(f"Session profiles loaded from {self.SAVE_PATH}")
        except Exception as exc:
            logger.warning(f"SessionLearner: could not load profiles: {exc}")

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _build_profile(self, session: str, trades: list[dict]) -> SessionProfile:
        from adaptive.recency_weight import (
            has_recency_weights,
            trade_weight,
            weighted_mean,
            weighted_win_rate,
        )

        pnls = [float(t.get("pnl", 0)) for t in trades]
        n = len(pnls)
        if has_recency_weights(trades):
            # Time-decayed stats: older trades (stale regime) count less.
            weights = [trade_weight(t) for t in trades]
            wr = weighted_win_rate(pnls, weights)
            avg_pnl = weighted_mean(pnls, weights) if pnls else 0.0
        else:
            wins = [p for p in pnls if p > 0]
            losses = [p for p in pnls if p < 0]
            # Scratch trades (pnl == 0) are excluded from the win-rate denominator.
            decided = len(wins) + len(losses)
            wr = len(wins) / decided if decided else 0.0
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
