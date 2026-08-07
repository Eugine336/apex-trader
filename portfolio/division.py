"""APEX TRADER — Portfolio Division (capital allocation & exposure).

``PortfolioDivision.evaluate`` is the single, auditable sizing path. It replaces
the scattered ``fresh PositionSizer() + ad-hoc multiplier chain`` that used to
live inline in the entry handler and bypassed the protective daily-loss budget
of the (now-deprecated) ``RiskEngine.assess`` chain.

Responsibilities:

1. **Sizing** — turn a (drawdown-adjusted) base risk %, a stop distance, and the
   organisation's transparent sizing factors into one concrete lot/stake size.
2. **Daily-loss budget** — never let a single trade's max loss exceed the
   account's remaining daily-loss room; reduce the size to fit, and reject when
   even the broker minimum lot would breach it (the protection ``assess`` owned).
3. **Exposure** — report currency / broker / pair concentration for the book and
   apply optional, transparent budget haircuts.

It does NOT veto on safety (Compliance), form a market opinion (Consensus), or
touch a broker (Execution).
"""

from __future__ import annotations

from typing import Any, Optional, Sequence

from loguru import logger

from portfolio.models import (
    OpenPositionView,
    PortfolioAccount,
    PortfolioCandidate,
    SizingFactors,
    normalize_book,
)
from portfolio.verdict import (
    EXPOSURE_FULL,
    EXPOSURE_REDUCED,
    PortfolioVerdict,
)

# Sizing bounds — match the historical inline behaviour exactly.
_SIZE_FLOOR = 0.15      # combined multiplier can de-risk to 15% of base, no lower
_SIZE_CEILING = 1.0     # never inflate above the sizer's per-trade ceiling
_MIN_LOT = 0.01
_MIN_STAKE = 0.35


