"""
Trade Planner — the coordinator that sits between the analysis advisors and
execution.  It reads a `TradePlanContext` (all advisor outputs) and produces a
`TradePlan`: a complete, self-explaining decision covering entry timing, stop
strategy, target strategy, sizing, and management.

Design rules:
  * Every threshold lives in `PlannerConfig` — no magic numbers in the logic.
  * The planner reads advisor outputs; it never writes back to them.
  * Every plan carries a human-readable `reasoning` string.
  * The config is serialisable (to/from JSON) so calibration persists across
    restarts.

Leaf module — depends only on the standard library + loguru + planning.models.
"""

from __future__ import annotations

import json
import warnings
from dataclasses import asdict, dataclass
from pathlib import Path

from loguru import logger

from planning.models import TradePlan, TradePlanContext


def _smoothstep(x: float, edge0: float, edge1: float) -> float:
    """Cubic Hermite smoothstep — 0 below ``edge0``, 1 above ``edge1`` with a
    continuous transition. Kept inline so the planner stays a leaf module."""
    if edge1 == edge0:
        return 0.0 if x < edge0 else 1.0
    t = max(0.0, min(1.0, (x - edge0) / (edge1 - edge0)))
    return t * t * (3.0 - 2.0 * t)


def _smooth_derisk(value: float, threshold: float, reduction: float, band: float) -> float:
    """Continuous de-risk factor centred on ``threshold``.

    Returns ~1.0 well below the threshold and ~``reduction`` well above it, with
    a smooth ramp through the threshold (half-applied at the threshold itself).
    Replaces the old hard step where ``value`` of 0.049 vs 0.051 jumped from
    1.0 to ``reduction``."""
    band = max(1e-9, band)
    t = _smoothstep(value, threshold - band, threshold + band)
    return 1.0 - (1.0 - reduction) * t


