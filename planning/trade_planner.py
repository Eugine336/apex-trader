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
from dataclasses import asdict, dataclass, field
from pathlib import Path

from loguru import logger

from planning.models import TradePlan, TradePlanContext


def _gate_quality_multiplier(
    measures: list[tuple[float, float]], floor: float = 0.15
) -> float:
    """Bounded quality multiplier for the softened conviction gate.

    Mirrors ``brain.orchestrator.gate_quality_multiplier`` but kept inline so the
    planner stays a leaf module (stdlib + loguru + planning.models). For each
    ``(value, threshold)`` a shortfall contributes ``value / threshold`` (<1.0);
    the product is bounded to ``[floor, 1.0]`` so a near-miss flows through small
    and is never zero (a graded "barely" is still a tiny trade) nor above 1.0.
    """
    mult = 1.0
    for value, threshold in measures:
        try:
            threshold = float(threshold)
            value = float(value)
        except (TypeError, ValueError):
            continue
        if threshold <= 0:
            continue
        ratio = value / threshold
        if ratio < 1.0:
            mult *= max(0.0, ratio)
    return max(floor, min(1.0, mult))


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
    # stays hard regardless. main_loop sets ``soften_gates`` from the
    # orchestrator config; default False keeps the legacy hard SKIP.
    soften_gates: bool = False
    gate_quality_floor: float = 0.15

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
    """Reads a `TradePlanContext` and produces a complete `TradePlan`."""

    def __init__(self, config: PlannerConfig | None = None, governor=None) -> None:
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
        cfg = self.config
        is_long = ctx.is_long

        agreement = self._advisor_agreement(ctx)
        confidence = self._confidence(ctx, agreement)

        plan = TradePlan(direction="BUY" if is_long else "SELL")
        plan.advisor_agreement = agreement
        plan.confidence = confidence

        # ── 1. ENTER / WAIT / SKIP ───────────────────────────────────────
        if confidence < cfg.min_confidence_to_enter and agreement < cfg.min_advisor_agreement:
            if cfg.soften_gates:
                # Phase 9: soften the conviction floor into a bounded dimmer.
                # Instead of killing the setup, flow it through as ENTER carrying
                # a quality multiplier (how far below the floors it was) that the
                # orchestrator folds into graded size. The governor veto below
                # still applies — only this QUALITY gate is softened.
                plan.gate_quality_multiplier = _gate_quality_multiplier(
                    [
                        (confidence, cfg.min_confidence_to_enter),
                        (agreement, cfg.min_advisor_agreement),
                    ],
                    cfg.gate_quality_floor,
                )
                logger.info(
                    "[gate-soften] planner {} low conviction "
                    "(confidence {:.2f}<{:.2f}, agreement {:.2f}<{:.2f}) — "
                    "flowing as ENTER ×{:.2f}",
                    ctx.symbol, confidence, cfg.min_confidence_to_enter,
                    agreement, cfg.min_advisor_agreement,
                    plan.gate_quality_multiplier,
                )
            else:
                plan.action = "SKIP"
                plan.reasoning = (
                    f"[{ctx.situation_label}] SKIP — low conviction "
                    f"(confidence {confidence:.2f} < {cfg.min_confidence_to_enter:.2f}, "
                    f"agreement {agreement:.2f} < {cfg.min_advisor_agreement:.2f})"
                )
                return plan

        wait_reason, wait_minutes = self._wait_decision(ctx)
        if wait_reason is not None:
            plan.action = "WAIT"
            plan.wait_reason = wait_reason
            plan.wait_until_minutes = wait_minutes
            plan.reasoning = (
                f"[{ctx.situation_label}] WAIT — {wait_reason} "
                f"(retry in ~{wait_minutes}min)"
            )
            return plan

        # ── 1b. Portfolio Governor — portfolio-level risk veto ───────────
        if self._governor is not None:
            try:
                verdict = self._governor.check(
                    ctx.symbol,
                    plan.direction,
                    ctx.open_position_book,
                    ctx.account_balance,
                )
            except Exception as exc:  # governor.check is normally self-guarding
                # Mirror the governor's own policy: fail-closed (SKIP) unless it
                # is explicitly configured fail-open.
                gov_fail_closed = getattr(
                    getattr(self._governor, "config", None), "fail_closed", True
                )
                if gov_fail_closed:
                    logger.error("[Planner] governor check error — SKIP (fail-closed): {}", exc)
                    plan.action = "SKIP"
                    plan.governor_blocked_by = "governor_error"
                    plan.reasoning = (
                        f"[{ctx.situation_label}] SKIP — governor error "
                        f"(fail-closed): {exc}"
                    )
                    return plan
                logger.warning("[Planner] governor check error — allowing (fail-open): {}", exc)
                verdict = None
            if verdict is not None and not getattr(verdict, "allowed", True):
                plan.action = "SKIP"
                plan.governor_blocked_by = getattr(verdict, "blocked_by", None)
                plan.reasoning = (
                    f"[{ctx.situation_label}] SKIP — governor "
                    f"({getattr(verdict, 'blocked_by', 'portfolio')}): "
                    f"{getattr(verdict, 'reason', 'portfolio limit')}"
                )
                return plan

        plan.action = "ENTER"

        # ── 2. Entry mode ────────────────────────────────────────────────
        plan.entry_mode, plan.entry_price = self._entry_mode(ctx)

        # ── 3. Stop loss strategy ────────────────────────────────────────
        plan.sl_strategy, plan.sl_price, plan.sl_pips = self._sl_plan(ctx)

        # ── 4. Take profit strategy ──────────────────────────────────────
        (
            plan.tp_strategy,
            plan.tp1_price,
            plan.tp1_rr,
            plan.tp2_price,
            plan.tp2_rr,
            plan.runner_pct,
        ) = self._tp_plan(ctx, plan.sl_pips)

        # ── 5. Sizing ────────────────────────────────────────────────────
        plan.risk_pct, plan.size_reasoning = self._size_plan(ctx, agreement, confidence)

        # ── 6. Management ────────────────────────────────────────────────
        plan.be_trigger_r = self._be_trigger(ctx)
        plan.trail_activation_r = cfg.default_trail_activation_r
        plan.trail_strategy = "swing" if plan.tp_strategy != "fixed_rr" else "none"
        plan.scale_in_allowed = agreement >= cfg.scale_in_min_agreement and ctx.daily_pnl_r >= 0

        # ── 7. Reasoning ─────────────────────────────────────────────────
        plan.reasoning = (
            f"[{ctx.situation_label}] ENTER {plan.direction} via {plan.entry_mode} | "
            f"SL={plan.sl_strategy}({plan.sl_pips:.1f}pip) "
            f"TP={plan.tp_strategy} runner={plan.runner_pct:.0%} | "
            f"risk={plan.risk_pct:.2f}% ({plan.size_reasoning}) | "
            f"BE@{plan.be_trigger_r:.1f}R trail={plan.trail_strategy} | "
            f"agreement={agreement:.2f} confidence={confidence:.2f}"
        )
        return plan

    # ── Advisor agreement ────────────────────────────────────────────────

    def _advisor_agreement(self, ctx: TradePlanContext) -> float:
        """Weighted directional agreement across advisors → 0..1.

        Each advisor contributes a signed alignment in [-1, +1]; the weighted
        mean is mapped to [0, 1].  Advisors that abstain contribute zero weight.
        """
        cfg = self.config
        num = 0.0
        denom = 0.0

        # Scanner produced the setup — full support, scaled by score.
        scanner_align = max(0.0, min(1.0, ctx.scanner_score / 100.0))
        num += cfg.scanner_weight * scanner_align
        denom += cfg.scanner_weight

        # Decision engine — signed directional read.
        if abs(ctx.de_tf_alignment) > 1e-6 or ctx.de_confidence > 0:
            num += cfg.de_weight * max(-1.0, min(1.0, ctx.de_tf_alignment))
            denom += cfg.de_weight

        # RL — only votes when it has an opinion (not HOLD).
        if ctx.rl_action in (1, 2):
            rl_supports = (
                (ctx.is_long and ctx.rl_action == 1)
                or (not ctx.is_long and ctx.rl_action == 2)
            )
            rl_align = ctx.rl_confidence if rl_supports else -ctx.rl_confidence
            num += cfg.rl_weight * max(-1.0, min(1.0, rl_align))
            denom += cfg.rl_weight

        # Adaptive — pair win-rate as a soft directional confirmation.
        if ctx.pair_win_rate > 0:
            adaptive_align = max(-1.0, min(1.0, (ctx.pair_win_rate - 0.5) * 2.0))
            num += cfg.adaptive_weight * adaptive_align
            denom += cfg.adaptive_weight

        if denom <= 0:
            return 0.5
        raw = num / denom            # −1..+1
        return max(0.0, min(1.0, (raw + 1.0) / 2.0))

    def advisor_vector(self, ctx: TradePlanContext) -> dict:
        """Per-advisor signed alignment in [-1, +1] kept alongside the mean.

        Where ``_advisor_agreement`` collapses the four advisors (scanner,
        decision engine, RL, adaptive pair win-rate) into one number, this keeps
        each advisor's signed read intact so a consumer (e.g. the orchestrator)
        can see *disagreement shape* — a strong scanner opposed by RL is very
        different from four mediocre advisors, but the mean hides that. Abstaining
        advisors are omitted. ``agreement`` mirrors ``_advisor_agreement``.
        """
        vec: dict = {}
        vec["scanner"] = round(max(0.0, min(1.0, ctx.scanner_score / 100.0)), 4)
        if abs(ctx.de_tf_alignment) > 1e-6 or ctx.de_confidence > 0:
            vec["decision_engine"] = round(max(-1.0, min(1.0, ctx.de_tf_alignment)), 4)
        if ctx.rl_action in (1, 2):
            rl_supports = (
                (ctx.is_long and ctx.rl_action == 1)
                or (not ctx.is_long and ctx.rl_action == 2)
            )
            rl_align = ctx.rl_confidence if rl_supports else -ctx.rl_confidence
            vec["rl"] = round(max(-1.0, min(1.0, rl_align)), 4)
        if ctx.pair_win_rate > 0:
            vec["pair_win_rate"] = round(max(-1.0, min(1.0, (ctx.pair_win_rate - 0.5) * 2.0)), 4)
        return {"advisors": vec, "agreement": round(self._advisor_agreement(ctx), 4)}

    def _confidence(self, ctx: TradePlanContext, agreement: float) -> float:
        """Blend DE confidence, advisor agreement and structure quality."""
        de = max(0.0, min(1.0, ctx.de_confidence))
        zone = max(0.0, min(1.0, ctx.zone_quality))
        c = de * 0.4 + agreement * 0.4 + zone * 0.2
        return max(0.0, min(1.0, c))

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

    def _size_plan(
        self, ctx: TradePlanContext, agreement: float, confidence: float,
    ) -> tuple[float, str]:
        cfg = self.config
        risk = ctx.base_risk_pct
        parts: list[str] = [f"base {risk:.2f}%"]

        if ctx.correlated_exposure >= cfg.correlation_threshold:
            risk *= cfg.correlation_size_reduction
            parts.append(f"×{cfg.correlation_size_reduction} correlated")

        if ctx.current_drawdown_pct >= cfg.drawdown_size_reduction_threshold:
            risk *= cfg.drawdown_size_reduction
            parts.append(f"×{cfg.drawdown_size_reduction} drawdown")

        if agreement >= cfg.high_conviction_agreement and confidence >= cfg.min_confidence_to_enter:
            risk *= cfg.high_conviction_size_boost
            parts.append(f"×{cfg.high_conviction_size_boost} conviction")

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
