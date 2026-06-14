"""
APEX TRADER — Score Optimizer
The scoring system starts at default weights matching the live scanner's
12-factor baseline magnitudes. Over time, I learn which factors actually
predict winners. If FVG entries win 90% but OB entries only win 60%,
I shift the weight — within bounded safety envelopes. Adapt or die.

Factor semantics (verified against scanner/pair_scanner.py):
  - structure:           binary (20 pts if bias tradeable)
  - ob_h1, ob_m5:        graduated by OB strength STRONG×1.0 / MODERATE×0.7 / WEAK×0.4
  - fvg:                 graduated by FVG strength STRONG×1.0 / MODERATE×0.7 / WEAK×0.4
  - mtf_confluence:      binary (15 pts if multi-TF FVG overlap)
  - session:             binary (10 pts if session active)
  - news:                binary (10 pts if news clear)
  - currency_strength:   binary (10 pts if aligned)
  - liquidity_sweep:     binary (8 pts if sweep detected)
  - volume:              binary (5 pts confirmation credit; −5 climax penalty is separate/hardcoded)
  - inducement:          binary (5 pts if detected)
  - wyckoff:             binary (5 pts if spring/upthrust)
"""

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional

from loguru import logger


FACTOR_KEYS = [
    "structure",
    "ob_h1",
    "ob_m5",
    "fvg",
    "mtf_confluence",
    "session",
    "news",
    "currency_strength",
    "liquidity_sweep",
    "volume",
    "inducement",
    "wyckoff",
]

ADAPTIVE_WEIGHT_ENVELOPE_PCT = 0.25

_OLD_TO_NEW_KEY_MAP = {
    "order_block_weight": ("ob_h1_weight", "ob_m5_weight"),
    "m1_trigger_weight": None,
}


@dataclass
class ScoringWeights:
    structure_weight: int = 20
    ob_h1_weight: int = 10
    ob_m5_weight: int = 10
    fvg_weight: int = 15
    mtf_confluence_weight: int = 15
    session_weight: int = 10
    news_weight: int = 10
    currency_strength_weight: int = 10
    liquidity_sweep_weight: int = 8
    volume_weight: int = 5
    inducement_weight: int = 5
    wyckoff_weight: int = 5

    @property
    def total(self) -> int:
        return (
            self.structure_weight
            + self.ob_h1_weight
            + self.ob_m5_weight
            + self.fvg_weight
            + self.mtf_confluence_weight
            + self.session_weight
            + self.news_weight
            + self.currency_strength_weight
            + self.liquidity_sweep_weight
            + self.volume_weight
            + self.inducement_weight
            + self.wyckoff_weight
        )

    def as_dict(self) -> dict[str, int]:
        return {
            "structure": self.structure_weight,
            "ob_h1": self.ob_h1_weight,
            "ob_m5": self.ob_m5_weight,
            "fvg": self.fvg_weight,
            "mtf_confluence": self.mtf_confluence_weight,
            "session": self.session_weight,
            "news": self.news_weight,
            "currency_strength": self.currency_strength_weight,
            "liquidity_sweep": self.liquidity_sweep_weight,
            "volume": self.volume_weight,
            "inducement": self.inducement_weight,
            "wyckoff": self.wyckoff_weight,
        }

    def clamped_to_envelope(
        self,
        baseline: "ScoringWeights",
        pct: float = ADAPTIVE_WEIGHT_ENVELOPE_PCT,
    ) -> "ScoringWeights":
        """Return a copy with every factor clamped to [baseline*(1-pct), baseline*(1+pct)]."""
        base_d = baseline.as_dict()
        self_d = self.as_dict()
        clamped: dict[str, int] = {}
        for key in FACTOR_KEYS:
            b = base_d[key]
            lo = round(b * (1.0 - pct))
            hi = round(b * (1.0 + pct))
            clamped[key] = max(lo, min(hi, self_d[key]))
        return ScoringWeights(
            structure_weight=clamped["structure"],
            ob_h1_weight=clamped["ob_h1"],
            ob_m5_weight=clamped["ob_m5"],
            fvg_weight=clamped["fvg"],
            mtf_confluence_weight=clamped["mtf_confluence"],
            session_weight=clamped["session"],
            news_weight=clamped["news"],
            currency_strength_weight=clamped["currency_strength"],
            liquidity_sweep_weight=clamped["liquidity_sweep"],
            volume_weight=clamped["volume"],
            inducement_weight=clamped["inducement"],
            wyckoff_weight=clamped["wyckoff"],
        )


