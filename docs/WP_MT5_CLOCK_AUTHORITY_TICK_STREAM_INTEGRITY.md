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
