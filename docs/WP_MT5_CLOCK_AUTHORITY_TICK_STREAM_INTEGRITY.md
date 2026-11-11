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


## Follow-on implementation — holdover and diagnostic capture (2026-11-11)

- Implemented immutable clock holdover views in `platforms/mt5/clock.py`. Holdover age is measured from monotonic cache age; uncertainty grows at the explicit conservative bound `CLOCK_HOLDOVER_DRIFT_PPM = 1,000` (1 ms/s). The verified offset and original monotonic anchor are preserved. Invalid/negative ages raise.
- Rewired `MT5Connector._synchronize_server_clock_state` cache returns to expose `synchronization_status="holdover"` and growing uncertainty. Negative monotonic cache age invalidates the cached state. The existing maximum cache age remains the expiry boundary.
- Added regression tests in `tests/test_mt5_server_clock.py` for uncertainty growth, anchor preservation and invalid ages. Tests are committed but not executed.
- Added `scripts/diagnose_mt5_timestamp_semantics.py`, a read-only terminal capture. It uses independent UTC witnesses (deduplicated by authority) for exact history query bounds, captures host wall and monotonic readings, raw latest tick and last historical rows, API/package/terminal version and broker-server identity. It does not access credentials, alter host clock/settings or submit orders.
- Added `tests/test_mt5_timestamp_diagnostic.py` static read-only safety checks. Tests are committed but not executed.
- Timestamp research was qualified: official MetaQuotes Python docs describe history data as UTC, while official snapshot docs omit explicit timezone semantics and forum reports conflict. No global UTC/server-local rewrite has been made. The raw capture is required before altering active normalization.
- No test, lint, import, integration or runtime execution has occurred in this environment. Source implementation is committed; verification remains open.


## Current execution ledger — 2026-11-11

| Item | Current state | Evidence |
|---|---|---|
| Holdover uncertainty growth | Implemented, not test-executed | `platforms/mt5/clock.py` |
| Connector holdover wiring | Implemented, not test-executed | `platforms/mt5/mt5_connector.py` |
| Broker identity revalidation | Implemented, not test-executed | Connector refuses holdover if account server identity cannot be read or differs |
| Holdover regression tests | Added, not executed | `tests/test_mt5_server_clock.py` |
| Raw timestamp capture | Implemented, not owner-host executed | `scripts/diagnose_mt5_timestamp_semantics.py` |
| Diagnostic safety test | Added, not executed | `tests/test_mt5_timestamp_diagnostic.py` |
| Timestamp semantics decision | OPEN / DATA-GATED | Official documentation and public field reports conflict; preserve raw evidence until host capture |
| Historical gap persistence | OPEN | Cursor, dedup and gap counters remain process-local |
| Full consumer audit | OPEN | Current inventory is partial |
| Python 3.11/3.12 CI | OPEN | No CI result yet |
| Live broker mutation | PROHIBITED / NONE | Diagnostic is read-only |

### Diagnostic execution

On each supported Windows host with the MT5 terminal already initialized and logged into the intended account, run:

```powershell
python scripts/diagnose_mt5_timestamp_semantics.py --symbol XAUUSD --window-seconds 60
```

Use the exact broker-native symbol if it differs from XAUUSD. Save the JSON output from each host as an artifact and sanitize account-sensitive metadata before sharing. Do not include login IDs, passwords, terminal paths containing usernames or unrelated account data. Capture at least two hosts with different local timezone/wall-clock configurations. Repeat around a broker DST transition where feasible.

The script requires at least two independent UTC authorities to establish query bounds. It records host wall time only for comparison; it does not use host wall time to establish the UTC request interval. A missing/empty history result is evidence to investigate, not proof by itself of timestamp convention.

### Exact next action

Open a draft PR to main to trigger CI. Review all checks before source normalization changes. Run the read-only capture on two owner hosts, compare the raw `time`/`time_msc` values and history rows against UTC witnesses and exact UTC request bounds, then settle the live and historical timestamp contract together. Continue durable tick history recovery, gap ledger persistence and complete downstream consumer audit. No acceptance gate may be marked complete until tests and owner-host evidence exist.


