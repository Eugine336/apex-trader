# Apex Trader — Engineering Progress Ledger

This is the durable session-to-session engineering ledger. The authoritative architecture remains `docs/APEX_FULL_REDESIGN_ROADMAP.md`; this file records exact repository state and evidence.

## 2026-11-11 — WP-T MT5 clock and tick-stream integrity

- **Repository:** `Eugine336/apex-trader`
- **Branch:** `fix/mt5-clock-holdover-tick-integrity`
- **Base / initial HEAD:** `c3d2f1c1a900a70f6c1baecee77b7c74304e6481`
- **Current recorded HEAD:** `4773ae23d0974fbc9578eab6003f1354f87b0a6b`
- **PR:** none created for WP-T yet.
- **Working-tree state:** not observable through GitHub contents API; do not infer clean.
- **Protected paths:** real `.env`, credentials, runtime databases, broker account selection/settings are off-limits.
- **Live broker mutation:** prohibited during this work.

### Completed and verified
1. Read repository `AGENTS.md` and `docs/APEX_ENGINEERING_DELIVERY_CONTRACT.md`.
2. Read authoritative `docs/APEX_FULL_REDESIGN_ROADMAP.md` and `docs/APEX_MT5_DATA_CAPABILITY_CONTRACT.md`.
3. Recovered the dedicated branch and verified it initially pointed to `c3d2f1c1a900a70f6c1baecee77b7c74304e6481`.
4. Recorded WP-T1 through WP-T6 and ten acceptance gates in the roadmap.
5. Recorded the same execution checklist in `docs/WP_MT5_CLOCK_AUTHORITY_TICK_STREAM_INTEGRITY.md`.

### Changes in this session
- `docs/APEX_FULL_REDESIGN_ROADMAP.md`: appended the critical WP-T work package, dependency mapping, execution sequence, acceptance matrix and verified baseline.
- `docs/WP_MT5_CLOCK_AUTHORITY_TICK_STREAM_INTEGRITY.md`: added the durable package checklist and evidence log.
- This ledger: created to retain exact continuation state.

### Test evidence
- No code changed in this initial planning increment.
- No tests or lint were run.
- No production integration or owner-host verification is claimed.
- No live broker orders were sent.

### Active work
- Trace and audit clock/tick production call graph from MT5 adapter through poller, normalization, router, store, history recovery, event bus, candle aggregation and all downstream consumers.
- Reuse existing verified clock work; do not restart completed investigations or duplicate abstractions.
- Inspect and remove lossy timestamp conversion only after proving the exact active call paths.
- Integrate and test the full lifecycle and recovery matrix.

### Blockers / limitations
- GitHub connector access can inspect and update tracked source files but cannot establish a local worktree state or run repository commands/tests in the current execution environment.
- Sustained broker/terminal behavior and multi-host acceptance require owner-host runtime evidence; synthetic tests cannot replace it.
- Existing concurrent PRs #903 and #904 are open draft PRs against the same baseline. Do not merge or rewrite them as part of WP-T without reviewing their exact diffs and dependency impact.

### Exact next action
Fetch and inspect, on `fix/mt5-clock-holdover-tick-integrity`, the full source of:
`platforms/mt5/clock.py`, `platforms/mt5/mt5_connector.py`, `tick/models.py`, `tick/tick_sources.py`, `tick/tick_router.py`, `tick/tick_store.py`, `tick/live_candle_aggregator.py`, `tick/event_bus.py`, and relevant clock/tick tests. Build a caller/consumer map, identify the active lossy conversions and synchronization lifecycle defects, then implement the first complete production-path correction with regression tests. Do not claim tests were run unless an actual supported execution environment is available.


