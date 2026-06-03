import MetaTrader5 as mt5
import pandas as pd

PAIR = "EURUSD"
TFS = {"H4": (mt5.TIMEFRAME_H4, 3000), "H1": (mt5.TIMEFRAME_H1, 6000),
       "M15": (mt5.TIMEFRAME_M15, 20000), "M5": (mt5.TIMEFRAME_M5, 50000),
       "M1": (mt5.TIMEFRAME_M1, 50000)}

if not mt5.initialize():
    raise SystemExit(f"MT5 init failed: {mt5.last_error()}")
for name, (tf, bars) in TFS.items():
    rates = mt5.copy_rates_from_pos(PAIR, tf, 0, bars)
    df = pd.DataFrame(rates)
    df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)
    df = df[["time", "open", "high", "low", "close"]]
    out = f"data/{PAIR}_{name}.csv"
    df.to_csv(out, index=False)
    print(f"{name}: wrote {len(df)} rows -> {out}")
mt5.shutdown()