## Owner-host diagnostic evidence — 2026-11-11

Two captures from the same Windows 11 host and JustMarkets-Demo terminal completed successfully after the direct-launch import fix. Both reported `symbol_info_tick("XAUUSD")` with null `time`/`time_msc` and a 60-second `copy_ticks_range` result with zero rows. The second capture repeated the same result at 15:51 local time. Terminal connectivity was true in both captures. These observations establish a repeatable empty-data symptom on this host, but do not prove that the broker has no ticks or settle timestamp semantics: the prior diagnostic omitted symbol metadata, Market Watch visibility/selection and quote fields from `symbol_info`.

The captures also showed host wall time approximately 0.7 seconds behind the UTC witness projection. Witness uncertainty was approximately 0.56–0.59 seconds, so this is not sufficient evidence for a sub-second clock correction. No host clock changes are authorized.

### Follow-up diagnostic increment

The read-only diagnostic now records a minimal non-sensitive `symbol_info` snapshot (`visible`, `select`, Bid/Ask/Last, source time/time_msc, spread and instrument precision), the symbol-info and tick API error tuples, and whether history is unavailable, empty or populated. If the exact symbol cannot be resolved, it performs a read-only wildcard `symbols_get` lookup and returns only matching symbol names. It deliberately does not call `symbol_select`; it does not change Market Watch or terminal settings. Regression tests cover field selection, candidate filtering and the no-selection/no-order safety boundary.

### Current evidence status

- Direct diagnostic launch: owner-host execution succeeded.
- Symbol/feed cause: unresolved pending the expanded symbol diagnostics.
- Timestamp semantics: DATA-GATED; no normalization change is justified by empty history.
- Multi-host comparison: not performed; a second independent host capture is still required.
- CI for the expanded symbol diagnostic: pending.
- All T-A1 through T-A10 acceptance gates remain OPEN.


## Owner-host diagnostic evidence — broker-native symbol and timestamp domains (2026-11-11)

A third capture on the same Windows 11 / JustMarkets-Demo terminal was run against the discovered broker-native symbol `XAUUSD.ecn`. It resolves successfully and proves that the earlier empty-feed captures were caused by querying the non-existent exact symbol `XAUUSD` in the diagnostic, not by a terminal-wide absence of market data.

Observed facts:
- `symbol_info("XAUUSD.ecn")` returned metadata; `visible=true`, `select=true`, `trade_mode=4`, Bid 4049.70 / Ask 4049.79.
- `symbol_info_tick("XAUUSD.ecn")` returned Bid 4049.70 / Ask 4049.79, `time=1794404576`, `time_msc=1794404576984`.
- `copy_ticks_range` for UTC `11:41:57.117621` through `11:42:57.117621` returned 203 rows with API result `[1, "Success"]`. The last returned raw tick had `time=1794397376`, `time_msc=1794397376835`, which lies inside the requested UTC interval.
- The latest quote's raw epoch is approximately 7,200 seconds ahead of the capture's UTC witness projection; history row epochs align with the UTC request interval. This is direct single-host evidence that the two API surfaces may expose different timestamp domains (latest quote: broker-server wall clock; history range: UTC epoch). It is not yet a fleet-wide contract.
- Host wall epoch was approximately 1794397376.52; UTC witnesses projected approximately 1794397377.1 with ~0.55–0.59 second uncertainty. No host-clock correction is supported.
- Safety flags confirm no credentials read, host clock modification, terminal setting modification or orders submitted.

### Engineering implication — do not normalize by assumption

The current `MT5Connector.get_ticks` implementation documents range-returned epochs as broker-server time and subtracts the live broker offset for recent ranges. If this capture's history epoch semantics are confirmed, that subtraction would incorrectly move already-UTC history rows by approximately two hours. Do not change production normalization based on one host capture; add a regression fixture that preserves raw fields and compares both API surfaces against independent UTC witnesses, then obtain repeat captures and a second independent host. Until then, source-time-dependent consumers must retain raw epochs and unresolved provenance rather than silently asserting one shared timestamp domain.

