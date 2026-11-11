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


## WP-T timestamp-semantics qualification — 2026-11-11

This document's earlier statement that broker timestamps are server-time labels is not yet accepted as a universal MT5 Python contract. MetaQuotes' official Python documentation for `copy_ticks_range` and `copy_ticks_from` states that returned tick and bar timestamps are UTC. The current Apex implementation applies a measured broker offset to live and historical tick epochs. That assumption must be reconciled against raw owner-terminal observations and the official API contract before further normalization changes.

In particular, do not infer that the broker's displayed chart/server clock timezone is the timezone basis of the Python API's `time` and `time_msc` fields. Keep the raw epoch, API surface/version, broker identity, receipt wall time, receipt monotonic instant, independent UTC witness and any derived offset separately recorded. The offset estimate is not itself proof that the provider timestamp requires offset subtraction.

See `docs/APEX_MT5_DATA_CAPABILITY_CONTRACT.md` and WP-T in `docs/APEX_FULL_REDESIGN_ROADMAP.md` for the required multi-host and historical-range verification. Until the discrepancy is resolved, affected source-time normalization remains DATA-GATED; do not assert production acceptance.