class PortfolioDivision:
    """Cohesive capital-allocation layer for the whole book."""

    def __init__(
        self,
        position_sizer: Any,
        *,
        correlation_engine: Any = None,
        size_floor: float = _SIZE_FLOOR,
        size_ceiling: float = _SIZE_CEILING,
        max_pair_concentration: int = 0,
        max_broker_positions: int = 0,
    ) -> None:
        # The sizer is injected (shared with the RiskEngine), never freshly
        # constructed per-trade — so its per-trade risk ceiling is authoritative.
        self._sizer = position_sizer
        self._corr = correlation_engine
        self._size_floor = float(size_floor)
        self._size_ceiling = float(size_ceiling)
        # Optional soft budgets. 0 == disabled (no-op) so behaviour is unchanged
        # unless an operator tightens them.
        self._max_pair_concentration = int(max_pair_concentration)
        self._max_broker_positions = int(max_broker_positions)

    # ── Public API ────────────────────────────────────────────────────────

    def evaluate(
        self,
        candidate: PortfolioCandidate,
        book: Optional[Sequence[Any]],
        account: PortfolioAccount,
        factors: SizingFactors,
    ) -> PortfolioVerdict:
        rationale: list[str] = []
        positions = normalize_book(book)

        balance = float(account.balance or 0.0)
        if balance <= 0:
            return PortfolioVerdict.rejected(
                "account balance unavailable or non-positive — refusing to size",
            )

        base_risk_pct = float(factors.base_risk_pct or 0.0)
        if base_risk_pct <= 0:
            return PortfolioVerdict.rejected("base risk fraction is zero")

        uses_stake = bool(getattr(candidate.context, "uses_stake", False))

        # ── 1. Base size from the shared sizer ──────────────────────────
        size_result = self._size(candidate, account, base_risk_pct)
        if size_result is None or "skip" in getattr(size_result, "sizing_mode", "skip"):
            mode = getattr(size_result, "sizing_mode", "skip") if size_result else "skip"
            return PortfolioVerdict.rejected(
                f"position sizing rejected ({mode})",
            )

        # ── 2. Transparent multiplier fold (de-risking only) ────────────
        raw_mult = factors.product()
        combined_mult = max(self._size_floor, min(self._size_ceiling, raw_mult))
        factor_str = " ".join(
            f"{name}×{val:.2f}" for name, val in factors.as_ordered()
        )
        rationale.append(
            f"base={base_risk_pct:.3%} factors[{factor_str}] "
            f"raw×{raw_mult:.2f} → clamped×{combined_mult:.2f}"
        )

        lots = float(getattr(size_result, "lots", 0.0) or 0.0)
        stake = float(getattr(size_result, "stake_usd", 0.0) or 0.0)
        if abs(combined_mult - 1.0) > 1e-6:
            if lots > 0:
                lots = round(max(_MIN_LOT, lots * combined_mult), 2)
            if stake > 0:
                stake = round(max(_MIN_STAKE, stake * combined_mult), 2)

        # ── 3. Daily-loss budget enforcement (the assess() protection) ──
        eff_max_loss = self._effective_max_loss(
            size_result, lots, stake, uses_stake,
        )
        budget_verdict = self._enforce_daily_budget(
            candidate, account, factors, combined_mult, uses_stake,
            lots, stake, eff_max_loss, rationale,
        )
        budget_reduced = False
        if budget_verdict is not None:
            # Either a rejection or a re-sized (REDUCED) result.
            if not budget_verdict.approved:
                return budget_verdict
            lots = budget_verdict.lots
            stake = budget_verdict.stake_usd
            eff_max_loss = budget_verdict.max_loss
            budget_reduced = True

        final_size = stake if uses_stake else lots
        if final_size <= 0:
            return PortfolioVerdict.rejected(
                f"position size resolved to zero (mode={getattr(size_result, 'sizing_mode', '')})",
                rationale=rationale,
            )

        # ── 4. Exposure accounting + optional soft budgets ──────────────
        exposure_verdict, impact = self._evaluate_exposure(
            candidate, positions, base_risk_pct, rationale,
        )
        if budget_reduced and exposure_verdict == EXPOSURE_FULL:
            # A daily-budget haircut is itself a reduction — surface it.
            exposure_verdict = EXPOSURE_REDUCED

        effective_risk_pct = (eff_max_loss / balance) if balance > 0 else base_risk_pct

        logger.info(
            "[PORTFOLIO] {} size×{:.2f} → {} {:.4g} | risk≈{:.3%} maxloss≈${:.2f} | {}",
            candidate.symbol, combined_mult,
            "stake" if uses_stake else "lots", final_size,
            effective_risk_pct, eff_max_loss, exposure_verdict,
        )

        return PortfolioVerdict(
            approved=True,
            approved_size=final_size,
            sizing_mode="stake" if uses_stake else "lots",
            risk_pct=round(effective_risk_pct, 6),
            max_loss=round(eff_max_loss, 2),
            exposure_verdict=exposure_verdict,
            combined_mult=round(combined_mult, 3),
            rationale=rationale,
            portfolio_impact=impact,
        )

    # ── Internals ───────────────────────────────────────────────────────

    def _size(
        self,
        candidate: PortfolioCandidate,
        account: PortfolioAccount,
        risk_pct: float,
    ) -> Any:
        """Delegate to the shared PositionSizer (lots or stake)."""
        try:
            return self._sizer.calculate(
                account_balance=account.balance,
                risk_pct=risk_pct,
                entry_price=candidate.entry_price,
                stop_loss=candidate.stop_loss,
                pip_size=candidate.pip_size,
                pip_value_per_lot=candidate.pip_value_per_lot,
                context=candidate.context,
                symbol=candidate.symbol,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("[PORTFOLIO] sizer failed for {}: {}", candidate.symbol, exc)
            return None

    @staticmethod
    def _effective_max_loss(
        size_result: Any,
        lots: float,
        stake: float,
        uses_stake: bool,
    ) -> float:
        """Dollar max loss at the post-multiplier size."""
        if uses_stake:
            # On Deriv the stake IS the max loss.
            return float(stake)
        risk_pips = float(getattr(size_result, "risk_pips", 0.0) or 0.0)
        pip_value = float(getattr(size_result, "pip_value", 0.0) or 0.0)
        if lots > 0 and risk_pips > 0 and pip_value > 0:
            return lots * risk_pips * pip_value
        # Fall back to the sizer's own max_loss scaled by the lot change.
        base_lots = float(getattr(size_result, "lots", 0.0) or 0.0)
        base_max = float(getattr(size_result, "max_loss", 0.0) or 0.0)
        if base_lots > 0 and base_max > 0:
            return base_max * (lots / base_lots)
        return base_max

    def _enforce_daily_budget(
        self,
        candidate: PortfolioCandidate,
        account: PortfolioAccount,
        factors: SizingFactors,
        combined_mult: float,
        uses_stake: bool,
        lots: float,
        stake: float,
        eff_max_loss: float,
        rationale: list[str],
    ) -> Optional[PortfolioVerdict]:
        """Reduce/reject the size so it never breaches the remaining daily-loss
        budget — the protection the dead ``RiskEngine.assess`` chain owned and
        the live inline path dropped.

        Returns ``None`` when no adjustment is needed, a rejected verdict when
        the trade cannot fit even at the broker minimum, or an approved
        ``REDUCED`` verdict carrying the re-sized position.
        """
        cap_pct = float(account.daily_loss_cap_pct or 0.0)
        balance = float(account.balance or 0.0)
        if cap_pct <= 0 or balance <= 0:
            return None  # budget enforcement disabled / no data

        # daily_pnl is negative for a loss; remaining room shrinks as losses grow.
        remaining = (cap_pct / 100.0 * balance) + float(account.daily_pnl or 0.0)
        if remaining <= 0:
            rationale.append("no daily-loss budget remaining")
            return PortfolioVerdict.rejected(
                "no daily-loss budget remaining", rationale=rationale,
            )
        if eff_max_loss <= remaining:
            return None  # fits the budget — no change

        # Reduce risk to fit the remaining budget, then re-apply the factor fold.
        reduced_risk = remaining / balance
        resized = self._size(candidate, account, reduced_risk)
        if resized is None or "skip" in getattr(resized, "sizing_mode", "skip"):
            return PortfolioVerdict.rejected(
                "cannot size within remaining daily-loss budget",
                rationale=rationale,
            )
        r_lots = float(getattr(resized, "lots", 0.0) or 0.0)
        r_stake = float(getattr(resized, "stake_usd", 0.0) or 0.0)
        if abs(combined_mult - 1.0) > 1e-6:
            if r_lots > 0:
                r_lots = round(max(_MIN_LOT, r_lots * combined_mult), 2)
            if r_stake > 0:
                r_stake = round(max(_MIN_STAKE, r_stake * combined_mult), 2)
        r_max_loss = self._effective_max_loss(resized, r_lots, r_stake, uses_stake)

        # The broker minimum lot can floor max_loss back above the budget.
        if r_max_loss > remaining + 1e-9:
            rationale.append(
                f"min-lot max_loss ${r_max_loss:.2f} exceeds remaining daily "
                f"budget ${remaining:.2f} — rejected"
            )
            return PortfolioVerdict.rejected(
                f"sized max_loss ${r_max_loss:.2f} exceeds remaining daily "
                f"budget ${remaining:.2f}",
                rationale=rationale,
            )

        rationale.append(
            f"size reduced to fit daily budget: risk {reduced_risk:.3%} "
            f"(maxloss ${r_max_loss:.2f} ≤ remaining ${remaining:.2f})"
        )
        final = r_stake if uses_stake else r_lots
        return PortfolioVerdict(
            approved=True,
            approved_size=final,
            sizing_mode="stake" if uses_stake else "lots",
            max_loss=round(r_max_loss, 2),
            exposure_verdict=EXPOSURE_REDUCED,
            combined_mult=round(combined_mult, 3),
            rationale=rationale,
        )

    def _evaluate_exposure(
        self,
        candidate: PortfolioCandidate,
        positions: list[OpenPositionView],
        risk_pct: float,
        rationale: list[str],
    ) -> tuple[str, dict[str, Any]]:
        """Compute book exposure and apply optional, transparent soft budgets.

        Hard correlation/exposure rejection is owned by Compliance; this layer
        reports the picture and only marks ``REDUCED`` when an explicitly
        configured Portfolio budget is exceeded.
        """
        impact: dict[str, Any] = {}
        verdict = EXPOSURE_FULL

        sym = candidate.symbol.upper()
        # Pair concentration (same exact instrument already open).
        pair_count = sum(1 for p in positions if p.symbol == sym)
        impact["pair_count"] = pair_count + 1

        # Broker concentration.
        broker = (candidate.broker or "").lower()
        if broker:
            broker_count = sum(
                1 for p in positions if (p.broker or "").lower() == broker
            )
            impact["broker_positions"] = {broker: broker_count + 1}

        # Currency-decomposition exposure (reuse the live CorrelationEngine).
        if self._corr is not None:
            try:
                from brain.correlation_engine import OpenTrade
                trades = [
                    OpenTrade(pair=p.symbol, direction=p.direction or "LONG",
                              risk_pct=(p.risk_pct or risk_pct))
                    for p in positions
                ]
                trades.append(
                    OpenTrade(pair=sym, direction=candidate.direction.upper(),
                              risk_pct=risk_pct)
                )
                emap = self._corr.calculate_exposure(trades)
                impact["currency_exposures"] = dict(emap.currency_exposures)
                impact["max_single_currency_exposure"] = (
                    emap.max_single_currency_exposure
                )
            except Exception as exc:  # noqa: BLE001
                logger.debug("[PORTFOLIO] exposure calc skipped: {}", exc)

        # Optional soft budgets (disabled by default → never alters behaviour).
        if (
            self._max_pair_concentration > 0
            and impact["pair_count"] > self._max_pair_concentration
        ):
            verdict = EXPOSURE_REDUCED
            rationale.append(
                f"pair concentration {impact['pair_count']} > "
                f"budget {self._max_pair_concentration}"
            )
        if self._max_broker_positions > 0 and broker:
            bc = impact.get("broker_positions", {}).get(broker, 0)
            if bc > self._max_broker_positions:
                verdict = EXPOSURE_REDUCED
                rationale.append(
                    f"broker {broker} positions {bc} > "
                    f"budget {self._max_broker_positions}"
                )

        return verdict, impact