### Symbol resolution implication

The connector's existing `_COMMON_SUFFIXES` includes `.ecn`; its `symbol_map("XAUUSD")` path should resolve the broker-native `XAUUSD.ecn` when connected and the symbol metadata is available. The diagnostic intentionally queried exact names and does not invoke connector mapping. Verify the live Apex call graph actually passes the canonical `XAUUSD` through `MT5Connector.symbol_map`, and that no caller bypasses the mapped symbol for tick/history requests. Do not rename the canonical instrument or apply a global suffix rewrite.

### Evidence status

- Broker-native symbol and market data: OBSERVED on one owner host.
- Connector suffix resolution: IMPLEMENTED in source; runtime integration not yet proven by this capture.
- Latest-quote vs history timestamp domains: STRONG SINGLE-HOST EVIDENCE; DATA-GATED pending repeat and second-host comparison.
- Clock offset and global normalization contract: OPEN.
- All T-A1 through T-A10 acceptance gates remain OPEN.

## Timestamp contract and recovery implementation — 2026-11-11

### Owner-host evidence

Two captures from the same Windows 11 host, MT5 package 5.0.6231 / terminal 500.6308, broker server `JustMarkets-Demo`, symbol `XAUUSD.ecn`, returned populated `copy_ticks_range` results. In both captures:

- Raw `symbol_info_tick().time` and `time_msc` were approximately 7,200 seconds ahead of the independent UTC witnesses.
- Raw `copy_ticks_range` rows were within the exact UTC request bounds and their seconds/milliseconds fields agreed.
- The host wall clock was within the independent witness uncertainty; no host clock adjustment is supported.
- The unsuffixed `XAUUSD` did not resolve; `XAUUSD.ecn` did. The connector's existing suffix resolver includes `.ecn`.

This establishes a repeatable API timestamp-domain difference for this broker/terminal/host. It does not establish a fleet-wide contract; a second independent MT5 host is still required.

### Implemented correction on `fix/wp-t-timestamp-contract-recovery`

- `MT5Connector.get_price` retains live-quote normalization through the identity-bound broker clock and now carries the broker server identity in tick provenance.
- `MT5Connector.get_ticks` no longer subtracts the live broker offset from `copy_ticks_range` rows. It preserves raw provider seconds/milliseconds and attests a batch as UTC only when both fields are finite, mutually consistent and inside the exact UTC request bounds. Uncorroborated data remains timezone-unknown and is not routed as verified UTC.
- The poller now requires the explicit `utc_request_bounds_verified` attestation instead of the previous `live_server_offset` label.
- Long recovery windows are split into independently validated 300-second batches. A failed, empty or unverified batch leaves the remaining range outstanding; the old jump-to-current-quote behavior is removed.
- Replay checkpoints use the existing canonical EventStore with synchronous durable writes, broker-server identity binding, UTC cursor and a bounded recent fingerprint overlap. A failed durable checkpoint prevents cursor advancement. No new database or per-tick generic event logging was introduced.
- Live candle warmup now rejects any frame without `completed_bars_verified=True`. The current broker warmup adapter does not yet provide this provider attestation, so warmup safely skips rather than classifying a potentially forming bar as closed. This is an explicit remaining integration/data-contract task, not accepted warmup functionality.
- Added focused tests for UTC-range corroboration, offset-shifted and inconsistent history, multi-batch recovery, failure without cursor advancement, durable checkpoint restore, broker-identity mismatch and unattested warmup rejection.

### Verification status

- These source and test changes are committed on the dedicated branch.
- No local pytest, lint, compile or integration command was executed in this environment. New CI is required.
- PR #907 diagnostic CI run `43014426036` is green for Python 3.11, Python 3.12 and lint; that run does not include this correction branch.
- Only one owner host has been captured (twice). Multi-host and sustained feed acceptance remain open.
- No broker orders or other broker mutations were performed.

### Remaining WP-T work