@dataclass
class PlannerConfig:
    """All tunable parameters for the planner.

    Starting values are reasonable defaults — they are *not* claimed to be
    optimal.  The `Calibrator` evolves them from real trade outcomes over time.
    """

    # ── Master switches ──────────────────────────────────────────────────
    enabled: bool = True
    journal_path: str = "data/plan_journal.jsonl"

    # ── Advisor agreement weights ────────────────────────────────────────
    scanner_weight: float = 1.0
    de_weight: float = 1.0
    rl_weight: float = 1.0
    adaptive_weight: float = 0.5

    # ── Minimum agreement / confidence to enter ──────────────────────────
    min_confidence_to_enter: float = 0.40
    min_advisor_agreement: float = 0.50

    # ── Gate softening (Phase 9: kill-switch → bounded dimmer) ────────────
    # When the orchestrator is the live sizer the conviction floor no longer
    # needs to *kill* a low-conviction setup — it can hand it through as ENTER
    # carrying a bounded quality multiplier the orchestrator folds into size, so
    # a near-miss trades SMALL instead of being dropped. The governor veto below
    # stays hard regardless. main_loop re-affirms ``soften_gates`` from the
    # orchestrator config; default True reflects the live orchestrator-on mode.
    soften_gates: bool = True
    gate_quality_floor: float = 0.15

    # ── Dispersion-aware advisor agreement (#8) ───────────────────────────
    # The conviction gate historically read only the advisor MEAN. A mean of
    # 0.50 is ambiguous: it can be four advisors genuinely at 0.50 (consensus)
    # or two at 1.0 and two at 0.0 (violent disagreement). When enabled, the
    # gate metric is pulled toward the WEAKEST advisor by the dispersion between
    # the mean and the minimum — so a split panel reads lower (and, with
    # soften_gates, sizes down) instead of the mean hiding the disagreement.
    # main_loop re-affirms this from the orchestrator config; default True
    # reflects the live orchestrator-on mode.
    dispersion_aware_agreement: bool = True
    advisor_dispersion_penalty: float = 0.5

    # ── Entry mode rules ─────────────────────────────────────────────────
    limit_order_zone_distance_atr: float = 0.5   # LIMIT if price > this×ATR from zone
    wait_for_session_if_wr_below: float = 0.40   # WAIT if session WR below this …
    wait_session_change_within_minutes: int = 45  # … and a new session is near
    spread_wait_ratio: float = 2.5               # WAIT if spread > this×ATR-implied normal

    # ── SL strategy selection ────────────────────────────────────────────
    prefer_structure_sl_within_atr: float = 2.0  # use structure SL if within this×ATR
    default_sl_atr_multiplier: float = 1.5

    # ── TP strategy selection ────────────────────────────────────────────
    trend_strength_for_trail_only: float = 0.75  # tf_alignment ≥ this → trail-only
    low_r_threshold_for_fixed_tp: float = 1.5    # RL expected_r < this → fixed TP
    default_tp1_rr: float = 1.5
    default_tp2_rr: float = 3.0
    default_runner_pct: float = 0.3

    # ── Sizing rules ─────────────────────────────────────────────────────
    correlation_size_reduction: float = 0.5      # ×this when correlated
    correlation_threshold: float = 0.4           # correlated_exposure above this triggers it
    drawdown_size_reduction_threshold: float = 5.0
    drawdown_size_reduction: float = 0.6
    high_conviction_size_boost: float = 1.25
    high_conviction_agreement: float = 0.75      # agreement above this earns the boost
    max_risk_pct: float = 2.0
    min_risk_pct: float = 0.1
    # #22 — transition bands for the smooth (continuous) sizing curves. A value
    # just below a threshold no longer behaves identically to one far below it;
    # the de-risk / boost ramps in across ±band centred on the threshold. Set a
    # band to ~0 to recover near-step behaviour.
    correlation_size_band: float = 0.15          # exposure units (0–1 scale)
    drawdown_size_band: float = 2.0              # drawdown percent
    conviction_boost_band: float = 0.15          # agreement/confidence units

    # ── BE / trailing ────────────────────────────────────────────────────
    default_be_trigger_r: float = 0.5
    news_be_trigger_r: float = 0.3
    friday_be_trigger_r: float = 0.3
    default_trail_activation_r: float = 1.0
    scale_in_min_agreement: float = 0.7          # only allow scale-in above this

    # ── Calibration ──────────────────────────────────────────────────────
    calibration_enabled: bool = True
    calibration_min_trades: int = 50
    calibration_interval_trades: int = 25
    calibration_lookback_trades: int = 200
    calibration_max_adjustment_pct: float = 0.20  # clamp ±20% per cycle
    # #34 — expectancy-aware calibration. The SL-strategy and min-confidence
    # analyses compare groups by WIN-RATE only, so a 3R win and a 0.1R win count
    # the same and a lower-win-rate / higher-payoff group is wrongly demoted.
    # aware) instead. LIVE: groups are compared by expectancy (mean R).
    calibration_expectancy_aware: bool = True
    calibration_expectancy_margin: float = 0.10  # min R gap between groups to act

    # ── Serialisation ────────────────────────────────────────────────────

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "PlannerConfig":
        valid = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in data.items() if k in valid})

    def save(self, path: str | None = None) -> None:
        target = Path(path or "data/planner_config.json")
        try:
            from persistence.atomic_write import atomic_write_text

            atomic_write_text(target, json.dumps(self.to_dict(), indent=2))
        except Exception:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps(self.to_dict(), indent=2), encoding="utf-8")

    @classmethod
    def load(cls, path: str | None = None) -> "PlannerConfig":
        target = Path(path or "data/planner_config.json")
        if not target.exists():
            return cls()
        try:
            return cls.from_dict(json.loads(target.read_text(encoding="utf-8")))
        except Exception as exc:
            logger.warning("[PlannerConfig] load failed, using defaults: {}", exc)
            return cls()


