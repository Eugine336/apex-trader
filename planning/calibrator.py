"""
Calibrator — turns the plan→outcome journal into evidence-based parameter
updates for the `PlannerConfig`.

No machine learning: simple split-group analysis.  For each tunable parameter
it splits completed trades into groups (e.g. structure-SL vs ATR-SL, or above
vs below a numeric threshold), compares win-rate / expectancy between groups,
and nudges the parameter in the direction the data supports.

Every adjustment is clamped to ±``calibration_max_adjustment_pct`` per cycle so
the planner evolves smoothly rather than swinging on noisy samples.

Leaf module — standard library + loguru + planning.trade_planner.
"""

from __future__ import annotations

from dataclasses import replace

from loguru import logger

from adaptive.tunable import TuningGuardMixin
from planning.trade_planner import PlannerConfig


def _r(outcome: dict) -> float:
    """Extract the realised R-multiple from an outcome record."""
    try:
        return float(outcome.get("pnl_r", 0.0) or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _win_rate(group: list[dict]) -> float:
    if not group:
        return 0.0
    wins = sum(1 for t in group if _r(t["outcome"]) > 0)
    return wins / len(group)


def _expectancy(group: list[dict]) -> float:
    if not group:
        return 0.0
    return sum(_r(t["outcome"]) for t in group) / len(group)


class Calibrator(TuningGuardMixin):
    """Evidence-based auto-tuning of `PlannerConfig`."""

    # Minimum trades in each side of a split before we trust a comparison.
    _MIN_GROUP = 8

    def __init__(self, config: PlannerConfig | None = None) -> None:
        self.config = config or PlannerConfig()
        self._last_calibration_count = 0

    def should_calibrate(self, completed_trade_count: int) -> bool:
        cfg = self.config
        if not cfg.calibration_enabled:
            return False
        if completed_trade_count < cfg.calibration_min_trades:
            return False
        return (
            completed_trade_count - self._last_calibration_count
            >= cfg.calibration_interval_trades
        )

    def calibrate(self, completed_trades: list[dict]) -> PlannerConfig:
        """Analyse outcomes and return an updated config (clamped per cycle)."""
        # Blocked (returns the unchanged config) when the Tuner Agent is sole
        # authority and this is a direct call rather than an agent-driven one.
        if self._tuning_blocked("calibrate"):
            return self.config
        cfg = self.config
        if not completed_trades:
            return cfg

        trades = completed_trades[-cfg.calibration_lookback_trades:]
        updates: dict[str, float] = {}
        changes: list[str] = []

        self._calibrate_sl_strategy(trades, updates, changes)
        self._calibrate_entry_mode(trades, updates, changes)
        self._calibrate_be_trigger(trades, updates, changes)
        self._calibrate_min_confidence(trades, updates, changes)

        new_cfg = replace(cfg, **updates) if updates else cfg
        self.config = new_cfg
        self._last_calibration_count = len(completed_trades)

        if changes:
            logger.info("[Calibrator] adjusted {} params: {}", len(changes), "; ".join(changes))
        else:
            logger.info("[Calibrator] no parameter changes warranted ({} trades)", len(trades))
        return new_cfg

    # ── Helpers ──────────────────────────────────────────────────────────

    def _clamp(self, current: float, target: float) -> float:
        """Limit movement to ±max_adjustment_pct of the current value."""
        max_pct = self.config.calibration_max_adjustment_pct
        if current == 0:
            return target
        lo = current * (1.0 - max_pct)
        hi = current * (1.0 + max_pct)
        return max(min(target, max(lo, hi)), min(lo, hi))

    @staticmethod
    def _by_plan_field(trades: list[dict], field: str, value) -> list[dict]:
        return [t for t in trades if t.get("plan", {}).get(field) == value]

    # ── Parameter analyses ───────────────────────────────────────────────

    def _calibrate_sl_strategy(self, trades, updates, changes) -> None:
        structure = self._by_plan_field(trades, "sl_strategy", "structure")
        atr = self._by_plan_field(trades, "sl_strategy", "atr")
        if len(structure) < self._MIN_GROUP or len(atr) < self._MIN_GROUP:
            return
        cur = self.config.prefer_structure_sl_within_atr
        if getattr(self.config, "calibration_expectancy_aware", False):
            # #34: compare by expectancy (R-magnitude aware) — a structure group
            # that wins less often but pays far more should still be preferred.
            margin = self.config.calibration_expectancy_margin
            exp_struct = _expectancy(structure)
            exp_atr = _expectancy(atr)
            if exp_struct - exp_atr > margin:
                target = cur * 1.10
            elif exp_atr - exp_struct > margin:
                target = cur * 0.90
            else:
                return
            metric = f"struct exp {exp_struct:+.2f}R vs atr {exp_atr:+.2f}R"
        else:
            wr_struct = _win_rate(structure)
            wr_atr = _win_rate(atr)
            # Structure stops winning more → widen the band that selects them.
            if wr_struct - wr_atr > 0.05:
                target = cur * 1.10
            elif wr_atr - wr_struct > 0.05:
                target = cur * 0.90
            else:
                return
            metric = f"struct WR {wr_struct:.0%} vs atr {wr_atr:.0%}"
        new = round(self._clamp(cur, target), 3)
        if abs(new - cur) > 1e-6:
            updates["prefer_structure_sl_within_atr"] = new
            changes.append(
                f"prefer_structure_sl_within_atr {cur:.2f}→{new:.2f} ({metric})"
            )

    def _calibrate_entry_mode(self, trades, updates, changes) -> None:
        market = self._by_plan_field(trades, "entry_mode", "MARKET")
        limit = self._by_plan_field(trades, "entry_mode", "LIMIT")
        if len(market) < self._MIN_GROUP or len(limit) < self._MIN_GROUP:
            return
        exp_market = _expectancy(market)
        exp_limit = _expectancy(limit)
        cur = self.config.limit_order_zone_distance_atr
        # LIMIT entries clearly better → use them more (lower the distance gate).
        if exp_limit - exp_market > 0.15:
            target = cur * 0.90
        elif exp_market - exp_limit > 0.15:
            target = cur * 1.10
        else:
            return
        new = round(self._clamp(cur, target), 3)
        if abs(new - cur) > 1e-6:
            updates["limit_order_zone_distance_atr"] = new
            changes.append(
                f"limit_order_zone_distance_atr {cur:.2f}→{new:.2f} "
                f"(limit exp {exp_limit:+.2f}R vs market {exp_market:+.2f}R)"
            )

    def _calibrate_be_trigger(self, trades, updates, changes) -> None:
        cur = self.config.default_be_trigger_r
        # Compare premature-stop rate for trades whose BE trigger was at/below
        # the current value vs above it.
        low = [t for t in trades if t.get("plan", {}).get("be_trigger_r", cur) <= cur]
        high = [t for t in trades if t.get("plan", {}).get("be_trigger_r", cur) > cur]
        if len(low) < self._MIN_GROUP or len(high) < self._MIN_GROUP:
            return
        prem_low = self._premature_stop_rate(low)
        prem_high = self._premature_stop_rate(high)
        # Tight BE producing many premature stops → raise the trigger.
        if prem_low - prem_high > 0.10:
            target = cur * 1.10
        elif prem_high - prem_low > 0.10:
            target = cur * 0.90
        else:
            return
        new = round(self._clamp(cur, target), 3)
        if abs(new - cur) > 1e-6:
            updates["default_be_trigger_r"] = new
            changes.append(
                f"default_be_trigger_r {cur:.2f}→{new:.2f} "
                f"(premature-stop low {prem_low:.0%} vs high {prem_high:.0%})"
            )

    def _calibrate_min_confidence(self, trades, updates, changes) -> None:
        cur = self.config.min_confidence_to_enter
        above = [
            t for t in trades
            if t.get("plan", {}).get("confidence", 0.0) >= cur
        ]
        below = [
            t for t in trades
            if t.get("plan", {}).get("confidence", 0.0) < cur
        ]
        if len(above) < self._MIN_GROUP or len(below) < self._MIN_GROUP:
            return
        if getattr(self.config, "calibration_expectancy_aware", False):
            # #34: low-confidence trades earning nearly as much R → relax the
            # gate; earning much less → tighten it. Magnitude-aware, so a few
            # big winners below the gate are not masked by a lower win count.
            margin = self.config.calibration_expectancy_margin
            exp_above = _expectancy(above)
            exp_below = _expectancy(below)
            if exp_above - exp_below > margin:
                target = cur * 1.10
            elif exp_below - exp_above > margin * 0.5:
                target = cur * 0.90
            else:
                return
            metric = f"exp above {exp_above:+.2f}R vs below {exp_below:+.2f}R"
        else:
            wr_above = _win_rate(above)
            wr_below = _win_rate(below)
            # Low-confidence trades doing nearly as well → relax the gate; if they
            # do much worse, tighten it.
            if wr_above - wr_below > 0.15:
                target = cur * 1.10
            elif wr_below - wr_above > 0.05:
                target = cur * 0.90
            else:
                return
            metric = f"WR above {wr_above:.0%} vs below {wr_below:.0%}"
        new = round(self._clamp(cur, target), 4)
        if abs(new - cur) > 1e-6:
            updates["min_confidence_to_enter"] = new
            changes.append(
                f"min_confidence_to_enter {cur:.2f}→{new:.2f} ({metric})"
            )

    @staticmethod
    def _premature_stop_rate(group: list[dict]) -> float:
        """Fraction of trades stopped out near breakeven with little profit."""
        if not group:
            return 0.0
        prem = 0
        for t in group:
            oc = t.get("outcome", {})
            r = _r(oc)
            reason = str(oc.get("outcome", "")).lower()
            if -0.2 <= r <= 0.3 and ("breakeven" in reason or "be" in reason or "stop" in reason):
                prem += 1
        return prem / len(group)