1. Run the new focused tests and complete CI; fix all failures without weakening tests.
2. Add durable checkpoint failure/restart integration tests against the real EventStore implementation and verify database-degraded behavior.
3. Complete the provider-attested completed-bar contract for MT5 OHLCV; do not turn the current warmup fail-closed guard into an inferred timestamp heuristic.
4. Complete System 2/6/7 consumer inventory, gap propagation and stale-data behavior; verify no Brain decision consumes raw or forming data.
5. Finish clock lifecycle tests for stale first tick, no tick, NTP disagreement, identity change, restart and holdover expiry.
6. Obtain a second independent MT5 host capture and sustained owner-host feed evidence.
7. Keep T-A1 through T-A10 OPEN until the full evidence matrix passes.


### Recovery hardening follow-up — 2026-11-11

- Durable checkpoint writes are rate-limited to at most one per five seconds per symbol; each checkpoint retains the most recent 256 fingerprints. This bounds EventStore write volume while keeping restart replay overlap bounded. The cursor in memory may advance between durable checkpoints; restart resumes from the last committed cursor and replays the intervening range.
- Empty history responses, invalid timestamp attestations, fetch failures, malformed timestamp batches and cursor-ahead conditions are retained as durable `MT5_TICK_RECOVERY_GAP` events when the canonical EventStore is available. The cursor is not advanced across these ranges.
- Added a real temporary-SQLite EventStore close/reopen test for checkpoint restoration, plus tests for write failure, checkpoint write throttling and durable gap deduplication. These tests are committed but not executed.
- The 300-second batch tests verify that a failed later batch leaves the cursor at the last successful batch rather than jumping to the requested end.
- The test suite still needs CI execution. Same-host timestamp evidence remains repeated but not multi-host.


## Completed-bar and timestamp contract supersession — 2026-11-11

The earlier implementation note that aggregator warmup remained unavailable is superseded by the completed-bar correction:

- `get_ohlcv(include_forming=False)` now requests MT5 `copy_rates_from_pos` with `start_pos=1`; position zero is the forming bar, position one is the last completed bar.
- `get_ohlcv_range` validates UTC provider epochs against the exact requested interval and, for closed-only reads, filters using a conservative independent UTC lower bound.
- The canonical event-driven `_fetch_candles` path now calls the completed-only fetcher. The live candle aggregator warmup uses the same provider-attested completed-bar contract.
- Cross-asset context requires `completed_bars_verified=True` and `forming_bar_included=False`; it no longer drops a guessed final row.
- Forming-bar WorldModel publications are no longer wired as cognition wake triggers. Brain analysis is fed completed, provenance-bearing bars; verified tick-change wakes remain independent.
- The separate developing observer remains isolated to the existing position-management advisory; it is not admitted as Brain evidence or a Brain wake source.

This does not close the remaining full consumer audit, clock lifecycle, multi-host, EventStore failure, or integrated System 2/6/7 gates.


## Completed-bar and timestamp contract supersession — 2026-11-11

The previous implementation note that aggregator warmup remained unavailable is superseded by the completed-bar correction:

- `get_ohlcv(include_forming=False)` now requests MT5 `copy_rates_from_pos` with `start_pos=1`; position zero is the forming bar, position one is the last completed bar.
- `get_ohlcv_range` validates UTC provider epochs against the exact requested interval and, for closed-only reads, filters using a conservative independent UTC lower bound.
- The canonical event-driven `_fetch_candles` path now calls the completed-only fetcher. The live candle aggregator warmup uses the same provider-attested completed-bar contract.
- Cross-asset context requires `completed_bars_verified=True` and `forming_bar_included=False`; it no longer drops a guessed final row.
- Forming-bar WorldModel publications are no longer wired as cognition wake triggers. Brain analysis is fed completed, provenance-bearing bars; verified tick-change wakes remain independent.
- The separate developing observer remains isolated to the existing position-management advisory; it is not admitted as Brain evidence or a Brain wake source.

This does not close the remaining full consumer audit, clock lifecycle, multi-host, EventStore failure, or integrated System 2/6/7 gates.
