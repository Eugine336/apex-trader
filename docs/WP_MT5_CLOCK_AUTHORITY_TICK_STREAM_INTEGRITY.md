# WP-T — MT5 Clock Authority and Tick-Stream Integrity

**Status:** IMPLEMENTING — no code acceptance claimed.  
**Priority:** Critical — production market-data integrity.  
**Branch:** `fix/mt5-clock-holdover-tick-integrity`  
**Baseline:** `c3d2f1c1a900a70f6c1baecee77b7c74304e6481`  
**Roadmap:** `docs/APEX_FULL_REDESIGN_ROADMAP.md`  
**Primary system:** System 2. **Dependencies:** Systems 6, 7, 9 and 10.

This document is the execution checklist for the WP-T roadmap entry. The roadmap remains the authoritative architecture and acceptance source.

## Non-negotiable constraints

- CognitiveBrain remains the sole market decision authority.
- Closed-bar reasoning consumes completed bars only.
- Unknown, stale, contradictory, missing or unverified data remains unknown.
- Host UTC, monotonic elapsed time and broker-server time are distinct domains.
- No host OS clock adjustment to compensate for broker offsets.
- No synthetic ticks or invented timestamp precision.
- MT5 ingestion must not depend on Deriv or external AI.
- Broker truth remains authoritative for positions, orders and reconciliation.
- No live broker order placement, modification or cancellation during engineering.
- Do not access or modify the real `.env`, credentials, runtime databases, account selection or production trading configuration.

## Execution checklist

### WP-T1 — Unified time authority
- [ ] Inventory all clock domains and consumers.
- [ ] Define typed clock observations, broker/server identity and witness provenance.
- [ ] Use monotonic time for elapsed time, freshness, retries and latency.
- [ ] Implement independent-witness validation, outlier handling and bounded uncertainty.
- [ ] Implement bounded holdover, uncertainty growth, expiry and fail-closed normalization.
- [ ] Prove no host clock mutation exists.

### WP-T2 — MT5 broker clock lifecycle
- [ ] Trace `_synchronize_server_clock_state` and all callers.
- [ ] Remove repeated-latest-quote polling as the sole synchronization progress mechanism.
- [ ] Distinguish unavailable, stale and non-advancing observations.
- [ ] Implement startup/acquire/synchronized/holdover/expired/retry/recovered transitions.
- [ ] Invalidate cache on broker/server/terminal identity changes.
- [ ] Expose offset, uncertainty, age, evidence, identity and failure reason.
- [ ] Verify restart behavior.

### WP-T3 — Tick timestamp integrity
- [ ] Inventory every pandas/Python datetime conversion on active tick paths.
- [ ] Preserve raw MT5 `time` and `time_msc` with exact precision.
- [ ] Validate field consistency without invented precision.
- [ ] Preserve source, normalized UTC, receipt wall, receipt monotonic and processing timestamps.
- [ ] Reject malformed, contradictory, future-dated and unverifiable records.
- [ ] Prove normalization is applied exactly once.

### WP-T4 — Complete ingestion and gap recovery
- [ ] Trace adapter → poller → router → store → recovery → event bus → consumers.
- [ ] Prove delivery semantics for every eligible tick.
- [ ] Detect duplicates, out-of-order ticks, stalls and gaps.
- [ ] Implement verified, idempotent overlapping history recovery.
- [ ] Persist unrecoverable gaps; never synthesize ticks.
- [ ] Prove MT5 independence from Deriv and AI.

### WP-T5 — Downstream and candle integrity
- [ ] Inventory all timestamp/clock consumers.
- [ ] Verify candle source-time ordering and completed-bar boundaries.
- [ ] Verify MTF alignment, warmup, event ordering and stale-data handling.
- [ ] Propagate data absence/gap separately from no-pattern observations.
- [ ] Expose feed health separately from strategy observations.

### WP-T6 — Restart, multi-host and failure recovery
- [ ] No tick and delayed first tick.
- [ ] Stale persisted clock and restart during gap.
- [ ] NTP unavailable, disagreement and outlier witness.
- [ ] Broker identity change, holdover, expiry and recovery.
- [ ] Multiple hosts with different wall clocks.
- [ ] Overlapping history recovery without duplicate/current-time replay.
- [ ] Broker-truth reconciliation unchanged.

## Acceptance matrix

| ID | Gate | Status |
|---|---|---|
| T-A1 | Explicit clock domains; monotonic elapsed-time decisions | OPEN |
| T-A2 | Witness provenance, uncertainty, disagreement, holdover and expiry | OPEN |
| T-A3 | Identity-bound MT5 clock lifecycle and restart validation | OPEN |
| T-A4 | Lossless timestamp precision and provenance end-to-end | OPEN |
| T-A5 | Complete ingestion, gap/stall/order detection and idempotent recovery | OPEN |
| T-A6 | All consumers use verified data; completed bars only | OPEN |
| T-A7 | Failure, restart and multi-host test matrix | OPEN |
| T-A8 | Obsolete clock/tick paths removed from active production routes | OPEN |
| T-A9 | Roadmap, coverage, delivery ledger and operations docs synchronized | OPEN |
| T-A10 | Safety/Brain/execution invariants preserved; no live broker mutation | OPEN |

## Baseline evidence — 2026-11-11

