"""
APEX TRADER — Pair Learner
Not all pairs are equal. I crush GBPUSD but struggle with NZDJPY.
I learn my strengths and play to them.
"""

import json
import math
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Optional

import numpy as np
from loguru import logger

# Per-user writeable state — resolve the learned-profile file under the owning
# user's data tree (APEX_DATA_DIR) rather than a bare cwd-relative ``data/`` path.
from runtime_paths import data_dir as _data_dir


# ── Continuous-multiplier defaults ────────────────────────────────────────
# Mirror PairLearnerConfig so the learner has sane behaviour when constructed
# without a config (tests, ad-hoc use). Read via getattr so adaptive.* never
# has to import config.py (avoids a heavy/circular import at construction).
_DEF_CONTINUOUS_ENABLED = False
_DEF_MIDPOINT = 0.50
_DEF_STEEPNESS = 10.0
_DEF_FLOOR = 0.3
_DEF_CEILING = 1.2
_DEF_PRIOR = 0.8
_DEF_SHRINKAGE_FULL_WEIGHT = 30
_DEF_ABSOLUTE_FLOOR = 0.1
_DEF_COLD_START_MULT = 0.8
_DEF_COLD_START_MIN = 5
_DEF_ENTRY_SPLIT = False
_DEF_ENTRY_BLEND_WEIGHT = 0.3
_DEF_MGMT_LOG = True

# Maps the PostCloseTracker management-quality categories onto a [0,1] score so
# a per-pair management quality can feed sizing/diagnostics.
_MANAGEMENT_QUALITY_WEIGHTS = {
    "bad_signal": 0.0,
    "bad_management": 0.3,
    "conservative_management": 0.7,
    "good_management": 1.0,
}


def sigmoid_multiplier(
    win_rate: float,
    *,
    midpoint: float = _DEF_MIDPOINT,
    steepness: float = _DEF_STEEPNESS,
    floor: float = _DEF_FLOOR,
    ceiling: float = _DEF_CEILING,
) -> float:
    """Smoothly map a win rate to a size multiplier on a logistic curve.

    No discontinuities: adjacent win rates produce adjacent multipliers, so a
    56%% and a 75%% pair no longer collapse onto the same bucket value.
    """
    span = ceiling - floor
    try:
        logistic = 1.0 / (1.0 + math.exp(-steepness * (win_rate - midpoint)))
    except OverflowError:
        logistic = 0.0 if win_rate < midpoint else 1.0
    return floor + span * logistic


def compute_capture_ratio(
    realized_r: Optional[float], best_mfe_r: Optional[float]
) -> Optional[float]:
    """Fraction of the favourable move actually captured (realized R / MFE R).

    ``None`` when either input is missing or the move never went our way; a
    value well below 1.0 means money was left on the table (a management read,
    not a signal read).
    """
    if realized_r is None or best_mfe_r is None or best_mfe_r <= 1e-9:
        return None
    return round(float(realized_r) / float(best_mfe_r), 4)


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
    # Entry-vs-management split (populated from PostCloseTracker when wired).
    # Kept Optional so legacy profiles without these fields still load.
    entry_accuracy: Optional[float] = None
    management_score: Optional[float] = None
    optimal_sl_r: Optional[float] = None


