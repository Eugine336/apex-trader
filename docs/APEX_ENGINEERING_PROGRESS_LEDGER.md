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


## 2026-11-11 — Owner-host MT5 symbol resolution and timestamp-domain evidence

- **Branch:** `fix/mt5-symbol-feed-diagnostics`
- **PR:** #907 — https://github.com/Eugine336/apex-trader/pull/907
- **Diagnostic revision:** read-only expanded symbol/feed diagnostic on the PR branch.
- **Owner host:** Windows 11, India Standard Time; MetaTrader5 Python 5.0.6231 / terminal 500.6308; broker server JustMarkets-Demo.
- **Capture symbol:** `XAUUSD.ecn`; no credentials or account identifiers included.

### Evidence
1. Exact `XAUUSD` lookup failed with terminal error `[-4, "Terminal: Not found"]`; wildcard discovery returned `XAUUSD.ecn`.
2. Exact `XAUUSD.ecn` lookup succeeded; visible and selected; Bid 4049.70 / Ask 4049.79; latest tick API succeeded.
3. UTC-bounded `copy_ticks_range` returned 203 rows for 11:41:57.117621–11:42:57.117621 UTC; final raw row epoch `1794397376` / `1794397376835` ms lies within the requested interval.
4. Latest quote raw epoch `1794404576` / `1794404576984` ms is approximately 7,200 seconds ahead of the UTC witness projection.
5. Host wall time remained within the broad ~0.55–0.59 second NTP witness uncertainty; no clock correction is justified.
6. Diagnostic safety flags: credentials_read=false, host_clock_modified=false, orders_submitted=false, terminal_settings_modified=false.

### Root-cause interpretation
- **Confirmed for this capture:** the prior empty feed result was caused by requesting the non-existent exact broker symbol `XAUUSD`; the terminal's actual instrument is `XAUUSD.ecn`.
- **Strong evidence, not yet a global contract:** latest-quote timestamps and history-range timestamps appear to use different domains (latest quote approximately broker-server UTC+2; history rows align with UTC request bounds).
- **Potential production defect:** `MT5Connector.get_ticks` currently treats history epochs as broker-server time and subtracts the live offset for recent windows. If the history behavior repeats, this would incorrectly shift UTC history rows by two hours. No normalization change has been made pending repeat and second-host evidence.
- **Mapping integration remains to be proven:** `_COMMON_SUFFIXES` includes `.ecn`; audit production call paths to ensure canonical `XAUUSD` always passes through `symbol_map` and no consumer bypasses the resolved broker symbol.

### Status and next actions
- Owner-host broker-native quote/history: OBSERVED.
- Canonical-to-broker symbol mapping in source: IMPLEMENTED; runtime integration NOT YET VERIFIED.
- Latest/history timestamp contract: DATA-GATED; repeat same-host capture and second independent host required.
- PR #907 CI run #4325: Python 3.12 passed, lint passed, Python 3.11 still running at last poll.
- WP-T acceptance: all T-A1 through T-A10 remain OPEN.
- Next: poll run #4325; audit `symbol_map` production call graph and all `get_ticks` consumers; add non-mutating timestamp-domain regression fixtures; repeat owner-host capture and obtain second-host capture before any normalization rewrite. Continue durable tick recovery, completed-bar warmup and consumer audit independently.

## 2026-11-11 — WP-T timestamp correction and recovery implementation

- **Repository:** `Eugine336/apex-trader`
- **Base:** `main` at `b417e349579adb9de210ede572b939cb87c75bee`
- **Branch:** `fix/wp-t-timestamp-contract-recovery`
- **Latest recorded commit at this ledger update:** `74e64ab280109cc839cdb1a80462d6b213ec2401`
- **PR:** not yet created.
- **Protected paths:** real `.env`, credentials, runtime databases, broker account selection/settings remain untouched and prohibited.
- **Live broker mutation:** none; prohibited during engineering.

### Root-cause evidence
Two owner-host captures on the same Windows 11 MT5 terminal (JustMarkets-Demo, package 5.0.6231, terminal 500.6308) for `XAUUSD.ecn` repeatedly show latest quote `time`/`time_msc` approximately 7,200 seconds ahead of independent UTC witnesses, while `copy_ticks_range` rows align with the exact UTC request interval. The wrong unsuffixed symbol `XAUUSD` is unavailable; `XAUUSD.ecn` is visible, selected and returning quotes/history. This is repeatable same-host evidence, not fleet-wide proof.