### Source trace — findings and decisions (2026-11-11)
- Inspected platforms/mt5/clock.py, relevant ranges in platforms/mt5/mt5_connector.py, tick/models.py, tick/tick_sources.py, tick/tick_router.py, tick/tick_store.py, tick/live_candle_aggregator.py, tick/candle_close_detector.py, cognition/concepts/tick_microstructure_readings.py, and clock/tick tests.
- **High-priority timestamp contract conflict:** MetaQuotes' official Python docs state that returned tick/bar times are UTC. Existing Apex code treats tick epochs as broker-server labels and subtracts a measured offset in both get_price and get_ticks. The history poller passes UTC bounds and rejects rows outside those bounds; if returned epochs are UTC, offset subtraction shifts them out of range. This can explain a history-replay path that routes only latest-quote fallback. This remains a source-derived hypothesis pending actual owner-terminal raw samples and package/version evidence. Relevant docs: https://www.mql5.com/en/docs/python_metatrader5/mt5copyticksrange_py and https://www.mql5.com/en/docs/python_metatrader5/mt5copyticksfrom_py.
- **Clock holdover:** cached state uncertainty is constant while cache age grows; first advancing tick can be used to establish a clock without a separate source-age proof.
- **Restart recovery:** clock state, tick replay cursor, bounded dedup fingerprints and gap counters are process-local and reset on connect/restart.
- **Gap recovery:** history replay is limited to 300 seconds. A longer gap causes cursor re-anchor and explicit log/counter, not recovery. The gap record is not durable.
- **Tick delivery:** router emits raw/verified events and raw callbacks before coalescing; TickStore is a deliberately sampled working set, not a durable tape. Live candle aggregation is on raw callbacks.
- **Precision correction:** MT5 time_msc is millisecond source precision; Python datetime microsecond resolution alone does not lose that precision. Keep raw integer epochs and avoid unsupported claims of nanosecond provider precision.
- Updated docs/APEX_MT5_DATA_CAPABILITY_CONTRACT.md and docs/MT5_CLOCK_BRIDGE.md to record the official-doc discrepancy and require multi-host owner-host evidence before choosing normalization semantics.
- Updated docs/WP_MT5_CLOCK_AUTHORITY_TICK_STREAM_INTEGRITY.md with detailed source trace findings and implementation order.

### Current status
- **Plan:** recorded.
- **Source audit:** partial; initial high-risk call path traced, full consumer/caller inventory remains open.
- **Implementation:** not yet changed.
- **Tests:** not run.
- **Production integration:** not verified.
- **Owner-host acceptance:** not performed.
- **Live broker mutation:** none; prohibited.
- **Current branch HEAD:** see latest branch ref; all documentation changes above are committed to this branch. Do not assume PR exists.

### Exact next action
Resolve the timestamp semantics conflict with a focused executable test suite and owner-host raw sample: capture symbol_info_tick (time, time_msc), copy_ticks_range raw epochs and query bounds, API/package/terminal versions, account server identity, independent UTC witness and monotonic receipt times on at least two hosts. Then correct get_price and get_ticks as one coherent contract, add regression tests for non-zero offset and history-window inclusion, and execute focused clock/market-data/poller/candle/Brain tests in Python 3.11 and 3.12. Do not use offset-adjusted timestamps as verified UTC until this gate passes.


### WP-T implementation increment — 2026-11-11
- **Current HEAD at update:** 049bf85e7770e8136f16d1a6c4457af88101fcab.
- Added bounded holdover state to platforms/mt5/clock.py. The original verified offset and monotonic anchor remain unchanged; uncertainty grows at the explicit 1,000 ppm holdover bound (1 ms/s). The state now identifies holdover status and age.
- Rewired MT5Connector cache returns to emit the holdover view, invalidate negative monotonic age, and require the currently connected broker server identity to be readable and match before reusing holdover. If identity is unavailable, cached state is not trusted.
- Added tests for holdover uncertainty growth, anchor preservation, invalid ages, identity revalidation and fail-closed identity absence. These tests are committed but have not been run.
- Added a read-only host diagnostic at scripts/diagnose_mt5_timestamp_semantics.py. It uses at least two independent UTC authorities to form query bounds, captures raw live and historical MT5 fields, host wall/monotonic readings, package/terminal versions and broker server identity. It does not place orders, modify the OS clock, alter terminal settings or read credentials.
- Added static safety test at tests/test_mt5_timestamp_diagnostic.py. Tests are committed but have not been run.
- Qualified earlier timestamp claims in the MT5 capability and clock bridge contracts. Official documentation for copy_ticks_range states UTC semantics, while official symbol_info_tick field documentation omits an explicit timezone description and public MQL5 forum reports conflict. This is unresolved evidence, not a basis to rewrite active timestamp semantics without host captures.
- Main remains at c3d2f1c1a900a70f6c1baecee77b7c74304e6481; WP-T branch is based directly on main. PRs #903 and #904 remain separate and untouched.