- Branch existed at baseline commit `c3d2f1c1a900a70f6c1baecee77b7c74304e6481`; this work package had no code changes or test runs on the branch at start.
- `docs/APEX_MT5_DATA_CAPABILITY_CONTRACT.md` explicitly states that historical MT5 tick range fetch exists but replay ingestion and Brain attribution remain incomplete.
- Relevant paths present at baseline: `platforms/mt5/clock.py`, `platforms/mt5/mt5_connector.py`, `tick/tick_sources.py`, `tick/tick_router.py`, `tick/tick_store.py`, `tick/live_candle_aggregator.py`, `scripts/diagnose_mt5_clock.py`, `scripts/mt5_clock_publisher.mq5`.
- Prior implementation reports monotonic anchoring, uncertainty, identity binding and fail-closed validation. These are audit targets, not assumed acceptance.
- Prior diagnostic evidence described intermittent stale/non-advancing observations and a 10-second wait. This does not prove sustained feed health.
- No tests, production integration, owner-host verification or acceptance are claimed in this baseline record.

## Change and evidence log

| Date | Change | Evidence | Status |
|---|---|---|---|
| 2026-11-11 | Added WP-T scope and gates to authoritative redesign roadmap | Commit `4773ae23d0974fbc9578eab6003f1354f87b0a6b` | Recorded |


## Source trace findings — 2026-11-11 (not yet runtime-accepted)

### Confirmed in source
1. `MT5Connector._synchronize_server_clock_state` at `platforms/mt5/mt5_connector.py` uses `time.monotonic()` for cache age and retry timing, and requires a second strictly advancing `symbol_info_tick` observation. The cache is reused up to its age limit, but the state uncertainty remains constant during holdover. A stale cached tick can be used to seed the first observation; the production estimator does not independently verify that the tick's source timestamp is current before anchoring the new clock state.
2. The broker clock cache and per-symbol advancing-tick observations are process memory only. `connect()` and `disconnect()` clear them. There is no restart-persisted verified clock state or durable tick-history cursor in the poller.
3. `MT5Connector.get_price` subtracts the estimated offset from the provider timestamp before constructing the UTC `datetime`. It preserves raw `time` and `time_msc` in `raw_fields`.
4. `MT5Connector.get_ticks` sends UTC-aware `from_dt`/`to_dt` to `copy_ticks_range`, then subtracts the estimated broker offset from returned `time` and `time_msc` when a live clock is available.
5. `MT5TickPoller._route_history_quotes` rejects returned rows outside the UTC query interval (±1 ms). If the official MT5 Python UTC timestamp contract applies to returned ticks, the offset subtraction above can shift every history row outside the requested interval. This would leave the latest-quote fallback as the only routed tick. This is a high-priority test hypothesis; confirm with raw terminal evidence before changing semantics.
6. The official MetaQuotes Python docs say returned tick and bar timestamps are UTC: `https://www.mql5.com/en/docs/python_metatrader5/mt5copyticksrange_py` and `https://www.mql5.com/en/docs/python_metatrader5/mt5copyticksfrom_py`. The prior Apex docs' blanket server-time assumption conflicts with this provider documentation. Both `APEX_MT5_DATA_CAPABILITY_CONTRACT.md` and `MT5_CLOCK_BRIDGE.md` now record the discrepancy and the required proof.
7. Historical replay is capped at a 300-second window. When the cursor gap exceeds 300 seconds, the poller increments an in-memory gap counter, jumps the cursor to the current quote and resumes. It deliberately does not recover the longer gap because it lacks a historical offset series; this is an explicit unrecovered data gap, not complete recovery.
8. `_history_cursor`, `_history_seen` and `_history_gap_count` are in-memory only. Their loss on restart can cause overlapping history replay and duplicates beyond the bounded per-process fingerprint cache.
9. `TickRouter` publishes raw and verified tick events and invokes raw callbacks before `TickStore` coalescing. `LiveCandleAggregator` subscribes to raw callbacks. `TickStore` intentionally rate-coalesces its bounded working set; it is not a complete durable tick tape.
10. The current `Tick` model stores an aware Python `datetime` and raw fields. MT5's source `time_msc` is millisecond precision, so Python datetime's microsecond resolution can represent that source precision. The critical risk is the offset/normalization and conversion lineage, not an unsupported claim that all MT5 millisecond precision is lost by `datetime` alone.
11. The live candle aggregator ignores unverified ticks and uses tick timestamps for candle buckets. It closes only the forming candle when a new tick crosses a boundary; it does not fabricate intermediate candles over gaps. The close detector/watchdog and aggregator lifecycle still require complete review for data-gap and stale-source behavior.

### Immediate implementation order
1. Resolve raw MT5 API timestamp semantics and historical query-bound behavior first. Add a failing regression test for non-zero offset with UTC source epochs; prove the correct behavior from official docs and owner-host captures.
2. Correct the live and historical normalization contract together; do not patch only `get_ticks` while leaving `get_price` on a contradictory time basis.
3. Add monotonic-age/uncertainty growth to clock holdover and reject a stale first tick as synchronization evidence.
4. Persist restart-safe replay cursor and gap ledger using the repository's established durable store; do not invent a second event authority.
5. Replace the 300-second silent re-anchor behavior with explicit recoverable range batching where the provider can verify history; retain durable unrecoverable gap records otherwise.
6. Add feed-health metrics for per-symbol last source tick, last receipt monotonic, poll errors, advancing observations, history rows requested/returned/accepted/rejected, duplicate/out-of-order counts, cursor lag and unrecovered gaps.
7. Verify every downstream consumer and preserve Brain/closed-bar authority.

### Execution limitation in this session
The available GitHub connector can inspect and commit repository files but does not provide a local checkout or command runner. No source code has been changed and no tests have been executed in this pass. Source changes must not be marked validated without an actual supported-runtime test run.