class ScoreOptimizer:
    """
    Adjusts the 12-factor confluence scoring weights based on actual trade
    performance. Changes are gradual — max 3 points per optimisation cycle
    — so the system evolves, never lurches.

    An out-of-sample validation gate ensures candidate weights are adopted
    only when they do not degrade win/loss discrimination on held-out
    (later-in-time) trades, preventing in-sample overfit.

    Fitted weights are normalised to CANONICAL_TOTAL (123) for fit
    stability, then clamped to a ±25% safety envelope around baseline
    magnitudes before persistence. The envelope clamp is the final
    authority — the post-clamp total may differ from 123.
    """

    DEFAULT_PATH = "data/scoring_weights.json"
    MAX_SHIFT = 3
    MIN_WEIGHT = 3
    CANONICAL_TOTAL = 123
    VALIDATION_RATIO = 0.30
    MIN_VALIDATION = 15

    def __init__(self) -> None:
        self.current_weights = ScoringWeights()
        self.load_weights()

    def optimize(self, trades: list[dict], min_trades: int = 50) -> ScoringWeights:
        if len(trades) < min_trades:
            logger.info(f"Only {len(trades)} trades — need {min_trades} before optimising")
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
        incumbent_metric = self._compute_validation_metric(self.current_weights, validation)

        if candidate_metric >= incumbent_metric:
            baseline = ScoringWeights()
            clamped = candidate.clamped_to_envelope(baseline)
            self.current_weights = clamped
            self.save_weights()
            logger.info(
                "Weights ADOPTED — OOS separation: "
                f"candidate={candidate_metric:.4f} >= "
                f"incumbent={incumbent_metric:.4f} | "
                f"train={len(train)} validation={len(validation)} | "
                f"total={clamped.total} (pre-clamp={candidate.total})"
            )
            return clamped

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
        lifts = {k: v["lift"] for k, v in effectiveness.items() if v["sample_present"] >= 10}

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

        target = self.CANONICAL_TOTAL
        scaled = {k: max(self.MIN_WEIGHT, round(v / raw_total * target)) for k, v in raw_new.items()}
        remainder = target - sum(scaled.values())
        best_key = max(scaled, key=lambda k: scaled[k])
        scaled[best_key] += remainder

        return ScoringWeights(
            structure_weight=scaled["structure"],
            ob_h1_weight=scaled["ob_h1"],
            ob_m5_weight=scaled["ob_m5"],
            fvg_weight=scaled["fvg"],
            mtf_confluence_weight=scaled["mtf_confluence"],
            session_weight=scaled["session"],
            news_weight=scaled["news"],
            currency_strength_weight=scaled["currency_strength"],
            liquidity_sweep_weight=scaled["liquidity_sweep"],
            volume_weight=scaled["volume"],
            inducement_weight=scaled["inducement"],
            wyckoff_weight=scaled["wyckoff"],
        )

    def _fit_and_adopt(self, trades: list[dict]) -> ScoringWeights:
        """Fit on all trades and adopt without OOS validation (small-sample fallback)."""
        candidate = self._fit_weights(trades)
        if candidate is None:
            return self.current_weights
        baseline = ScoringWeights()
        clamped = candidate.clamped_to_envelope(baseline)
        self.current_weights = clamped
        self.save_weights()
        logger.info(f"Weights optimised (no OOS gate) — total={clamped.total} (pre-clamp={candidate.total})")
        return clamped

    @staticmethod
    def _compute_validation_metric(weights: ScoringWeights, trades: list[dict]) -> float:
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

        return (sum(win_scores) / len(win_scores)) - (sum(loss_scores) / len(loss_scores))

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
        try:
            from persistence.atomic_write import atomic_write_text

            atomic_write_text(p, json.dumps(asdict(weights), indent=2))
        except Exception:
            # Defensive fallback only if the atomic helper is unavailable.
            p.write_text(json.dumps(asdict(weights), indent=2))
        logger.info(f"Weights saved to {filepath}")

    def load_weights(self, filepath: Optional[str] = None) -> ScoringWeights:
        filepath = filepath or self.DEFAULT_PATH
        p = Path(filepath)
        if not p.exists():
            logger.info("No saved weights found — using defaults")
            return ScoringWeights()
        try:
            data = json.loads(p.read_text())
        except Exception as exc:
            logger.warning("Could not parse weights file {}: {}", filepath, exc)
            return ScoringWeights()
        migrated = _migrate_old_weights(data)
        valid = {k: v for k, v in migrated.items() if k in ScoringWeights.__dataclass_fields__}
        weights = ScoringWeights(**valid)
        self.current_weights = weights
        logger.info(f"Weights loaded from {filepath} — total={weights.total}")
        return weights

    @staticmethod
    def _win_rate(trades: list[dict]) -> float:
        if not trades:
            return 0.0
        wins = sum(1 for t in trades if t.get("pnl", 0) > 0)
        return wins / len(trades)


def _migrate_old_weights(data: dict) -> dict:
    """Migrate a saved-weights dict from the old 9-factor schema to the new 12-factor schema.

    Handles:
    - order_block_weight → split evenly into ob_h1_weight + ob_m5_weight
    - m1_trigger_weight → dropped (deferred to EntryEngine phase)
    - Missing new fields → filled from ScoringWeights defaults
    """
    out = dict(data)

    if "order_block_weight" in out:
        ob_val = out.pop("order_block_weight")
        half = ob_val // 2
        out.setdefault("ob_h1_weight", half)
        out.setdefault("ob_m5_weight", ob_val - half)

    out.pop("m1_trigger_weight", None)

    defaults = ScoringWeights()
    for field_name in ScoringWeights.__dataclass_fields__:
        out.setdefault(field_name, getattr(defaults, field_name))

    return out


def load_saved_weights(filepath: str = "data/scoring_weights.json") -> ScoringWeights:
    """Load OOS-validated weights from disk, migrating old schemas if needed.
    Falls back to canonical defaults if absent or corrupt.
    Shared by the backtest orchestrator and the live scanner."""
    p = Path(filepath)
    if not p.exists():
        return ScoringWeights()
    try:
        data = json.loads(p.read_text())
    except Exception as exc:
        logger.warning("Could not parse weights file {}: {} — using defaults", filepath, exc)
        return ScoringWeights()
    migrated = _migrate_old_weights(data)
    valid = {k: v for k, v in migrated.items() if k in ScoringWeights.__dataclass_fields__}
    return ScoringWeights(**valid)