### Changes committed
1. `platforms/mt5/mt5_connector.py`
   - Removed live broker-offset subtraction from `copy_ticks_range` history.
   - Preserves raw provider epochs in explicit source-epoch fields and legacy aliases.
   - Verifies seconds/milliseconds consistency and exact UTC query-bound membership before assigning UTC.
   - Leaves shifted, malformed or out-of-range history timezone-unknown and unverified.
   - Adds broker-server identity to live tick provenance for recovery checkpoint scoping.
2. `tick/tick_sources.py`
   - Requires `utc_request_bounds_verified` for history replay.
   - Splits long gaps into independently verified 300-second batches.
   - Stops on failed/empty/unverified batches; no cursor jump to current quote.
   - Restores and durably persists broker-server-scoped cursor and bounded fingerprints through EventStore.
   - Does not advance the durable cursor when checkpoint commit fails.
3. `persistence/domain_events.py`: registered `MT5_TICK_RECOVERY_CHECKPOINT`.
4. `event_driven_bootstrap.py`: injects the existing EventStore into the MT5 poller; no new database.
5. `tick/live_candle_aggregator.py`: rejects warmup frames without explicit `completed_bars_verified=True`.
6. `tests/test_mt5_market_data.py`: tests batch-local UTC corroboration, no offset subtraction, offset-shifted rows, inconsistent fields and stale-clock independence.
7. `tests/test_mt5_tick_poller_recovery.py`: tests 300-second batching, no advancement after a failed batch, durable checkpoint persistence/restore and broker identity mismatch.
8. `tests/test_live_candle_aggregator.py`: tests that unattested history cannot seed closed candles; synthetic accepted fixtures explicitly attest completion.
9. Documentation updates are being recorded in this branch.

### Verification and limitations
- No local test/lint/compile command has been executed in this environment.
- PR #907 diagnostic CI run `43014426036` completed green for Python 3.11, Python 3.12 and lint; this is not CI evidence for this implementation branch.
- Same-host timestamp captures are repeated; a second independent MT5 host and sustained feed evidence are still missing.
- The live aggregator's production warmup currently skips because the existing MT5 candle fetcher does not provide the new completed-bar attestation. This is intentionally fail-closed and must be resolved with a verified provider completion contract.
- Durable checkpoint code needs CI and real EventStore restart/failure integration tests.
- The complete clock lifecycle, full consumer map, gap/missingness propagation and Systems 2/6/7 acceptance remain open.
- T-A1 through T-A10 remain OPEN. No production acceptance or WP-T completion is claimed.

### Exact next actions
1. Open a PR from this branch to main and wait for Python 3.11/3.12 plus lint CI; fix failures.
2. Inspect CI results and correct test/source defects before further expansion.
3. Add EventStore-backed checkpoint integration/fault tests and ensure no cursor advancement on persistence failure.
4. Verify the broker timestamp semantics on a second independent MT5 host.
5. Complete the MT5 OHLCV completed-bar attestation, then re-enable aggregator warmup only when the provider contract is proven.
6. Finish System 2/6/7 consumer and lifecycle audits; update the roadmap and ledger with actual test evidence.


### Recovery hardening follow-up — 2026-11-11

- Durable checkpoint writes are rate-limited to at most one per five seconds per symbol; each checkpoint retains the most recent 256 fingerprints. This bounds EventStore write volume while keeping restart replay overlap bounded. The cursor in memory may advance between durable checkpoints; restart resumes from the last committed cursor and replays the intervening range.
- Empty history responses, invalid timestamp attestations, fetch failures, malformed timestamp batches and cursor-ahead conditions are retained as durable `MT5_TICK_RECOVERY_GAP` events when the canonical EventStore is available. The cursor is not advanced across these ranges.
- Added a real temporary-SQLite EventStore close/reopen test for checkpoint restoration, plus tests for write failure, checkpoint write throttling and durable gap deduplication. These tests are committed but not executed.
- The 300-second batch tests verify that a failed later batch leaves the cursor at the last successful batch rather than jumping to the requested end.
- The test suite still needs CI execution. Same-host timestamp evidence remains repeated but not multi-host.


## 2026-11-11 — WP-T completed-bar and API-specific UTC correction

- Updated MT5 API contracts based on MetaQuotes' official Python documentation and repeated owner-host captures.
- `get_ohlcv(include_forming=False)` now requests MT5 bar position 1, excluding the current forming bar by provider contract rather than a server-clock heuristic.
- `get_ohlcv_range` validates returned UTC bar epochs against the exact UTC request bounds; closed-only filtering uses the conservative lower bound of the independent UTC witness anchor.
- Event-driven `_fetch_candles` and LiveCandleAggregator warmup now use a completed-only fetcher and require `completed_bars_verified=True`.
- Cross-asset context requires completed-bar attestation for both primary and related legs.
- Forming-bar WorldModel publications are no longer wired to cognition wake callbacks. Verified tick-change wakes and completed candle-close analysis remain.
- Tests updated for MT5 chronological OHLCV ordering, start-position closed-bar semantics, independent-UTC range filtering and completed-bar-only cross-asset context. Tests are committed but not executed locally.
- Live quote `symbol_info_tick` remains broker-clock normalized; historical `copy_ticks_range` and bar data use API-specific UTC semantics. Multi-host and DST acceptance remain open.