class PairLearner:
    """
    Learns which pairs the bot trades best and produces a confidence
    multiplier that feeds into position sizing.
    """

    MIN_TRADES = 20
    CONFIDENCE_FULL = 80
    SAVE_PATH = "data/ml_pair_profiles.json"

    def __init__(self, config=None) -> None:
        self._profiles: dict[str, PairProfile] = {}
        # Optional post-close MFE/MAE tracker (injected by the main loop). Read
        # only — used to split entry quality from management quality. Never
        # mutated here; a missing tracker simply leaves the split fields None.
        self._post_close_tracker = None
        self._configure(config)
        self._load()

    # ------------------------------------------------------------------
    # Configuration
    # ------------------------------------------------------------------

    def _configure(self, config) -> None:
        """Resolve continuous-multiplier params from config (getattr defaults)."""

        def g(name, default):
            return getattr(config, name, default) if config is not None else default

        self.continuous_enabled = bool(g("continuous_pair_multiplier", _DEF_CONTINUOUS_ENABLED))
        self.continuous_midpoint = float(g("continuous_midpoint", _DEF_MIDPOINT))
        self.continuous_steepness = float(g("continuous_steepness", _DEF_STEEPNESS))
        self.continuous_floor = float(g("continuous_floor", _DEF_FLOOR))
        self.continuous_ceiling = float(g("continuous_ceiling", _DEF_CEILING))
        self.continuous_prior = float(g("continuous_prior", _DEF_PRIOR))
        self.shrinkage_full_weight = int(g("shrinkage_full_weight", _DEF_SHRINKAGE_FULL_WEIGHT))
        self.continuous_absolute_floor = float(g("continuous_absolute_floor", _DEF_ABSOLUTE_FLOOR))
        self.cold_start_multiplier = float(g("cold_start_multiplier", _DEF_COLD_START_MULT))
        self.cold_start_min_trades = int(g("cold_start_min_trades", _DEF_COLD_START_MIN))
        self.entry_management_split_enabled = bool(
            g("entry_management_split_enabled", _DEF_ENTRY_SPLIT)
        )
        self.entry_accuracy_blend_weight = float(
            g("entry_accuracy_blend_weight", _DEF_ENTRY_BLEND_WEIGHT)
        )
        self.management_quality_log_enabled = bool(
            g("management_quality_log_enabled", _DEF_MGMT_LOG)
        )

    def set_post_close_tracker(self, tracker) -> None:
        """Inject the live PostCloseTracker so learning can read per-pair signal
        accuracy / management quality. Read-only; safe to pass ``None``."""
        self._post_close_tracker = tracker

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
        if self.continuous_enabled:
            if profile is None:
                return self.cold_start_multiplier
            return round(self._continuous_multiplier(profile), 4)
        # Legacy 4-bucket behaviour (flag off).
        if profile is None or profile.total_trades < self.MIN_TRADES:
            return 0.8
        if profile.recommendation == "AVOID":
            return 0.0
        if profile.recommendation == "REDUCE_SIZE":
            return 0.7
        return 1.0

    # ------------------------------------------------------------------
    # Continuous multiplier
    # ------------------------------------------------------------------

    def _continuous_multiplier(self, profile: PairProfile) -> float:
        """Smooth, Bayesian-shrunk size multiplier for a learned pair.

        * Cold start (too few trades) → ``cold_start_multiplier``.
        * AVOID recommendation → hard 0.0 (safety floor preserved).
        * Otherwise a logistic curve over the *effective* win rate, shrunk
          toward the prior on thin samples and clamped to [floor, ceiling].
        """
        n = int(profile.total_trades or 0)
        if n < self.cold_start_min_trades:
            return self.cold_start_multiplier
        # Preserve the hard AVOID safety stop — a statistically-confident loser
        # is never sized up, regardless of the smooth curve.
        if profile.recommendation == "AVOID":
            return 0.0

        eff_wr = self._effective_win_rate(profile)
        raw = sigmoid_multiplier(
            eff_wr,
            midpoint=self.continuous_midpoint,
            steepness=self.continuous_steepness,
            floor=self.continuous_floor,
            ceiling=self.continuous_ceiling,
        )
        # Bayesian shrinkage: blend toward the prior until the sample is full.
        if self.shrinkage_full_weight > 0:
            shrink = min(n / self.shrinkage_full_weight, 1.0)
        else:
            shrink = 1.0
        mult = shrink * raw + (1.0 - shrink) * self.continuous_prior
        return max(self.continuous_absolute_floor, min(mult, self.continuous_ceiling))

    def _effective_win_rate(self, profile: PairProfile) -> float:
        """Win rate the sigmoid sees, optionally blended toward entry accuracy.

        If the split is on and the pair has entry-accuracy data, a low raw win
        rate driven by poor *management* (good signal, bad stops) is partially
        rescued so the pair is not avoided for a problem the read didn't cause.
        """
        wr = float(profile.win_rate or 0.0)
        if not self.entry_management_split_enabled or profile.entry_accuracy is None:
            return wr
        w = self.entry_accuracy_blend_weight
        return (1.0 - w) * wr + w * float(profile.entry_accuracy)

    def get_profile(self, pair: str) -> Optional[PairProfile]:
        """Read-only accessor for a learned pair profile (``None`` if unseen).

        Lets consumers read the observed per-pair win rate / sample size
        without reaching into private state. Does not mutate or recompute.
        """
        return self._profiles.get(pair)

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

        p = _data_dir() / Path(self.SAVE_PATH).name
        data = {k: asdict(v) for k, v in self._profiles.items()}
        atomic_write_text(p, json.dumps(data, indent=2, default=str))
        logger.info(f"Pair profiles saved to {self.SAVE_PATH}")

    def _load(self) -> None:
        p = _data_dir() / Path(self.SAVE_PATH).name
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

        entry_accuracy, management_score, optimal_sl_r = self._post_close_metrics(pair)

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
            entry_accuracy=entry_accuracy,
            management_score=management_score,
            optimal_sl_r=optimal_sl_r,
        )

    def _post_close_metrics(
        self, pair: str
    ) -> tuple[Optional[float], Optional[float], Optional[float]]:
        """Read per-pair entry/management metrics from the PostCloseTracker.

        Returns ``(entry_accuracy, management_score, optimal_sl_r)``; any is
        ``None`` when the tracker is absent or has no finalised rows for the
        pair. Never raises — a tracker read must not break learning.
        """
        tracker = self._post_close_tracker
        if tracker is None:
            return None, None, None
        try:
            mgmt = tracker.get_management_score(pair) or {}
            total = int(mgmt.get("total", 0) or 0)
            if total <= 0:
                # No finalised post-close rows yet — don't fabricate a 0.0.
                return None, None, None
            entry_accuracy = float(tracker.get_signal_accuracy(pair))
            fractions = mgmt.get("fractions", {}) or {}
            management_score = round(
                sum(
                    _MANAGEMENT_QUALITY_WEIGHTS.get(k, 0.0) * float(v)
                    for k, v in fractions.items()
                ),
                4,
            )
            sl_stats = tracker.get_optimal_sl_stats(pair) or {}
            median = sl_stats.get("median")
            optimal_sl_r = round(float(median), 4) if median is not None else None
            if self.management_quality_log_enabled:
                logger.debug(
                    "[pair_learner] {} post-close: entry_acc={:.2f} mgmt={:.2f} "
                    "optimal_sl_r={} (n={})",
                    pair, entry_accuracy, management_score,
                    optimal_sl_r if optimal_sl_r is not None else "n/a", total,
                )
            return entry_accuracy, management_score, optimal_sl_r
        except Exception as exc:  # noqa: BLE001
            logger.debug("[pair_learner] post-close read failed for {}: {}", pair, exc)
            return None, None, None

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
