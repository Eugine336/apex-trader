"""
APEX TRADER — Position Sizer
Every trade is sized precisely. Not too big, not too small.
Risk exactly 2 percent (or less in recovery). Never more.
"""

from dataclasses import dataclass

from loguru import logger

from config import INSTRUMENT_REGISTRY, InstrumentInfo


@dataclass
class SizeResult:
    lots: float
    risk_amount: float
    risk_pips: float
    pip_value: float
    max_loss: float
    margin_estimate: float


class PositionSizer:
    """
    Converts a risk percentage and stop distance into an exact lot size.
    Adjusts for volatility when ATR data is available.
    """

    MIN_LOT = 0.01
    MAX_LOT = 100.0

    def calculate(
        self,
        account_balance: float,
        risk_pct: float,
        entry_price: float,
        stop_loss: float,
        pip_size: float,
        pip_value_per_lot: float = 10.0,
        leverage: int = 100,
    ) -> SizeResult:
        risk_amount = account_balance * risk_pct
        risk_pips = abs(entry_price - stop_loss) / pip_size

        if risk_pips <= 0:
            logger.warning("Risk pips is zero — returning minimum lot")
            return SizeResult(
                lots=self.MIN_LOT,
                risk_amount=risk_amount,
                risk_pips=0.0,
                pip_value=pip_value_per_lot,
                max_loss=0.0,
                margin_estimate=0.0,
            )

        lots = risk_amount / (risk_pips * pip_value_per_lot)
        lots = round(max(self.MIN_LOT, min(lots, self.MAX_LOT)), 2)
        max_loss = lots * risk_pips * pip_value_per_lot
        margin_estimate = self.calculate_margin(lots, entry_price, leverage)

        return SizeResult(
            lots=lots,
            risk_amount=round(risk_amount, 2),
            risk_pips=round(risk_pips, 2),
            pip_value=pip_value_per_lot,
            max_loss=round(max_loss, 2),
            margin_estimate=round(margin_estimate, 2),
        )

    def calculate_for_instrument(
        self,
        symbol: str,
        account_balance: float,
        risk_pct: float,
        entry_price: float,
        stop_loss: float,
        leverage: int = 100,
    ) -> SizeResult:
        info = INSTRUMENT_REGISTRY.get(symbol.upper())
        if info is None:
            logger.warning(f"Unknown instrument {symbol} — using forex defaults")
            return self.calculate(
                account_balance, risk_pct, entry_price, stop_loss,
                pip_size=0.0001, pip_value_per_lot=10.0, leverage=leverage,
            )

        return self.calculate(
            account_balance, risk_pct, entry_price, stop_loss,
            pip_size=info.pip_size,
            pip_value_per_lot=info.pip_value_per_lot,
            leverage=leverage,
        )

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

        if ratio >= 2.0:
            factor = max_adjustment
        elif ratio > 1.0:
            factor = 1.0 - (ratio - 1.0) * (1.0 - max_adjustment)
        elif ratio < 0.5:
            factor = 1.5
        else:
            factor = 1.0 + (1.0 - ratio) * 0.5

        adjusted = base_lots * factor
        return round(max(self.MIN_LOT, min(adjusted, self.MAX_LOT)), 2)

    @staticmethod
    def calculate_margin(
        lots: float,
        price: float,
        leverage: int = 100,
        contract_size: int = 100_000,
    ) -> float:
        return (lots * contract_size * price) / leverage
