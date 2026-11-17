# Apex — next work (handoff)

Written at the end of a working session so someone else can pick up without re-deriving context.
State when written: `main` @ `8e316a3a` (green, 4,619 tests incl. my open PRs), open PRs **#909** and **#910**, nothing merged by me.
Nothing here has run against a real MT5 terminal except the owner-host *diagnostic* captures recorded in
`docs/APEX_ENGINEERING_PROGRESS_LEDGER.md`. Everything else is tested with fakes and synthetic candles.

## 0. What is already done (do not redo)
| Area | Where | State |
|---|---|---|
| Concept framework, 56 methods + 20 lenses readable from price bars | `cognition/concepts/`, `docs/APEX_CONCEPT_COVERAGE.md` | merged |
| Quality harness (scale, mirror, time-shift, purity) + 3 direction-bias fixes | PR **#909**, `tests/test_concept_quality_harness.py` | open, green |
| Tick-history time domain proven from the response (not assumed) | PR **#910**, `MT5Connector.get_ticks` | open, green |
| Bounded verified tick gap recovery (300 s chunks, ≤3,600 s/poll, recorded gaps) | PR **#910**, `tick/tick_sources.py::MT5TickPoller._route_history_quotes/_route_history_window` | open, green |
| Bar time domain proven from the newest M1 bar; unproven bars are unverified | PR **#910**, `MT5Connector._bar_time_authority`, `get_ohlcv`, `get_ohlcv_range` | open, green |

**Why the bar fix matters:** before it, a broker with a non-UTC server clock (the owner host is ~UTC+2) made every bar look
hours in the future → every concept reading failed its freshness check → `derive_opportunity_geometry` returned nothing → no
trade could ever be derived. Reproduced with ±7200/10800 s labels (101 readings, no geometry). First step for anyone picking up:
merge/rebase #910 and #909.

## 1. Next work, in priority order

### 1. WP-T6 — durable tick cursor and restart recovery (blocks any live run)
- Persist `MT5TickPoller._history_cursor` (per symbol) and the recorded-gap list atomically (temp file + rename) under the
  runtime data dir; load at start; validate against terminal identity (account, server, symbol resolution) and refuse a cursor
  from a different identity.
- On restart, recover from the persisted cursor through the existing chunked path; anything older than the recovery horizon is
  a recorded gap (never synthesised). Add a restart test matrix: clean restart, crash mid-recovery, stale cursor from another
  terminal, clock moved backwards.
- Acceptance: WP-T **T-A5, T-A7**.

### 2. WP-T2 — terminal lifecycle and clock witnesses (T-A2, T-A3)
- Bind the clock state to terminal identity; detect terminal restart/reconnect/server change and invalidate cached proofs
  (`_bar_domain_cache`, clock offset). Witness provenance, disagreement, holdover and expiry per the WP-T spec.
- Replace the fixed 600 s TTL of the bar-domain proof with invalidation on identity/offset change plus a TTL.

### 3. WP-T5 — downstream consumers and candle integrity (T-A6, T-A4)
- Audit every consumer of bars/ticks: only verified frames reach analysis; completed bars only; lossless millisecond precision
  and provenance end to end (the raw epochs are now preserved as `time_raw_epoch` / `time_msc_raw_epoch` / `time_server_epoch`).
- Candle integrity: continuity/gap checks per timeframe, rejection of out-of-order or duplicate bars, with recorded evidence.
- Known limit to resolve: bar normalization applies one proven offset to the whole returned window. Bars older than a broker
  daylight-saving change may carry a different offset (recorded as `offset_validity`). Decide: per-chunk proof for older
  windows, or mark older bars as relative-time only.

### 4. Owner-host verification (cannot be done in the sandbox)
Run the diagnostics in the ledger on the owner host and a second host, across a DST change:
1. Ticks: confirm history rows sit inside the UTC request bounds while latest-quote epochs are server time
   (`time_domain_evidence` in the frame attrs records this per call).
