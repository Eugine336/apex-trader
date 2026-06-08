# Live Deriv SL/TP Integration Check — Runbook

## Purpose

Validates that the **C1 fix** (`modify_order` in `DerivConnector`) computes
correct dollar SL/TP values end-to-end against a real Deriv broker connection.

Unit tests (`tests/test_live_execution_safety.py`) prove the formula math in
isolation.  This script proves the formula is what the broker actually receives
and accepts — closing the integration gap.

## Prerequisites

| Requirement          | Details                                              |
|----------------------|------------------------------------------------------|
| Python 3.10+         | Same as the APEX runtime                             |
| Deriv demo account   | Create at <https://app.deriv.com/> → Demo            |
| Deriv API token      | <https://app.deriv.com/account/api-token> (scope: **Trade, Read**) |
| Deriv App ID         | <https://api.deriv.com/app-manager/applications>     |
| Dependencies         | `pip install websockets loguru pandas`                |

## Environment Variables

Set these **before** running the script.  Never hardcode them.

```powershell
# PowerShell (Windows)
$env:DERIV_APP_ID = "<your_app_id>"
$env:DERIV_API_TOKEN = "<your_demo_api_token>"
$env:DERIV_ACCOUNT_TYPE = "demo"     # default — omit or set explicitly
```

```bash
# bash / Linux / macOS
export DERIV_APP_ID="<your_app_id>"
export DERIV_API_TOKEN="<your_demo_api_token>"
export DERIV_ACCOUNT_TYPE="demo"
```

## Running (PowerShell — step by step)

Run from the **repo root** (`apex-trader/`):

```powershell
# 1. Activate your venv
.\.venv\Scripts\Activate.ps1

# 2. Set credentials (if not already set)
$env:DERIV_APP_ID = "<your_app_id>"
$env:DERIV_API_TOKEN = "<your_demo_api_token>"

# 3. Run with the required safety flag
python scripts/live_deriv_sltp_check.py --i-understand-this-places-a-real-order
```

### Optional flags

| Flag                 | Default     | Description                                      |
|----------------------|-------------|--------------------------------------------------|
| `--symbol`           | `1HZ100V`  | Deriv symbol (24/7 synthetic, high liquidity)    |
| `--stake`            | `1.0`      | USD stake (minimum = $1)                         |
| `--direction`        | `BUY`      | `BUY` or `SELL`                                  |
| `--sl-offset-pct`    | `0.5`      | Initial SL distance as % of price                |
| `--tp-offset-pct`    | `1.0`      | Initial TP distance as % of price                |
| `--tolerance`        | `0.05`     | Max acceptable delta in USD between values        |
| `--allow-real`       | (off)      | Required if `DERIV_ACCOUNT_TYPE` is not `demo`   |

## What It Does

1. Connects to Deriv via the **real** `DerivConnector` class (not a mock).
2. Opens a minimal position (`$1 stake`, configurable).
3. Computes a new SL value and independently calculates the expected dollar amount:
   `expected = round(abs(open_price - new_sl) / open_price * stake * multiplier, 2)`.
4. Calls the **production** `modify_order()` code path.
5. Captures the exact payload sent to Deriv (the dollar SL value the connector computed).
6. Reads back from the broker via `proposal_open_contract` to get what Deriv actually recorded.
7. Compares all three values:

   | Value                  | Source                                          |
   |------------------------|-------------------------------------------------|
   | **Expected**           | Independently computed by the script            |
   | **Connector sent**     | What `modify_order` actually transmitted         |
   | **Broker acknowledged**| What Deriv reports back after the modification   |

8. **ALWAYS** closes the test position in a `finally` block, even on failure or Ctrl+C.

## Expected Output (PASS)

```
[STEP 1] Connected ✓
[STEP 4] Position opened ✓ contract_id=12345 fill_price=1000.50
[STEP 9] modify_order returned True ✓
============================================================
  COMPARISON RESULTS
============================================================
  Expected (independent) : $5.00
  Connector sent         : $5.0
  Old formula (broken)   : $0.50
  Broker acknowledged    : $5.00
------------------------------------------------------------
  CHECK 1 (connector == expected) : PASS ✓ (delta=0.0000, tol=0.05)
  CHECK 2 (not old formula)       : PASS ✓ (sent=5.0 vs old=0.5)
  CHECK 3 (broker == expected)    : PASS ✓ (delta=0.0000, tol=0.05)
============================================================
  ██  OVERALL: PASS  ██
============================================================
[CLEANUP] Position closed ✓ PnL=-0.02
```

Exit code `0` = PASS, `1` = FAIL.

## SAFETY

- **Demo first.**  Always run on a demo account before touching real money.
  The script **refuses** to run against a non-demo account unless you also
  pass `--allow-real`.
- **Explicit consent.**  The `--i-understand-this-places-a-real-order` flag
  must be passed or the script exits immediately.
- **Guaranteed cleanup.**  The position is closed in a `finally` block that
  catches exceptions and `KeyboardInterrupt`.  If cleanup fails, the script
  prints the `contract_id` and tells you to close it manually in the Deriv
  dashboard.
- **Minimal risk.**  Default stake is `$1.00` (the Deriv minimum).  The
  position is held for seconds — long enough to modify and read back,
  then closed immediately.

## Limitations

- **Broker readback field.**  The script reads `limit_order.stop_loss.order_amount`
  from `proposal_open_contract`.  If Deriv changes its API response shape, CHECK 3
  may report `UNAVAILABLE`.  CHECKs 1 and 2 are still valid in that case.
- **No MetaTrader5.**  This validates the Deriv path only.  MT5 exit attribution
  uses `history_deals_get()` which is a different code path (`mt5_connector.py`).
- **Single modify.**  The script tests one SL modification.  For full coverage of
  trailing/breakeven/tighten scenarios, extend the script or run it multiple times
  with different `--sl-offset-pct` values.