### Current verified state (do not overstate)
- **Plan and tracking:** committed.
- **Clock holdover implementation:** committed; not test-executed.
- **Read-only timestamp capture tool:** committed; not owner-host executed.
- **Timestamp normalization correction:** deliberately not made pending raw evidence.
- **Full tick ingestion/recovery:** not implemented.
- **Downstream consumer acceptance:** open.
- **CI / Python 3.11 and 3.12:** not run.
- **Production and multi-host acceptance:** not performed.
- **Live broker mutations:** none.

### Exact next action
Create a draft PR from this dedicated branch to main to trigger repository CI without merging. Inspect the resulting checks and fix any regressions. Then run scripts/diagnose_mt5_timestamp_semantics.py on each supported Windows MT5 host (at minimum two hosts with differing local timezone/wall-clock settings), retain sanitized JSON captures, compare live and history raw epochs to independent UTC and exact UTC query bounds, and only then choose the timestamp normalization contract. Continue WP-T4 durable replay cursor/gap persistence and WP-T5 full consumer inventory after the timestamp contract is settled.


### CI and integration update — 2026-11-11
- PR #905 is open and marked ready for review; it remains unmerged. Base main is c3d2f1c1a900a70f6c1baecee77b7c74304e6481. Latest recorded source HEAD before this ledger update: 1a374d31e672d4bfd8188c0e16a210395511473e.
- CI run #4312 identified a lint failure: F841 unused identity_verified. The connector exception path was corrected to actually require identity_verified before returning holdover.
- CI run #4313 exposed a static diagnostic safety-test failure because the diagnostic serialized the raw tick tuple generically without explicitly naming time_msc. The diagnostic now explicitly preserves raw time and time_msc fields.
- A further production-path defect was found: get_ticks computed a recent_window boolean but still applied the live offset even when the requested query window was not near the current verified UTC anchor. It now clears the live clock authority when the range is stale and leaves the records explicitly unverified broker-server time. Added regression coverage in tests/test_mt5_market_data.py.
- Latest run #4316 is executing against source HEAD 1a374d31e672d4bfd8188c0e16a210395511473e. Lint has passed. Python 3.11 and 3.12 dependency installation succeeded and test collection is in progress. No full test result is available yet.
- Previous CI failure evidence is retained; no test protections were weakened. The static test failure was corrected in a subsequent commit; acceptance awaits the latest full matrix.
- PR #905 currently reports mergeable=true; do not merge until the latest CI and owner-host gates are satisfied.

### Exact next action
Poll CI run #4316 (and any subsequent run for the latest HEAD) until both Python test matrices finish. Fix every failure without weakening tests. Then run the read-only timestamp capture on at least two supported Windows MT5 hosts with distinct local clock/timezone configurations and compare raw snapshot/history epochs to independent UTC witnesses and exact UTC request bounds. Only after that evidence may get_price/get_ticks normalization be changed. Continue with durable replay cursor/gap persistence and the complete downstream consumer/candle audit. No live broker mutations.


## 2026-11-11 — WP-T2 through WP-T6 continuation checkpoint

- **Repository:** `Eugine336/apex-trader`
- **Completed predecessor PR:** #905 merged to `main` as `b5c1c34996d95509bd2eb0fba115e276b61f268d`.
- **Predecessor CI:** workflow run `43000951533` completed successfully; lint, Python 3.11 and Python 3.12 jobs all report success.
- **Continuation branch:** `phase2/wp-t2-t6-tick-recovery`, created from the merged main commit above.
- **Current branch HEAD:** branch created; no source changes committed in this continuation checkpoint.
- **Working-tree / CLI:** local clone attempt failed because this execution environment cannot resolve `github.com`. GitHub connector can inspect and update tracked files, but no local command/test execution is available in this environment.
- **Safety:** no live broker mutations; no real `.env`, credentials, account selection, production settings or runtime DBs accessed or modified.