## 2026-11-11 — Completed-bar production integration

- MT5 OHLCV now follows MetaQuotes' documented UTC contract. `copy_rates_from_pos(start_pos=1)` excludes the current forming bar by provider position; `copy_rates_range` validates returned epochs against exact UTC request bounds and uses a conservative independent UTC lower bound when closed-only filtering is requested.
- Event-driven `_fetch_candles` now requires provider-attested completed bars. The same contract feeds `CandleCloseHandler` and LiveCandleAggregator warmup. Cross-asset analysis rejects any frame without completed-bar attestation.
- Removed the forming-bar WorldModel publish callback into CognitionLoop. Brain reasoning now wakes on verified tick-change observations and completed candle-close events; provisional developing bars remain isolated to the position-management advisory.
- Added regression coverage for completed-bar position semantics, UTC range filtering, completed-only fetch wiring and cross-asset attestation.
- No tests on this branch have yet been executed. CI must validate the rebased branch.


## 2026-11-11 — Rebased WP-T integration state (supersedes earlier base/PR notes)

- **Base main:** `8e316a3a6a6d303c6f576807473797e551aca8ae` (includes merged PR #907).
- **Branch:** `fix/wp-t-timestamp-contract-recovery`.
- **PR:** #908, draft, base main.
- **Current branch HEAD:** `86bb4ea1d122d1c82b6260dbd087b5a0cb9b24c4`.
- **Changed production paths:** `platforms/mt5/mt5_connector.py`, `tick/tick_sources.py`, `event_driven_bootstrap.py`, `tick/live_candle_aggregator.py`, `persistence/domain_events.py`.
- **Changed tests:** `tests/test_mt5_market_data.py`, `tests/test_mt5_tick_poller_recovery.py`, `tests/test_live_candle_aggregator.py`, `tests/test_market_context_cross_asset.py`.
- **Changed docs:** `docs/APEX_MT5_DATA_CAPABILITY_CONTRACT.md`, `docs/MT5_CLOCK_BRIDGE.md`, `docs/WP_MT5_CLOCK_AUTHORITY_TICK_STREAM_INTEGRITY.md`, `docs/APEX_FULL_REDESIGN_ROADMAP.md`, this ledger, and `AUDIT_CONSTITUTIONAL.md`.

### Current implementation status
- Historical `copy_ticks_range` no longer has the live broker offset subtracted. UTC is attested only for bounded batches (≤300 seconds) when raw seconds/milliseconds agree and corroborate the exact UTC request bounds.
- Long recovery is split into bounded requests. Failed/empty/unverified ranges do not advance the cursor; durable gap events are recorded when EventStore is available.
- Existing EventStore stores broker-server-scoped cursors and bounded fingerprint overlap. Writes are rate-limited to five seconds per symbol; write failure blocks durable cursor advancement.
- MT5 bars follow documented UTC semantics. Position-one reads exclude the current forming bar; range reads validate UTC bounds and use an independent UTC lower bound for completion.
- Confirmed CandleCloseHandler reads, LiveCandleAggregator warmup and cross-asset analysis require completed-bar provenance.
- Forming-bar WorldModel publications no longer wake cognition. Verified tick-change wakes and completed candle closes remain.
- No live broker orders, terminal setting changes, host clock changes or protected-path access occurred.

### Verification state
- PR #907 diagnostic CI passed Python 3.11, Python 3.12 and lint.
- PR #908 CI for rebased HEAD `86bb4ea1d122d1c82b6260dbd087b5a0cb9b24c4` is queued: Python 3.11, Python 3.12, Gold Desk 3.11/3.12 and lint. No result is claimed yet.
- No local test/lint/compile command was executed.
- Same-host timestamp evidence: two captures, one host. A second independent host and DST-transition evidence remain outstanding.
- Durable EventStore restart test has been added using a temporary SQLite database; it is not yet executed.
- All T-A1 through T-A10 remain OPEN. WP-T is not accepted.

### Exact next action
Poll PR #908's full CI matrix for HEAD `86bb4ea1d122d1c82b6260dbd087b5a0cb9b24c4`. Fix every failure without weakening assertions. Then obtain a second independent MT5 host capture, exercise sustained owner-host tick recovery, and complete the remaining clock lifecycle and Systems 2/6/7 integration gates.