2. Bars: confirm `bar_time_evidence` reports `m1_open_proof_server_offset` with the expected offset (or `m1_open_proof_utc`).
3. Symbols: confirm every production path resolves `XAUUSD` → `XAUUSD.ecn` (broker-native suffix) end to end.
Record evidence in the ledger; only then can T-A1…T-A10 move from OPEN.

### 5. Multi-symbol bar channel (unblocks 4 data-gated methods and 4+ lenses)
`analyze_market_frames` / `run_concepts` receive one symbol's bars. Cross-asset, currency strength, SMT divergence and
pairs/cointegration need synchronized completed bars for several symbols (same UTC cut-off, each proven by the bar time
authority). Add a channel that supplies other enabled symbols' frames (lazy, bounded) to concept modules, with the missing
symbol/untrusted bars represented as `unknowns`, never defaulted. See `docs/APEX_CONCEPT_IMPLEMENTATION_PLAN.md` WP-D.

### 6. Seven concept modules still fail mirror symmetry
Recorded as strict xfails with reasons in `tests/test_concept_quality_harness.py::KNOWN_MIRROR_DEFECTS`. Fix, then delete the
entry (the test XPASSes strictly). Modules: `swing_geometry` (Fibonacci anchor swing), `structure_patterns` (detection
asymmetric), `volume_profile` and `range_rotation` (bin alignment anchored at the minimum price — anchor at the range
midpoint), `seasonality` (signed tendency), `regime_switching` (trigger state), `wyckoff_volume_readings` (range-bound
selection). About 100 fixed window/lookback constants remain across concept modules (e.g. a 10-bar failed-auction lookback,
20-bar rolling dealing range): classify each as *indicator/concept definition* (comment it) or *parameter* (derive from the
instrument's own history).

### 7. Data-gated concepts that MT5 can satisfy (23 methods / 18 lenses are gated; ~10 are MT5-derivable)
Cross-asset, currency strength, SMT, pairs (multi-symbol bars, item 5); market impact (own fill history via
`history_deals_get`); carry/financing (swap fields); portfolio risk (positions/margin/`order_calc_*`); Bayesian, Monte Carlo,
calibration (need accumulated broker-truth outcomes). Depends on the broker: VWAP/volume profile/absorption/delta/footprint
(real volume or buy/sell tick flags), sessions/opening range (verified timezone + holiday calendar), futures expiry, synthetic
mechanics. **Not obtainable from the MT5 Python API:** economic calendar, rates/inflation/growth series, crypto
funding/open-interest, options IV — these stay `DATA-GATED` unless a separate source is approved.

### 8. System acceptance and the supervised demo run
Systems 1–10 in `docs/APEX_FULL_REDESIGN_ROADMAP.md` are merged-not-accepted. Write the demo-capture procedure for the owner
host: read-only first (market data only, no orders), then a supervised demo-account run recording the full cycle
(observe → concept readings → hypothesis → geometry → compliance → sizing → execution → management → close → learning) with
the evidence stored for review. No live broker mutation during engineering.

## 2. Rules for whoever picks this up
- Branch from the latest `main`; run the **full suite on a clean checkout of `main` first**, then on your branch, and list any
  test that fails on yours but not on `main`. (Two earlier merged PRs shipped broken tests/syntax because this was skipped.)
- Never relax a safeguard to make a test pass; never delete a failing test to hide it; never claim behaviour you have not run.
- Do not commit secrets or tokens; do not place broker orders; keep `docs/APEX_CONCEPT_COVERAGE.md` in sync
  (`python scripts/concept_coverage.py --check`).
- Brain is the only decision authority; concept code returns evidence with `polarity=0`; no configured analysis thresholds;
  unknown stays unknown (see `docs/APEX_REMAINING_WORK_PROMPT.md`).

## 3. How to verify quickly
```
python -m pytest -q tests --continue-on-collection-errors            # full suite
python -m pytest -q tests/test_mt5_market_data.py tests/test_mt5_bar_time_domain.py tests/test_mt5_tick_recovery.py
python -m pytest -q tests/test_concept_quality_harness.py             # 84 passed, 7 xfailed on #909
python scripts/concept_coverage.py --check
```