### Recovered production findings (still open)
1. `tick/tick_sources.py` keeps replay cursor, fingerprint deduplication and gap count in process memory only.
2. A replay window exceeding 300 seconds is not recovered: it increments an in-memory gap counter, jumps the cursor to the current quote and resumes. This is a known data-loss boundary, not complete recovery.
3. Historical replay requires dataframe attributes `source_timestamp_verified=True`, `source_timezone=UTC`, and `utc_normalization=live_server_offset`; this remains coupled to an unresolved MT5 timestamp semantics question.
4. `TickRouter` publishes raw/verified events and invokes raw callbacks before its coalesced TickStore path. TickStore is not a durable raw tape.
5. `LiveCandleAggregator.warmup` currently treats every fetched broker row as closed, including the broker's potentially forming final bar. This is a confirmed source-level violation requiring a contract-safe correction and regression tests.
6. Live candle aggregation closes one prior forming candle when a tick crosses a boundary and does not synthesize empty bars. This is preferable to fabricating data, but gap state must be represented separately and consumers must distinguish missing data from no pattern.
7. Broker clock state and poller replay state are process-local. No verified restart-safe clock authority or replay cursor has yet been integrated.
8. MT5 timestamp semantics must not be globally rewritten until sanitized raw diagnostic captures from at least two owner hosts establish behavior for the supported terminal/broker combinations.

### Verified vs unverified
- **Verified:** PR #905 is merged; predecessor CI run #4317 is green for lint and both Python versions.
- **Implemented in predecessor:** bounded clock holdover uncertainty, broker identity revalidation, stale-live-clock exclusion for non-recent history requests, read-only timestamp diagnostic.
- **Not implemented/accepted:** complete WP-T2 lifecycle, WP-T3 timestamp contract resolution, durable WP-T4 gap/cursor recovery, full WP-T5 consumer/candle integrity, WP-T6 restart/multi-host recovery.
- **Not verified:** owner-host timestamp captures, sustained live feed continuity, restart recovery against real terminal behavior, complete end-to-end consumer delivery.
- **No acceptance gates T-A1 through T-A10 are marked complete by this checkpoint.**

### Exact next actions
1. Obtain sanitized output from `scripts/diagnose_mt5_timestamp_semantics.py` on at least two supported Windows MT5 hosts; compare raw `time`/`time_msc`, UTC query bounds, independent UTC witnesses, terminal/package versions and broker server identity.
2. Inspect the existing canonical persistence lifecycle and choose a durable per-symbol cursor/gap checkpoint that does not create a second event authority or store every tick in the generic event log.
3. Implement broker-identity-scoped cursor persistence, transactional checkpoint advancement only after successful routed replay, bounded overlap deduplication, paginated history recovery and durable unrecoverable-gap records. Never advance past unverified ranges.
4. Complete WP-T2 state transitions and observable failure/uncertainty/age evidence; test restart and identity changes.
5. Correct warmup so only provider-attested completed bars seed the closed-bar collection; preserve forming-bar state separately and test all market-open/market-closed edge cases.
6. Inventory every consumer of raw ticks, verified ticks, TickStore events, candles and cognition wakes; prove completed-bar-only Brain consumption and explicit gap/missingness propagation.
7. Run focused and full CI in a supported checkout, then obtain owner-host multi-host acceptance. Do not merge or mark WP-T accepted before these gates pass.


## 2026-11-11 — MT5 diagnostic direct-launch import fix

- **Branch:** `phase2/wp-t2-t6-tick-recovery`
- **Change:** `scripts/diagnose_mt5_timestamp_semantics.py` now resolves the repository root from its own file location and inserts it at the front of `sys.path` before importing the `MetaTrader5` package or Apex modules. This supports the documented direct invocation from the repository root and from other working directories.
- **Regression coverage:** `tests/test_mt5_timestamp_diagnostic.py` loads the diagnostic module without importing MT5, removes the repository root from a controlled `sys.path`, calls the bootstrap helper, and asserts the root is restored at index zero. Existing read-only safety assertions remain.
- **User-reported failure:** direct invocation raised `ModuleNotFoundError: No module named 'platforms'` at the import of `platforms.mt5.clock`. Root cause was Python's script-directory import path, not MT5 connectivity.
- **Status:** implementation committed via GitHub contents API; local pytest/lint not run in this execution environment. CI must verify.
- **Next action:** rerun the exact documented PowerShell command after pulling this commit. If it advances beyond import and fails, diagnose that next failure separately; do not conflate it with the import-path defect.


## 2026-11-11 — Repeated empty MT5 capture and symbol-feed diagnostic

