"""Conftest — stub platform-specific modules unavailable in CI/Linux sandboxes."""
import sys
import types


def _ensure_mt5_stub():
    """Create a minimal MetaTrader5 stub with required constants."""
    if "MetaTrader5" in sys.modules:
        return
    mt5 = types.ModuleType("MetaTrader5")
    for tf_name in [
        "TIMEFRAME_M1", "TIMEFRAME_M5", "TIMEFRAME_M15",
        "TIMEFRAME_H1", "TIMEFRAME_H4", "TIMEFRAME_D1",
    ]:
        setattr(mt5, tf_name, 1)
    for const in [
        "ORDER_TYPE_BUY", "ORDER_TYPE_SELL",
        "TRADE_ACTION_DEAL", "TRADE_ACTION_SLTP",
        "ORDER_FILLING_IOC", "ORDER_TIME_GTC",
        "POSITION_TYPE_BUY", "POSITION_TYPE_SELL",
    ]:
        setattr(mt5, const, 0)
    mt5.initialize = lambda **kw: True
    mt5.shutdown = lambda: None
    mt5.last_error = lambda: (0, "OK")
    mt5.account_info = lambda: None
    mt5.terminal_info = lambda: None
    mt5.symbol_info = lambda s: None
    mt5.symbol_info_tick = lambda s: None
    mt5.symbol_select = lambda s, e=True: True
    mt5.copy_rates_from_pos = lambda *a, **kw: None
    mt5.positions_get = lambda: ()
    mt5.order_send = lambda req: None
    sys.modules["MetaTrader5"] = mt5


_ensure_mt5_stub()
