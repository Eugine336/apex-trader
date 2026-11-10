# MT5 automatic broker-clock synchronization

## Production design

Apex no longer requires a chart-attached Expert Advisor or a terminal-local
clock publisher file. The production connector estimates the broker timestamp
basis automatically from:

- A live MT5 tick whose timestamp has advanced consistently with monotonic
  elapsed time.
- Independent network UTC observations from multiple NTP servers.
- Authenticated HTTPS Date-header observations when outbound UDP/123 is blocked.

The estimate is associated with the exact MT5 account server. It is accepted
only when at least two independent time sources agree, the inferred offset is
within the plausible UTC-offset range, and estimated uncertainty remains below
the configured safety ceiling. The resulting clock is anchored to monotonic
time, not the workstation wall clock, and is refreshed while the tick stream
advances.

## Runtime guarantees

- No fixed GMT+2/GMT+3 offset is configured or assumed.
- No workstation local-time value is substituted for broker time.
- Broker timestamps remain the source of market-data ordering.
- Tick freshness and completed-bar checks use the synchronized broker clock.
- The clock is refreshed after reconnects and can detect a verified offset
  change, including seasonal broker-time changes.
- If live ticks do not advance, network time sources are unavailable, sources
  disagree, or uncertainty is excessive, the clock remains unavailable and
  market data requiring verified time plus new exposure fail closed.
- Existing-position management and observation remain subject to their
  independent data and safety requirements; clock failure never authorizes a
  trade.

## Important limitation

The MetaTrader5 Python API does not expose the terminal's TimeTradeServer()
function. Tick timestamps plus independent UTC observations provide an
automatic estimate, but the estimate's uncertainty includes network delay and
tick timestamp precision. This is why Apex requires multiple agreeing time
sources and an advancing tick stream. It must not claim a verified clock when
those conditions are not met. If the broker changes timestamp semantics or
network timing becomes ambiguous, the system fails closed rather than guessing.

The MQL5 publisher in `scripts/mt5_clock_publisher.mq5` is retained only as an
optional diagnostic/reference utility. It is not part of production startup
and does not need to be compiled or attached.

## Diagnostic

Run the existing read-only diagnostic:

```powershell
python scripts/diagnose_mt5_clock.py --symbols XAUUSD.ecn EURUSD.ecn
```

For runtime diagnosis, inspect the MT5 connector log for
`broker server clock synchronized`, the selected time sources, inferred
offset, uncertainty, and any explicit synchronization failure.
