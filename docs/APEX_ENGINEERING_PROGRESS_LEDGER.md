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
