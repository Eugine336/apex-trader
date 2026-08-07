"""
APEX TRADER — Position Sizer
Every trade is sized precisely. Not too big, not too small.
Risk exactly 2 percent (or less in recovery). Never more.

Platform-aware:
  MT5  (lots)   →  risk_amount / (risk_pips × pip_value_per_lot)
  Deriv (stake) →  stake such that max_loss == risk_amount
"""

from dataclasses import dataclass
import math
from typing import Optional

from loguru import logger

from config import INSTRUMENT_REGISTRY
from platform_context import PlatformContext


@dataclass
class SizeResult:
    lots: float
    stake_usd: float          # populated for Deriv; 0.0 for MT5
    risk_amount: float
    risk_pips: float
    pip_value: float
    max_loss: float
    margin_estimate: float
    sizing_mode: str          # "lots" | "stake"


class PositionSizer:
    """
    Converts a risk percentage and stop distance into an exact lot size (MT5)
    or dollar stake (Deriv).  A PlatformContext tells it which path to take.
    Adjusts for volatility when ATR data is available.
    """

    MIN_LOT = 0.01
    MAX_LOT = 100.0

    def __init__(
        self,
        micro_account_threshold_usd: float = 100.0,
        deriv_min_stake_usd: float = 0.35,
        max_risk_pct_per_trade: float = 5.0,
        engine_cap: float = 0.025,
        allow_min_lot_over_risk: bool = False,
    ):
        self.micro_account_threshold_usd = micro_account_threshold_usd
        self.deriv_min_stake_usd = deriv_min_stake_usd
        self.max_risk_pct_per_trade = max_risk_pct_per_trade
        # Hard per-trade ceiling enforced by the RiskEngine (fraction, e.g. 0.025
        # = 2.5%). Sizing clamps to this so the sizer can never risk more than the
        # engine believes it approved.
        self._engine_cap = engine_cap
        # Opportunity-harvesting opt-in (Constitution Part V): when True, an entry
        # whose UNAVOIDABLE broker-minimum lot risks more than the per-trade
        # ceiling is still taken at the minimum lot rather than rejected — so a
        # genuinely small account can participate in the opportunities the Brain
        # authorises. The broker's own margin/lot floors (Part IX Art 3) remain
        # the true hard limit. Default OFF (unchanged, conservative behaviour).
        self.allow_min_lot_over_risk = bool(allow_min_lot_over_risk)

    def _sanitize_risk_pct(self, risk_pct: float, label: str = "") -> float:
        """Validate that ``risk_pct`` is a fraction (0-1), not a percentage.

        Returns a safe fraction, clamped to the hard per-trade cap. A value of
        0/negative returns 0.0 (skip). A value > 1.0 is almost certainly a
        percentage passed by mistake (e.g. 2.0 meaning 200%) and is clamped to
        the cap rather than silently risking the whole account.
        """
        cap = self.max_risk_pct_per_trade / 100.0
        tag = label or "trade"
        if risk_pct is None or not math.isfinite(risk_pct):
            logger.error(
                "[PositionSizer] {} risk_pct {!r} is non-finite (NaN/inf) — skipping (invalid)",
                tag, risk_pct,
            )
            return 0.0
        if risk_pct <= 0:
            logger.warning("[PositionSizer] {} risk_pct {!r} ≤ 0 — skipping (invalid)", tag, risk_pct)
            return 0.0
        if risk_pct > 1.0:
            logger.error(
                "[PositionSizer] {} risk_pct {} > 1.0 — looks like a percentage, "
                "not a fraction; clamping to cap {:.4f}", tag, risk_pct, cap,
            )
            return cap
        if risk_pct > cap:
            logger.warning(
                "[PositionSizer] {} risk_pct {:.4f} exceeds per-trade cap {:.4f} — clamping",
                tag, risk_pct, cap,
            )
            return cap
        return risk_pct

    # ── MT5 lot-based sizing ─────────────────────────────────────────────

    def calculate(
        self,
        account_balance: float,
        risk_pct: float,
        entry_price: float,
        stop_loss: float,
        pip_size: float,
        pip_value_per_lot: float = 10.0,
        leverage: int = 100,
        context: Optional[PlatformContext] = None,
        symbol: str = "",
    ) -> SizeResult:
        """
        Unified entry point.  Delegates to stake path if context.uses_stake,
        falls back to lot path otherwise.
        """
        if context is not None and context.uses_stake:
            return self.calculate_stake(
                account_balance=account_balance,
                risk_pct=risk_pct,
                entry_price=entry_price,
                stop_loss=stop_loss,
            )

        risk_pct = self._sanitize_risk_pct(risk_pct, symbol)
        # Harmonize with the engine's hard cap so the sizer never risks more than
        # the engine believes it approved.
        risk_pct = min(risk_pct, self._engine_cap)
        if risk_pct <= 0.0:
            logger.warning(
                "[PositionSizer] {} risk_pct resolved to 0 — skipping (no allocation)",
                symbol or "trade",
            )
            return SizeResult(
                lots=0.0,
                stake_usd=0.0,
                risk_amount=0.0,
                risk_pips=0.0,
                pip_value=pip_value_per_lot,
                max_loss=0.0,
                margin_estimate=0.0,
                sizing_mode="skip_zero_alloc",
            )
        risk_amount = account_balance * risk_pct
        if pip_size <= 0 or pip_value_per_lot <= 0:
            logger.warning(
                "Invalid instrument params (pip_size={}, pip_value_per_lot={}) — "
                "skipping trade (cannot size safely)",
                pip_size, pip_value_per_lot,
            )
            return SizeResult(
                lots=0.0,
                stake_usd=0.0,
                risk_amount=round(risk_amount, 2),
                risk_pips=0.0,
                pip_value=pip_value_per_lot,
                max_loss=0.0,
                margin_estimate=0.0,
                sizing_mode="skip_invalid_params",
            )
        risk_pips = abs(entry_price - stop_loss) / pip_size

        if risk_pips <= 0:
            logger.warning("Risk pips is zero or negative — skipping trade (invalid stop)")
            return SizeResult(
                lots=0.0,
                stake_usd=0.0,
                risk_amount=risk_amount,
                risk_pips=0.0,
                pip_value=pip_value_per_lot,
                max_loss=0.0,
                margin_estimate=0.0,
                sizing_mode="skip_invalid_stop",
            )

        lots = risk_amount / (risk_pips * pip_value_per_lot)
        # Last line of defence before a lot size leaves for the broker: a
        # non-finite size (NaN/inf from an upstream learned multiplier) must
        # never be sent — skip the trade rather than clamp it to a min lot.
        if not math.isfinite(lots):
            logger.error(
                "[PositionSizer] computed non-finite lots for {} "
                "(risk_amount={!r}, risk_pips={!r}, pip_value={!r}) — skipping trade",
                symbol or "trade", risk_amount, risk_pips, pip_value_per_lot,
            )
            return SizeResult(
                lots=0.0,
                stake_usd=0.0,
                risk_amount=round(risk_amount, 2) if math.isfinite(risk_amount) else 0.0,
                risk_pips=round(risk_pips, 2) if math.isfinite(risk_pips) else 0.0,
                pip_value=pip_value_per_lot,
                max_loss=0.0,
                margin_estimate=0.0,
                sizing_mode="skip_non_finite",
            )
        lots = round(max(self.MIN_LOT, min(lots, self.MAX_LOT)), 2)
        max_loss = lots * risk_pips * pip_value_per_lot
        margin_estimate = self.calculate_margin(lots, entry_price, leverage)

        lots, sizing_mode = self._adjust_for_account_size(
            lots, account_balance, risk_amount, max_loss, symbol,
        )

        return SizeResult(
            lots=lots,
            stake_usd=0.0,
            risk_amount=round(risk_amount, 2),
            risk_pips=round(risk_pips, 2),
            pip_value=pip_value_per_lot,
            max_loss=round(max_loss, 2) if lots > 0 else 0.0,
            margin_estimate=round(margin_estimate, 2) if lots > 0 else 0.0,
            sizing_mode=sizing_mode,
        )

    # ── Deriv stake-based sizing ─────────────────────────────────────────

    def calculate_stake(
        self,
        account_balance: float,
        risk_pct: float,
        entry_price: float,
        stop_loss: float,
        multiplier: int = 100,
    ) -> SizeResult:
        """
        Deriv multiplier contract sizing.

        On Deriv the stake IS the max loss — stake = risk_amount.
        The multiplier controls leverage (profit potential), not loss size.
        """
        risk_pct = self._sanitize_risk_pct(risk_pct, "deriv")
        risk_pct = min(risk_pct, self._engine_cap)
        risk_amount = account_balance * risk_pct
        risk_distance = abs(entry_price - stop_loss)
        stake = round(risk_amount, 2)

        # Last line of defence before a stake leaves for the Deriv broker: a
        # non-finite stake (NaN/inf from an upstream learned multiplier) must
        # never be sent — skip the trade rather than dispatch garbage. Mirrors
        # the MT5 lot path's isfinite guard.
        if not math.isfinite(stake):
            logger.error(
                "[PositionSizer] deriv computed non-finite stake "
                "(account_balance={!r}, risk_pct={!r}) — skipping trade",
                account_balance, risk_pct,
            )
            return SizeResult(
                lots=0.0,
                stake_usd=0.0,
                risk_amount=round(risk_amount, 2) if math.isfinite(risk_amount) else 0.0,
                risk_pips=round(risk_distance, 5) if math.isfinite(risk_distance) else 0.0,
                pip_value=0.0,
                max_loss=0.0,
                margin_estimate=0.0,
                sizing_mode="stake_skip_non_finite",
            )

        if risk_pct <= 0.0 or stake <= 0.0:
            logger.warning(
                "[PositionSizer] deriv stake resolved to ${:.2f} — skipping (no allocation)",
                stake,
            )
            return SizeResult(
                lots=0.0,
                stake_usd=0.0,
                risk_amount=round(risk_amount, 2),
                risk_pips=round(risk_distance, 5),
                pip_value=0.0,
                max_loss=0.0,
                margin_estimate=0.0,
                sizing_mode="stake_skip_zero_alloc",
            )

        if account_balance < self.micro_account_threshold_usd and stake < self.deriv_min_stake_usd:
            logger.warning(
                f"Micro account skip: stake ${stake:.2f} below Deriv minimum ${self.deriv_min_stake_usd}"
            )
            return SizeResult(
                lots=0.0,
                stake_usd=0.0,
                risk_amount=round(risk_amount, 2),
                risk_pips=round(risk_distance, 5),
                pip_value=0.0,
                max_loss=0.0,
                margin_estimate=0.0,
                sizing_mode="stake_skip_micro",
            )

        return SizeResult(
            lots=0.0,           # meaningless for Deriv
            stake_usd=stake,
            risk_amount=round(risk_amount, 2),
            risk_pips=round(risk_distance, 5),
            pip_value=0.0,      # no pip-value concept on Deriv
            max_loss=stake,     # max loss == stake for multiplier contracts
            margin_estimate=stake,
            sizing_mode="stake",
        )

    # ── Micro account adjustment ────────────────────────────────────────

    def _adjust_for_account_size(
        self,
        lots: float,
        account_balance: float,
        risk_amount: float,
        max_loss: float,
        symbol: str = "",
    ) -> tuple[float, str]:
        if lots <= 0 or account_balance <= 0:
            return lots, "lots"

        # Within the soft tolerance — the size is essentially on-target.
        if max_loss <= risk_amount * 1.5:
            return lots, "lots"

        # The broker minimum lot floored the size upward (unavoidable on
        # small accounts). Judge by the ACTUAL fraction of the account at
        # risk rather than a fixed ratio: a micro account may still trade as
        # long as the real risk stays within the hard per-trade cap.
        actual_risk_pct = (max_loss / account_balance) * 100.0
        label = symbol or "trade"

        # The elevated ``max_risk_pct_per_trade`` tolerance exists ONLY because a
        # genuine micro account ($5–$200) cannot trade below the 0.01 min lot.
        # Above the micro threshold the account is large enough to size on
        # target, so the min-lot tolerance must NOT exceed the engine's hard
        # per-trade cap — otherwise min-lot inflation silently lets a normal
        # account risk up to 5% when the engine only ever approved 2.5%.
        is_micro = account_balance < self.micro_account_threshold_usd
        engine_cap_pct = self._engine_cap * 100.0
        ceiling_pct = (
            self.max_risk_pct_per_trade if is_micro
            else min(self.max_risk_pct_per_trade, engine_cap_pct)
        )

        if actual_risk_pct <= ceiling_pct:
            logger.info(
                f"[PositionSizer] Micro-account mode: {label} using min lot {lots} "
                f"at {actual_risk_pct:.1f}% risk (target ${risk_amount:.2f}, "
                f"min-lot max_loss ${max_loss:.2f})"
            )
            return lots, "lots"

        # Opportunity-harvesting opt-in: the ceiling is exceeded only because the
        # broker minimum lot is unavoidable. When the operator has opted in, take
        # the opportunity at the minimum lot rather than rejecting it — the
        # broker's margin/lot floor remains the real limit. Logged loudly so the
        # elevated risk is never silent.
        if self.allow_min_lot_over_risk:
            logger.warning(
                f"[PositionSizer] {label} opportunity-harvest: taking min lot {lots} "
                f"at {actual_risk_pct:.1f}% risk (${max_loss:.2f}) — ABOVE the "
                f"{ceiling_pct:.1f}% ceiling for a ${account_balance:.2f} account "
                f"(allow_min_lot_over_risk opt-in)"
            )
            return lots, "lots_min_lot_over_risk_opt_in"

        skip_mode = (
            "lots_skip_micro"
            if account_balance < self.micro_account_threshold_usd
            else "skip_min_lot_over_risk"
        )
        logger.warning(
            f"[PositionSizer] {label} skip: min lot {lots} risks "
            f"{actual_risk_pct:.1f}% (${max_loss:.2f}) — exceeds max "
            f"{ceiling_pct:.1f}% per-trade cap for "
            f"${account_balance:.2f} account"
        )
        return 0.0, skip_mode

    # ── Instrument-aware sizing ─────────────────────────────────────────

    def calculate_for_instrument(
        self,
        symbol: str,
        account_balance: float,
        risk_pct: float,
        entry_price: float,
        stop_loss: float,
        leverage: int = 100,
        context: Optional[PlatformContext] = None,
    ) -> SizeResult:
        if context is not None and context.uses_stake:
            return self.calculate_stake(
                account_balance=account_balance,
                risk_pct=risk_pct,
                entry_price=entry_price,
                stop_loss=stop_loss,
            )

        info = INSTRUMENT_REGISTRY.get(symbol.upper())
        if info is None:
            logger.warning(f"Unknown instrument {symbol} — using forex defaults")
            return self.calculate(
                account_balance, risk_pct, entry_price, stop_loss,
                pip_size=0.0001, pip_value_per_lot=10.0, leverage=leverage,
                symbol=symbol,
            )

        return self.calculate(
            account_balance, risk_pct, entry_price, stop_loss,
            pip_size=info.pip_size,
            pip_value_per_lot=info.pip_value_per_lot,
            leverage=leverage,
            context=context,
            symbol=symbol,
        )

    # ── Volatility adjustment ─────────────────────────────────────────

    def adjust_for_volatility(
        self,
        base_lots: float,
        current_atr: float,
        average_atr: float,
        max_adjustment: float = 0.5,
    ) -> float:
        if average_atr <= 0 or current_atr <= 0:
            return base_lots

        ratio = current_atr / average_atr

        # #30 — continuous everywhere (the old ``ratio < 0.5 → 1.5`` branch
        # created a jump from 1.25 to 1.5 at ratio 0.5). The low-volatility
        # up-scaling now extends the same linear ramp and is capped at 1.5.
        if ratio >= 2.0:
            factor = max_adjustment
        elif ratio > 1.0:
            factor = 1.0 - (ratio - 1.0) * (1.0 - max_adjustment)
        else:
            factor = min(1.5, 1.0 + (1.0 - ratio) * 0.5)

        adjusted = base_lots * factor
        return round(max(self.MIN_LOT, min(adjusted, self.MAX_LOT)), 2)

    @staticmethod
    def calculate_margin(
        lots: float,
        price: float,
        leverage: int = 100,
        contract_size: int = 100_000,
    ) -> float:
        if leverage <= 0:
            return 0.0
        return (lots * contract_size * price) / leverage