class TradePlanner:
    """Deprecated — retired directional decider with no directional authority.

    ``plan_trade`` is a non-actionable no-op (see its docstring). The remaining
    ``PlannerConfig``/``Calibrator``/``OutcomeLogger`` ecosystem and the pure
    execution-geometry helpers stay usable, but this class no longer decides
    direction or computes advisor agreement — that is the Brain's authority.
    """

    def __init__(self, config: PlannerConfig | None = None, governor=None) -> None:
        warnings.warn(
            "TradePlanner is deprecated and retired from the live decision path. "
            "It no longer has directional authority: plan_trade() does not compute "
            "advisor agreement or emit a BUY/SELL verdict — opportunity direction "
            "is decided by the Brain. Only PlannerConfig/Calibrator/OutcomeLogger "
            "remain in use.",
            DeprecationWarning,
            stacklevel=2,
        )
        logger.warning(
            "[Planner] TradePlanner instantiated — deprecated, no directional "
            "authority (direction is the Brain's; plan_trade is a non-actionable "
            "no-op)."
        )
        self.config = config or PlannerConfig()
        # Portfolio Governor (duck-typed: any object with a .check() returning
        # an object carrying .allowed/.reason/.blocked_by).  Optional — when
        # absent the planner applies no portfolio-level limits.
        self._governor = governor

    def update_config(self, config: PlannerConfig) -> None:
        self.config = config

    def set_governor(self, governor) -> None:
        self._governor = governor

    # ── Main entry point ─────────────────────────────────────────────────

    def plan_trade(self, ctx: TradePlanContext) -> TradePlan:
        """Deprecated — the planner has no directional authority.

        Constitution (DIRECTIONAL_AUTHORITY, V-005): this method used to compute
        a weighted directional "advisor agreement" across the scanner, decision
        engine, RL and adaptive pair win-rate, blend it into a confidence, and
        emit a ``TradePlan(direction="BUY"/"SELL")`` verdict. That recreated the
        forbidden directional-consensus architecture downstream of cognition.

        Opportunity discovery and trade direction are the Brain's authority, so
        the planner no longer reasons over direction or advisor agreement and
        returns a non-actionable, directionless plan. The execution-geometry
        helpers (``_sl_plan``/``_tp_plan``/``_entry_mode``/``_size_plan``/
        ``_be_trigger``) remain as pure utilities that shape an already-decided
        opportunity, but they are no longer driven from here.
        """
        logger.warning(
            "[Planner] plan_trade is deprecated and has no directional authority; "
            "returning a non-actionable plan for {} — direction is decided by the "
            "Brain, not recomputed here.",
            ctx.symbol or "?",
        )
        return TradePlan(
            action="SKIP",
            direction="",
            reasoning=(
                f"[{ctx.situation_label}] SKIP — TradePlanner is deprecated and "
                "has no directional authority; opportunity direction and entry "
                "are decided by the Brain."
            ),
        )

    # ── WAIT decision ────────────────────────────────────────────────────

    def _wait_decision(self, ctx: TradePlanContext) -> tuple[str | None, int | None]:
        cfg = self.config
        # Poor session win-rate but a better session is imminent.
        if (
            ctx.session_win_rate < cfg.wait_for_session_if_wr_below
            and ctx.minutes_to_session_change <= cfg.wait_session_change_within_minutes
        ):
            return (
                f"session {ctx.session} WR {ctx.session_win_rate:.0%} weak; "
                f"session change in {ctx.minutes_to_session_change}min",
                int(ctx.minutes_to_session_change) + 1,
            )
        # Temporarily wide spread (relative to ATR-implied normal).
        if ctx.atr_pips > 0 and ctx.spread_pips > 0:
            implied = ctx.atr_pips * 0.10  # rough normal spread proxy
            if implied > 0 and ctx.spread_pips > implied * cfg.spread_wait_ratio:
                return (f"spread {ctx.spread_pips:.1f}pip elevated vs normal", 5)
        return (None, None)

    # ── Entry mode ───────────────────────────────────────────────────────

    def _entry_mode(self, ctx: TradePlanContext) -> tuple[str, float | None]:
        cfg = self.config
        # Honour the entry engine's MARKET decision (price already past zone, etc.).
        if ctx.brain_entry_mode == "MARKET":
            return ("MARKET", None)
        # If price has moved well away from the zone, a LIMIT back to the zone.
        if ctx.atr_pips > 0 and ctx.zone_entry_price > 0 and ctx.current_price > 0:
            dist_pips = abs(ctx.current_price - ctx.zone_entry_price) / max(ctx.pip_size, 1e-9)
            if dist_pips > cfg.limit_order_zone_distance_atr * ctx.atr_pips:
                return ("LIMIT", ctx.zone_entry_price)
        return ("MARKET", None)

    # ── SL plan ──────────────────────────────────────────────────────────

    def _sl_plan(self, ctx: TradePlanContext) -> tuple[str, float, float]:
        cfg = self.config
        proposed_pips = ctx.proposed_sl_pips
        # Prefer the structural stop when it is available and not excessively wide.
        if (
            ctx.structure_sl_available
            and ctx.proposed_sl_price > 0
            and (
                ctx.atr_pips <= 0
                or proposed_pips <= cfg.prefer_structure_sl_within_atr * ctx.atr_pips
            )
        ):
            return ("structure", ctx.proposed_sl_price, proposed_pips)
        # Otherwise an ATR-based stop.
        if ctx.atr_pips > 0:
            sl_pips = cfg.default_sl_atr_multiplier * ctx.atr_pips
            # Regime-learned additive buffer (bounded), applied to the ATR stop
            # ONLY — never to a real structure level. Applied here (pre-sizing)
            # so position size derives from the final stop and risk stays correct.
            sl_pips += max(-2.0, min(5.0, ctx.regime_sl_buffer_pips))
            sl_pips = max(1.0, sl_pips)
            offset = sl_pips * ctx.pip_size
            sl_price = (
                ctx.current_price - offset if ctx.is_long else ctx.current_price + offset
            )
            return ("atr", round(sl_price, 6), sl_pips)
        # Fall back to whatever the entry engine proposed.
        return ("fixed_rr", ctx.proposed_sl_price, proposed_pips)

    # ── TP plan ──────────────────────────────────────────────────────────

    def _tp_plan(
        self, ctx: TradePlanContext, sl_pips: float,
    ) -> tuple[str, float | None, float, float | None, float | None, float]:
        cfg = self.config
        entry = ctx.zone_entry_price or ctx.current_price
        pip = ctx.pip_size
        sign = 1.0 if ctx.is_long else -1.0

        # Regime-learned TP stretch + runner fraction (bounded). Both are neutral
        # (1.0 multiplier / planner default runner) until the RegimeLearner is
        # confident for this regime, so behaviour is unchanged until evidence
        # accrues. tp_mult scales the R-multiples only — it never touches the SL.
        tp_mult = max(0.8, min(1.5, ctx.regime_tp_mult))
        tp1_rr = cfg.default_tp1_rr * tp_mult
        tp2_rr = cfg.default_tp2_rr * tp_mult
        runner = (
            max(0.1, min(0.7, ctx.regime_runner_pct))
            if ctx.regime_runner_pct is not None
            else cfg.default_runner_pct
        )

        def price_at_rr(rr: float) -> float:
            return round(entry + sign * sl_pips * rr * pip, 6)

        # Strong trend → let it run with a trailing stop, no fixed TP2.
        if ctx.de_tf_alignment >= cfg.trend_strength_for_trail_only:
            tp1 = price_at_rr(tp1_rr)
            return ("trail_only", tp1, tp1_rr, None, None, 1.0 - 0.5)
        # Low expected R from RL → bank a fixed target, no runner.
        if 0 < ctx.rl_expected_r < cfg.low_r_threshold_for_fixed_tp:
            tp1 = price_at_rr(tp1_rr)
            tp2 = price_at_rr(tp2_rr)
            return ("fixed_rr", tp1, tp1_rr, tp2, tp2_rr, 0.0)
        # Default: partial at TP1, leave a runner trailing toward TP2.
        tp1 = price_at_rr(tp1_rr)
        tp2 = price_at_rr(tp2_rr)
        return (
            "partial_trail",
            tp1,
            tp1_rr,
            tp2,
            tp2_rr,
            runner,
        )

    # ── Sizing ───────────────────────────────────────────────────────────

    def _size_plan(self, ctx: TradePlanContext) -> tuple[float, str]:
        cfg = self.config
        risk = ctx.base_risk_pct
        parts: list[str] = [f"base {risk:.2f}%"]

        # #22 — correlated-exposure de-risk as a smooth ramp (no cliff at the
        # threshold). Below the band → no reduction; well above → full reduction.
        corr_factor = _smooth_derisk(
            ctx.correlated_exposure,
            cfg.correlation_threshold,
            cfg.correlation_size_reduction,
            cfg.correlation_size_band,
        )
        if corr_factor < 1.0 - 1e-6:
            risk *= corr_factor
            parts.append(f"×{corr_factor:.2f} correlated")

        # Drawdown de-risk — smooth ramp through the threshold.
        dd_factor = _smooth_derisk(
            ctx.current_drawdown_pct,
            cfg.drawdown_size_reduction_threshold,
            cfg.drawdown_size_reduction,
            cfg.drawdown_size_band,
        )
        if dd_factor < 1.0 - 1e-6:
            risk *= dd_factor
            parts.append(f"×{dd_factor:.2f} drawdown")

        # Adaptive pair multiplier folds in too.
        if ctx.pair_multiplier > 0 and abs(ctx.pair_multiplier - 1.0) > 1e-6:
            risk *= ctx.pair_multiplier
            parts.append(f"×{ctx.pair_multiplier:.2f} pair")

        risk = max(cfg.min_risk_pct, min(cfg.max_risk_pct, risk))
        return (round(risk, 4), "; ".join(parts))

    # ── BE trigger ───────────────────────────────────────────────────────

    def _be_trigger(self, ctx: TradePlanContext) -> float:
        cfg = self.config
        if ctx.is_news_window or ctx.minutes_to_news < 60:
            return cfg.news_be_trigger_r
        if ctx.day_of_week == 4:  # Friday — protect before the weekend
            return cfg.friday_be_trigger_r
        return cfg.default_be_trigger_r
