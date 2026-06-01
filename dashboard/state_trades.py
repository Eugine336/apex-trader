"""APEX TRADER — Dashboard Open Trades Mixin."""

from typing import Any

from config import get_pip_size
from dashboard.state_helpers import (
    HelpersMixin,
    build_stage,
    normalize_direction,
    safe_float,
)
from platform_context import build_context_for_symbol


class TradesMixin(HelpersMixin):
    """get_open_trades(), _live_open_trades()."""

    def get_open_trades(self) -> dict:
        if self.is_live:
            return self._live_open_trades()
        return {"trades": [], "count": 0}

    def _live_open_trades(self) -> dict:
        trades: list[dict[str, Any]] = []
        positions = getattr(self._trading_loop, "managed_positions", {})

        for oid, pos in positions.items():
            symbol = str(getattr(pos, "symbol", ""))
            direction = normalize_direction(getattr(pos, "direction", ""))
            entry_price = safe_float(getattr(pos, "entry_price", 0.0), 0.0)
            current_price = entry_price

            try:
                tick = self._platform_manager.get_price(symbol)
                if direction == "LONG":
                    current_price = safe_float(getattr(tick, "bid", entry_price), entry_price)
                elif direction == "SHORT":
                    current_price = safe_float(getattr(tick, "ask", entry_price), entry_price)
            except Exception:
                pass

            pip_size = safe_float(get_pip_size(symbol), 0.0001)
            if pip_size <= 0:
                pip_size = 0.0001

            if direction == "LONG":
                pnl_pips = (current_price - entry_price) / pip_size
            elif direction == "SHORT":
                pnl_pips = (entry_price - current_price) / pip_size
            else:
                pnl_pips = 0.0

            # Prefer broker-reported P&L (synced each cycle from the broker).
            # This is the real dollar P&L the broker sees — not a local
            # reconstruction from pip math and estimated pip values.
            broker_pnl = safe_float(getattr(pos, "broker_pnl", 0.0), 0.0)
            if broker_pnl != 0.0:
                pnl_dollars = broker_pnl
            else:
                pos_ctx = build_context_for_symbol(symbol)
                lot_size = safe_float(getattr(pos, "lots", 0.0), 0.0)
                if pos_ctx.uses_stake:
                    stake_usd = safe_float(getattr(pos, "stake_usd", 0.0), 0.0)
                    multiplier = safe_float(getattr(pos, "multiplier", 100), 100)
                    if entry_price > 0 and stake_usd > 0:
                        price_move_pct = (current_price - entry_price) / entry_price
                        if direction == "SHORT":
                            price_move_pct = -price_move_pct
                        pnl_dollars = stake_usd * price_move_pct * multiplier
                    else:
                        pnl_dollars = 0.0
                else:
                    lot_size = safe_float(getattr(pos, "lots", 0.0), 0.0)
                    pip_value = self._estimate_pip_value(symbol)
                    pnl_dollars = pnl_pips * pip_value * lot_size

            lot_size = safe_float(getattr(pos, "lots", 0.0), 0.0)
            trades.append(
                {
                    "id": str(oid),
                    "instrument": symbol,
                    "direction": direction,
                    "entry_price": round(entry_price, 5),
                    "current_price": round(current_price, 5),
                    "stop_loss": round(safe_float(getattr(pos, "sl", 0.0), 0.0), 5),
                    "tp1": round(safe_float(getattr(pos, "tp1", 0.0), 0.0), 5),
                    "tp2": round(safe_float(getattr(pos, "tp2", 0.0), 0.0), 5),
                    "pnl_pips": round(pnl_pips, 1),
                    "pnl_dollars": round(pnl_dollars, 2),
                    "lot_size": round(lot_size, 2),
                    "score": int(round(safe_float(getattr(pos, "score", 0), 0.0))),
                    "stage": build_stage(pos),
                }
            )

        return {"trades": trades, "count": len(trades)}
