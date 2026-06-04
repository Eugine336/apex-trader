"""
APEX TRADER — discover_icmarkets.py
=====================================
One-time script to discover IC Markets symbol names on MT5
and write the correct broker config file.

Run once after connecting to IC Markets:
  python discover_icmarkets.py

This fixes the 5 skipped symbols (XAUUSD, XAGUSD, US100, GER40, FRA40)
by writing the correct config/brokers/icmarkets_sc_mt5.json
"""

import json
from pathlib import Path

try:
    import MetaTrader5 as mt5
except ImportError:
    raise SystemExit("MetaTrader5 not installed.")

# Canonical APEX names to search for on IC Markets
SEARCH_TARGETS = {
    "XAUUSD": ["XAUUSD", "GOLD", "XAUUSDm", "XAUUSD."],
    "XAGUSD": ["XAGUSD", "SILVER", "XAGUSDm", "XAGUSD."],
    "US100":  ["US100", "NAS100", "USTEC", "NASDAQ", "US100m", "NAS100m"],
    "GER40":  ["GER40", "DE40", "DAX40", "GER30", "DAX", "GER40m"],
    "FRA40":  ["FRA40", "CAC40", "FRA40m", "CACC.NAS", "CAC40m"],
}

BROKER_DIR = Path("config/brokers")
BROKER_DIR.mkdir(parents=True, exist_ok=True)


def main():
    if not mt5.initialize():
        raise SystemExit(f"MT5 init failed: {mt5.last_error()}")

    info    = mt5.terminal_info()
    account = mt5.account_info()

    broker  = getattr(info, "company", "IC Markets") if info else "IC Markets"
    server  = account.server if account else "unknown"
    login   = account.login if account else 0
    balance = account.balance if account else 0

    print(f"Connected: {login} | {server} | ${balance:.2f}")
    print(f"Broker: {broker}\n")

    # Get all symbols available on this broker
    all_symbols = mt5.symbols_get()
    all_names   = {s.name for s in all_symbols} if all_symbols else set()

    print(f"Total symbols available: {len(all_names)}\n")

    found    = {}
    notfound = []

    for canonical, candidates in SEARCH_TARGETS.items():
        match = None
        for candidate in candidates:
            if candidate in all_names:
                match = candidate
                break

        if match:
            found[canonical] = match
            note = f" (same)" if match == canonical else f" → broker uses: {match}"
            print(f"  ✓ {canonical}{note}")
        else:
            notfound.append(canonical)
            # Search for anything similar
            similar = [n for n in all_names
                       if canonical[:3].upper() in n.upper()
                       or canonical[-3:].upper() in n.upper()][:5]
            print(f"  ✗ {canonical} — not found. Similar: {similar or 'none'}")

    print()

    # Write IC Markets broker config
    slug = server.lower().replace("-", "_").replace(" ", "_").replace(".", "_")
    config_path = BROKER_DIR / f"{slug}.json"

    # Start with existing config if present, else empty
    if config_path.exists():
        with open(config_path) as f:
            cfg = json.load(f)
    else:
        cfg = {
            "name": slug,
            "_note": "Auto-discovered by discover_icmarkets.py",
            "overrides": {}
        }

    # Add found mappings
    for canonical, broker_name in found.items():
        cfg["overrides"][canonical] = broker_name

    with open(config_path, "w") as f:
        json.dump(cfg, f, indent=2)

    print(f"Wrote broker config: {config_path}")
    print(f"Added mappings: {found}")

    if notfound:
        print(f"\nStill not found: {notfound}")
        print("These may need manual lookup in MT5 Market Watch (Ctrl+U)")

    mt5.shutdown()

    print(f"""
Next step — re-run fetch_csv.py to get the missing symbols:
  python fetch_csv.py

The script will now load {config_path.name} and resolve symbols correctly.
""")


if __name__ == "__main__":
    main()
