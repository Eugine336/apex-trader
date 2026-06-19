"""
APEX TRADER — Symbol Discovery Script
Run this ONCE to find what your MT5 broker actually calls each instrument.
Output tells you exactly what to put in config/brokers/your_broker.json

Usage:
    python scripts/discover_symbols.py
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

# Instruments we want to find
WANT = {
    # Indices
    "US100":  ["US100", "NAS100", "NASDAQ", "USTEC", "NDX", "NAS100.cash", "US100.cash", "USTECH100"],
    "US30":   ["US30", "DJ30", "DOW30", "DOWJONES", "US30.cash", "DJ30.cash", "WALL ST 30"],
    "US500":  ["US500", "SP500", "SPX500", "S&P500", "US500.cash", "SP500.cash"],
    "GER40":  ["GER40", "DAX40", "DAX", "DE40", "GER30", "GER40.cash", "DAX40.cash"],
    "UK100":  ["UK100", "FTSE100", "FTSE", "UK100.cash"],
    "FRA40":  ["FRA40", "CAC40", "CAC", "FRA40.cash", "CAC40.cash"],
    "ESP35":  ["ESP35", "IBEX35", "IBEX", "ESP35.cash"],
    "JP225":  ["JP225", "JPN225", "NIKKEI", "N225", "JP225.cash"],
    "AUS200": ["AUS200", "ASX200", "AUS200.cash"],
    "HK50":   ["HK50", "HSI50", "HANGSENG", "HK50.cash"],
    # Commodities
    "XAUUSD": ["XAUUSD", "GOLD", "XAUUSDm", "XAUUSD.raw"],
    "XAGUSD": ["XAGUSD", "SILVER", "XAGUSDm"],
    "XTIUSD": ["XTIUSD", "USOUSD", "USOIL", "WTI", "CL"],
    "XBRUSD": ["XBRUSD", "UKOUSD", "UKOIL", "BRENT"],
}

try:
    import MetaTrader5 as mt5
except ImportError:
    print("❌ MetaTrader5 package not installed.")
    print("   Run: pip install MetaTrader5")
    sys.exit(1)

if not mt5.initialize():
    print("❌ MT5 not connected. Open MetaTrader5 first.")
    sys.exit(1)

print("=" * 60)
print("APEX TRADER — Symbol Discovery")
print("=" * 60)
print()

found = {}
not_found = []

for apex_name, candidates in WANT.items():
    resolved = None
    for candidate in candidates:
        info = mt5.symbol_info(candidate)
        if info is not None:
            resolved = candidate
            # Make sure it's visible in Market Watch
            if not info.visible:
                mt5.symbol_select(candidate, True)
            break

    if resolved:
        found[apex_name] = resolved
        print(f"✅  {apex_name:12} → {resolved}")
    else:
        not_found.append(apex_name)
        print(f"❌  {apex_name:12} → NOT FOUND on this broker")

print()
print("=" * 60)
print("PASTE THIS INTO config/brokers/YOUR_BROKER.json overrides:")
print("=" * 60)
print()
print('"overrides": {')
for apex_name, broker_name in found.items():
    print(f'    "{apex_name}": "{broker_name}",')
print('}')

if not_found:
    print()
    print("⚠️  These instruments were NOT found on your broker:")
    for s in not_found:
        print(f"   - {s}")
    print()
    print("Options:")
    print("  1. Check Market Watch in MT5 — search manually and add to JSON")
    print("  2. Remove them from config.py enabled_categories")
    print("  3. Leave them — they'll be skipped gracefully with a warning")

mt5.shutdown()