- **Repository:** `Eugine336/apex-trader`
- **Main baseline:** `b417e349579adb9de210ede572b939cb87c75bee` (includes merged PR #906).
- **Working branch:** `fix/mt5-symbol-feed-diagnostics`
- **Branch base:** `b417e349579adb9de210ede572b939cb87c75bee`
- **PR:** not yet opened at time of this entry.
- **Protected paths:** real `.env`, credentials, runtime databases, broker account selection/settings remain untouched and off-limits.
- **Live broker mutation:** none; prohibited during this work.

### New owner-host evidence
Two successful direct invocations on the user's Windows 11 host, MetaTrader5 package 5.0.6231 / terminal 500.6308, broker server JustMarkets-Demo, both returned:
- terminal connected = true;
- `symbol_info_tick("XAUUSD")`: `time=null`, `time_msc=null`;
- `copy_ticks_range` for the preceding 60 seconds: zero rows;
- history API last error: `[1, "Success"]`;
- three independent NTP authorities sampled twice, each reporting roughly 0.56–0.59 seconds uncertainty;
- host wall clock approximately 0.7 seconds behind the projected UTC anchor.

Interpretation: this is a repeatable empty-data observation, not proof that the broker is not publishing ticks. The capture did not yet include symbol resolution, Market Watch visibility/selection or the quote fields available from `symbol_info`. The wall-clock difference is not a basis for correction at the reported witness uncertainty.

### Implementation in this increment
1. `scripts/diagnose_mt5_timestamp_semantics.py`: added read-only `symbol_info` snapshot fields, immediate error capture for symbol/tick/history calls, explicit history result state, and a wildcard candidate-name lookup only when exact symbol resolution fails. No `symbol_select` call is made; the diagnostic does not mutate Market Watch.
2. `tests/test_mt5_timestamp_diagnostic.py`: added regression tests for safe symbol-field projection, matching-symbol filtering, and static prohibition of symbol selection/order mutation.
3. `docs/WP_MT5_CLOCK_AUTHORITY_TICK_STREAM_INTEGRITY.md`: recorded both owner-host captures, their limits, and the follow-up diagnostic.

### Verification
- User executed the prior diagnostic successfully twice; this verifies the direct-launch fix on their host.
- The expanded symbol diagnostic has been committed to the working branch but has not yet been executed on an MT5 host.
- Local tests were not run in this execution environment.
- CI for the expanded diagnostic is pending.
- No timestamp normalization decision, feed-health acceptance or WP-T acceptance is claimed.

### Exact next action
Open a draft PR from `fix/mt5-symbol-feed-diagnostics` to `main`, inspect CI and fix all failures without weakening tests. Then have the user pull the branch and rerun the exact command. Interpret `symbol_info`, visibility/selection, Bid/Ask/time fields, tick error and history state together. If the exact symbol is absent, use the reported candidate names to identify the broker-native suffix. Obtain a second independent host capture before deciding timestamp semantics. Continue WP-T2–T6 implementation after the diagnostic cause is narrowed; do not stall the independent durable cursor/gap, candle warmup and consumer-audit work on this evidence gate.


## 2026-11-11 — PR #907 CI failure and correction

- **PR:** https://github.com/Eugine336/apex-trader/pull/907
- **Branch:** `fix/mt5-symbol-feed-diagnostics`
- **CI run:** `43003257190` (workflow run 4324)
- **Initial head:** `eb0e6ef6473a1e13780ee98cfb59731d75827d3e`
- **Failure:** both Python 3.11 and 3.12 jobs reported 4,534 passed / 1 failed; lint passed.
- **Root cause:** `test_mt5_timestamp_diagnostic_is_read_only` scans the diagnostic source for prohibited mutation API calls. A safety comment mentioned the prohibited API name, causing a source-text false positive. The diagnostic contains no invocation of that API.
- **Correction committed:** `59ddd9e4c8f674930c3efb41f45ffea03f94c17a`; reworded the comment to describe the read-only guarantee without embedding the forbidden API token. The test's safety assertion remains unchanged.
- **Verification status:** correction committed; CI rerun required. No tests are claimed passing for this correction until GitHub Actions completes.
- **Next action:** poll the new CI run for PR #907; fix any genuine failures without weakening safety checks. Keep WP-T acceptance gates open pending owner-host expanded diagnostic captures and end-to-end tick/recovery validation.
