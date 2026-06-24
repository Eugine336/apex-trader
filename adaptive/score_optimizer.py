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

# Per-user writeable state — learned scoring weights resolve under the owning
# user's data tree (APEX_DATA_DIR) instead of a bare cwd-relative ``data/`` path.
from runtime_paths import data_dir as _data_dir


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

# Per-class weight profiles (collapse fix: a single global weight set averages
# forex + synthetic + crypto into a mediocre middle that is optimal for nothing).
# When ``per_class`` mode is on, ScoreOptimizer keeps a separate ScoringWeights
# profile per asset class plus this shared fallback used for cold-start /
# unknown symbols and as the Bayesian-shrinkage prior for thin classes.
DEFAULT_CLASS = "default"

_OLD_TO_NEW_KEY_MAP = {
    "order_block_weight": ("ob_h1_weight", "ob_m5_weight"),
    "m1_trigger_weight": None,
}


def classify_asset_class(symbol: str) -> str:
    """Return the asset-class key for a symbol.

    Reuses the central ``InstrumentCategory`` registry (forex / commodity /
    index / synthetic / crypto). Unknown or empty symbols fall back to
    ``DEFAULT_CLASS`` so per-class lookups always resolve to a real profile.
    """
    if not symbol:
        return DEFAULT_CLASS
    try:
        from config import get_instrument

        return get_instrument(symbol).category.value
    except Exception:
        return DEFAULT_CLASS


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
        remainder = baseline.total - sum(clamped.values())
        if remainder != 0:
            best_key = max(clamped, key=lambda k: clamped[k])
            clamped[best_key] += remainder
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

    # Default filename only. The directory is resolved at call time through
    # runtime_paths.data_dir() (per-user writeable tree) — see load_weights /
    # save_weights — so DEFAULT_PATH itself is never opened directly.
    DEFAULT_PATH = "data/scoring_weights.json"
    MAX_SHIFT = 3
    MIN_WEIGHT = 3
    CANONICAL_TOTAL = 123
    VALIDATION_RATIO = 0.30
    MIN_VALIDATION = 15

    # Per-class defaults (overridable via ScoringConfig).
    PER_CLASS_DEFAULT = False
    MIN_TRADES_PER_CLASS = 30
    CLASS_SHRINKAGE_STRENGTH = 0.3

    def __init__(self, config=None) -> None:
        # Per-class mode is driven by ScoringConfig. When off, behaviour is
        # byte-for-byte identical to the legacy single-profile optimizer.
        self.per_class = bool(getattr(config, "per_class_optimizer", self.PER_CLASS_DEFAULT))
        self.min_trades_per_class = int(
            getattr(config, "min_trades_per_class", self.MIN_TRADES_PER_CLASS)
        )
        self.class_shrinkage_strength = float(
            getattr(config, "class_shrinkage_strength", self.CLASS_SHRINKAGE_STRENGTH)
        )
        # The shared / global profile (also the cold-start + shrinkage prior).
        self.current_weights = ScoringWeights()
        # Per-asset-class profiles. Empty / missing entries resolve to the
        # global ``current_weights`` so lookups never fail.
        self.class_weights: dict[str, ScoringWeights] = {}
        self.load_weights()

    def optimize(self, trades: list[dict], min_trades: int = 50) -> ScoringWeights:
        if self.per_class:
            return self._optimize_per_class(trades, min_trades)

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

    def _fit_weights(
        self, trades: list[dict], base: Optional["ScoringWeights"] = None
    ) -> Optional[ScoringWeights]:
        """Fit candidate weights from the given trades. Returns None if insufficient lift data.

        ``base`` is the incumbent the per-factor shifts are applied to; defaults
        to ``self.current_weights`` (the legacy/global profile) so existing
        callers are unaffected. Per-class fits pass the class incumbent instead.
        """
        base_weights = base if base is not None else self.current_weights
        effectiveness = self.get_factor_effectiveness(trades)
        lifts = {k: v["lift"] for k, v in effectiveness.items() if v["sample_present"] >= 10}

        if not lifts:
            return None

        current = base_weights.as_dict()
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

    # ------------------------------------------------------------------
    # Per-class optimisation
    # ------------------------------------------------------------------

    def _optimize_per_class(
        self, trades: list[dict], min_trades: int
    ) -> ScoringWeights:
        """Fit a separate weight profile per asset class.

        The shared/global profile (``current_weights``) is fitted on ALL trades
        and is used as the cold-start fallback and the Bayesian-shrinkage prior.
        Each class with at least ``min_trades_per_class`` trades is fitted
        independently on only its own trades, then shrunk toward the global
        profile by sample confidence. Classes below the floor keep no separate
        profile and resolve to the global one at lookup time.

        Returns the global profile (mirrors the legacy return contract).
        """
        # 1. Global profile from the full history (incumbent = current global).
        global_weights = self._fit_profile(trades, self.current_weights, min_trades)
        self.current_weights = global_weights

        # 2. Per-class profiles from the class-split history.
        by_class = self._split_by_class(trades)
        new_class_weights: dict[str, ScoringWeights] = {}
        for cls, cls_trades in by_class.items():
            if cls == DEFAULT_CLASS:
                continue
            n = len(cls_trades)
            if n < self.min_trades_per_class:
                # Thin class — no independent profile; resolves to global.
                continue
            incumbent = self.class_weights.get(cls, global_weights)
            fitted = self._fit_profile(
                cls_trades,
                incumbent,
                min_trades=min(min_trades, self.min_trades_per_class),
            )
            blended = self._shrink_toward_global(fitted, global_weights, n)
            new_class_weights[cls] = blended

        self.class_weights = new_class_weights
        self.save_weights()
        logger.info(
            "Per-class weights optimised — global total={} | classes={}".format(
                global_weights.total,
                ", ".join(
                    f"{cls}(n={len(by_class.get(cls, []))})"
                    for cls in sorted(new_class_weights)
                )
                or "none above floor",
            )
        )
        return global_weights

    def _fit_profile(
        self, trades: list[dict], incumbent: ScoringWeights, min_trades: int
    ) -> ScoringWeights:
        """Pure OOS-gated fit for ONE profile — no disk write, no global mutation.

        Returns the adopted (envelope-clamped) weights, or the incumbent
        unchanged when there is insufficient data or the candidate fails the
        out-of-sample separation gate.
        """
        if len(trades) < min_trades:
            return incumbent

        sorted_trades = self._sort_by_time(trades)
        split_idx = int(len(sorted_trades) * (1 - self.VALIDATION_RATIO))
        train = sorted_trades[:split_idx]
        validation = sorted_trades[split_idx:]

        baseline = ScoringWeights()

        if len(validation) < self.MIN_VALIDATION:
            candidate = self._fit_weights(sorted_trades, base=incumbent)
            if candidate is None:
                return incumbent
            return candidate.clamped_to_envelope(baseline)

        candidate = self._fit_weights(train, base=incumbent)
        if candidate is None:
            return incumbent

        candidate_metric = self._compute_validation_metric(candidate, validation)
        incumbent_metric = self._compute_validation_metric(incumbent, validation)
        if candidate_metric >= incumbent_metric:
            return candidate.clamped_to_envelope(baseline)
        return incumbent

    def _shrink_toward_global(
        self, fitted: ScoringWeights, global_weights: ScoringWeights, n: int
    ) -> ScoringWeights:
        """Bayesian shrinkage: blend the class fit toward the global profile.

        ``lam = n / (n + k)`` where the pseudo-count ``k`` scales with
        ``class_shrinkage_strength`` and ``min_trades_per_class``. More class
        trades → trust the class fit more; thin classes lean on the global
        prior. The result is clamped to the same ±envelope as every other
        profile so per-class divergence stays bounded.
        """
        k = max(0.0, self.class_shrinkage_strength) * float(self.min_trades_per_class)
        denom = n + k
        lam = (n / denom) if denom > 0 else 1.0
        fit_d = fitted.as_dict()
        glob_d = global_weights.as_dict()
        blended = {
            key: max(
                self.MIN_WEIGHT,
                round(lam * fit_d[key] + (1.0 - lam) * glob_d[key]),
            )
            for key in FACTOR_KEYS
        }
        weights = ScoringWeights(
            structure_weight=blended["structure"],
            ob_h1_weight=blended["ob_h1"],
            ob_m5_weight=blended["ob_m5"],
            fvg_weight=blended["fvg"],
            mtf_confluence_weight=blended["mtf_confluence"],
            session_weight=blended["session"],
            news_weight=blended["news"],
            currency_strength_weight=blended["currency_strength"],
            liquidity_sweep_weight=blended["liquidity_sweep"],
            volume_weight=blended["volume"],
            inducement_weight=blended["inducement"],
            wyckoff_weight=blended["wyckoff"],
        )
        return weights.clamped_to_envelope(ScoringWeights())

    @staticmethod
    def _split_by_class(trades: list[dict]) -> dict[str, list[dict]]:
        """Group trades by their symbol's asset class (``t['pair']``)."""
        grouped: dict[str, list[dict]] = {}
        for t in trades:
            cls = classify_asset_class(str(t.get("pair", "")))
            grouped.setdefault(cls, []).append(t)
        return grouped

    def weights_for_class(self, asset_class: str) -> ScoringWeights:
        """Return the weight profile for an asset class, falling back to global."""
        if not self.per_class:
            return self.current_weights
        return self.class_weights.get(asset_class, self.current_weights)

    def weights_for_symbol(self, symbol: str) -> ScoringWeights:
        """Return the weight profile that applies to ``symbol``."""
        return self.weights_for_class(classify_asset_class(symbol))

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
            pnl = t.get("pnl", 0)
            if pnl > 0:
                win_scores.append(score)
            elif pnl < 0:
                loss_scores.append(score)
            # Scratch trades (pnl == 0) are excluded from the separation metric.

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
        filepath = filepath or str(_data_dir() / "scoring_weights.json")
        p = Path(filepath)
        p.parent.mkdir(parents=True, exist_ok=True)

        if self.per_class:
            default_w = weights or self.current_weights
            payload: dict = {DEFAULT_CLASS: asdict(default_w)}
            for cls, w in self.class_weights.items():
                payload[cls] = asdict(w)
        else:
            payload = asdict(weights or self.current_weights)

        text = json.dumps(payload, indent=2)
        try:
            from persistence.atomic_write import atomic_write_text

            atomic_write_text(p, text)
        except Exception:
            # Defensive fallback only if the atomic helper is unavailable.
            p.write_text(text)
        logger.info(f"Weights saved to {filepath}")

    @staticmethod
    def _weights_from_dict(data: dict) -> ScoringWeights:
        """Build ScoringWeights from a (possibly old-schema) flat dict."""
        migrated = _migrate_old_weights(data)
        valid = {k: v for k, v in migrated.items() if k in ScoringWeights.__dataclass_fields__}
        return ScoringWeights(**valid)

    @staticmethod
    def _is_nested(data: dict) -> bool:
        """True when the persisted file holds per-class profiles.

        Flat files carry weight keys (e.g. ``structure_weight``) at the top
        level. Nested files map asset-class names to weight dicts.
        """
        if not isinstance(data, dict) or not data:
            return False
        if any(k in ScoringWeights.__dataclass_fields__ for k in data):
            return False
        return any(isinstance(v, dict) for v in data.values())

    def load_weights(self, filepath: Optional[str] = None) -> ScoringWeights:
        filepath = filepath or str(_data_dir() / "scoring_weights.json")
        p = Path(filepath)
        if not p.exists():
            logger.info("No saved weights found — using defaults")
            return ScoringWeights()
        try:
            data = json.loads(p.read_text())
        except Exception as exc:
            logger.warning("Could not parse weights file {}: {}", filepath, exc)
            return ScoringWeights()

        if self._is_nested(data):
            default_data = data.get(DEFAULT_CLASS, {})
            self.current_weights = self._weights_from_dict(default_data)
            self.class_weights = {
                cls: self._weights_from_dict(d)
                for cls, d in data.items()
                if cls != DEFAULT_CLASS and isinstance(d, dict)
            }
            logger.info(
                "Per-class weights loaded from {} — global total={} | classes=[{}]",
                filepath,
                self.current_weights.total,
                ", ".join(sorted(self.class_weights)),
            )
            return self.current_weights

        weights = self._weights_from_dict(data)
        self.current_weights = weights
        self.class_weights = {}
        logger.info(f"Weights loaded from {filepath} — total={weights.total}")
        return weights

    @staticmethod
    def _win_rate(trades: list[dict]) -> float:
        if not trades:
            return 0.0
        wins = sum(1 for t in trades if t.get("pnl", 0) > 0)
        losses = sum(1 for t in trades if t.get("pnl", 0) < 0)
        # Scratch trades (pnl == 0) are excluded from the win-rate denominator.
        decided = wins + losses
        return wins / decided if decided else 0.0


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


def load_saved_weights(filepath: Optional[str] = None) -> ScoringWeights:
    """Load OOS-validated weights from disk, migrating old schemas if needed.
    Falls back to canonical defaults if absent or corrupt.
    Shared by the backtest orchestrator and the live scanner.

    When the file holds per-class profiles (nested), the shared ``default``
    profile is returned — single-profile consumers stay correct without
    needing to know about per-class mode.
    """
    filepath = filepath or str(_data_dir() / "scoring_weights.json")
    p = Path(filepath)
    if not p.exists():
        return ScoringWeights()
    try:
        data = json.loads(p.read_text())
    except Exception as exc:
        logger.warning("Could not parse weights file {}: {} — using defaults", filepath, exc)
        return ScoringWeights()
    if ScoreOptimizer._is_nested(data):
        data = data.get(DEFAULT_CLASS, {})
    migrated = _migrate_old_weights(data)
    valid = {k: v for k, v in migrated.items() if k in ScoringWeights.__dataclass_fields__}
    return ScoringWeights(**valid)
