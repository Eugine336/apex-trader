import MetaTrader5 as mt5
import pandas as pd

PAIR = "EURUSD"
BARS = 50000          # ~35 days of M1
OUT  = "data/EURUSD_M1.csv"

if not mt5.initialize():
    raise SystemExit(f"MT5 init failed: {mt5.last_error()}")

rates = mt5.copy_rates_from_pos(PAIR, mt5.TIMEFRAME_M1, 0, BARS)
mt5.shutdown()

df = pd.DataFrame(rates)
df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)   # MT5 epoch secs -> UTC
df = df[["time", "open", "high", "low", "close"]]             # exact columns the loader needs
df.to_csv(OUT, index=False)
print(f"Wrote {len(df)} rows -> {OUT}")
