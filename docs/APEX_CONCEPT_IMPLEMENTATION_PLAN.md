# Apex — Remaining Implementation Plan (full concept knowledge, native Brain)

Status baseline: `main` @ `dba20cd19eead2181d7788baa70c90fb81c747a5`.
WP-J strategy-lens composition was merged from PR `#823` (`phase2/wp-j-strategy-lens-composition`).
Source of truth for current coverage: `docs/APEX_CONCEPT_COVERAGE.md` (generated; run
`python scripts/concept_coverage.py`).
Current register: **56/79 analytical methods FULL, 8 DATA-GATED, 9 MEASURED and 6 UNMEASURED;
20/38 strategy lenses FULL and 18 DATA-GATED.**
PR #823 passed **4,151 tests** on both Python 3.11 and 3.12, Ruff, coverage synchronization,
canonical identity/safety audit, import checks, GitGuardian and both Gold desk jobs.
WP-J acceptance gates for current-cutoff freshness, no-look-ahead, mirror symmetry, Brain assessment
and full CI passed. This accepts the composition/assessment layer; it does not claim all 79 methods
are FULL or remove the explicitly named external-data gates.
The RL package, nine RL-only test files, training tools and PyTorch dependency were retired in PR #813.
The earlier audit-sandbox report and pre-PR #822 test counts are historical, not the current CI baseline.
Every PR must run the full suite and compare with `main` (§9).

## 0. What "done" means

Apex is one expert discretionary trader. Given the full market state it decides which concepts, tools, indicators and
price action this moment needs, analyses with them, opens, manages and closes on that judgement, and learns from what the
market does. It has no assigned strategy, style or direction bias and no tuning thresholds for analysis. Only execution
has hard limits (broker validity, account limits set by the account owner, capital preservation).

"Done" for this plan = for **every** catalogued method and lens:
1. a concept module returns a **full reading** (not a measurement) — applicability, per-side implication, strength vs
   the instrument's own history, trigger state, invalidation, objective/milestones, what is unconfirmed, what weakens it,
   management meaning;
2. the **Brain** can select it by context, combine it with other readings, build geometry from it and manage a position
   with it;
3. it is **tested** (§5) and the coverage register shows `FULL`;
4. nothing in it is a configured threshold, a static fraction, or an invented level.

## 1. Non-negotiable rules for every work package

1. **The Brain is the only decision authority.** A concept module returns evidence with `polarity = 0`. It never opens,
   sizes, exits or vetoes. (`cognition/concepts/base.py::reading_to_evidence`.)
2. **Completed bars only; no look-ahead.** Every swing/pattern is confirmed only by bars that have closed. Each module
   must pass the generic truncation test in §5.
3. **No configured thresholds.** Strength is a percentile rank against the instrument's own history on that timeframe
   (`percentile_rank`). The only constants allowed are *definitions of the concept itself* (e.g. CRT's three candles, a
   three-bar fractal, Fibonacci 0.62/0.79 for OTE), each commented as a definition.
4. **Unknown stays unknown.** Missing data, short history, or no objective ⇒ `strength=None` / `objective_price=None` and
   an entry in `requires` / `unknowns`. Never default, never back-fill.
5. **Levels are prices or events.** Invalidation is a price/event (e.g. "completed close beyond X on tf"); objectives and
   milestones are earlier structural levels that exist in the data.
6. **Instrument-agnostic.** No symbol/asset-class branches in analysis. Asset-class behaviour enters only as data the
   module reads (contract spec, session calendar) and as its own history.
7. **Faults are visible.** A module that raises is logged at WARNING by `run_concepts` and skipped; it never fabricates.
8. **Owner limits are not analysis.** Max risk/daily loss/etc. belong to the owner-limits surface (WP-B7), not to
   concept code.

## 2. Module contract and file layout

```
cognition/concepts/<module>.py   class X: key, family, covers_methods, covers_lenses, read(symbol, bars_by_tf)->list[ConceptReading]
cognition/concepts/__init__.py   register in CONCEPT_MODULES
tests/test_concept_<module>.py   see §5
docs/APEX_CONCEPT_COVERAGE.md    regenerate (a test fails if stale)
```
`bars_by_tf`: `{"M5","M15","H1","H4","D1": [ {open,high,low,close,volume?,timestamp_epoch,source_timestamp_verified} ]}`,
completed bars only, full history the connector supplies (`market_lenses.analyze_market_frames`). Multi-symbol,
tick, order-book, calendar and derivatives inputs need new input channels (§4) — until a channel exists the module
must return a reading with the data listed under `unknowns`, not guess.

Reference implementations to copy: `crt.py` (phases, invalidation, milestones), `market_structure.py` (swings, state,
events), `liquidity.py` (pools, sweeps, objective from real pools), `price_delivery.py`, `indicator_context.py`.

## 3. Work packages — concepts (79 analytical methods, 38 strategy lenses)

Priority tiers: **T1** needs only OHLC(V) already supplied; **T2** needs multi-symbol bars or a new
scalar feed; **T3** needs a new data source (tick/DOM/derivatives/macro). Items in T3 must ship their data channel
first (§4). Level = register level today.

### WP-A Session, time, auction (T1; VWAP needs volume — tick volume is a proxy, state that in `unknowns`)
| Key | Level | Full reading must include |
|---|---|---|
| `opening_range` | UNMEASURED | Opening range of each session (from `session_calendar`), break/acceptance/failure of the high/low, retest, measured objective only from earlier structure; invalidation = close back inside; session-relative percentile of range size. |
| `session_calendar` | MEASURED | Session/DST/holiday state, time-to-open/close, rollover window, thin-liquidity flags — as applicability + `requires` for other readings (not a veto). |
| `vwap` | MEASURED | Session and anchored VWAP (anchors from swing/event/gap), deviation percentile vs own history, acceptance/rejection at VWAP, VWAP as mean-reversion draw or trend support. |
| `volume_profile` | MEASURED | POC/VAH/VAL/HVN/LVN for session/week, value-area migration, acceptance vs rejection at nodes, naked POCs as objectives. |
| `seasonality` | MEASURED | Day-of-week/time-of-day/month tendencies measured against the instrument's own history with sample sizes; reading says "weak/strong/insufficient evidence", never a rule. |
| `range_rotation` | MEASURED | Balance vs imbalance, rotation between range edges, value area, failed auction at edges, objective = opposite edge/POC. |

### WP-B Wyckoff, effort/result, volume indicators (T1)
| Key | Level | Full reading must include |
|---|---|---|
| `wyckoff` | MEASURED | Phase (accumulation/distribution/markup/markdown A–E), events (PS, SC, AR, ST, Spring/Upthrust, SOS/SOW, LPS/LPSY, BU), range bounds, phase confidence from event sequence, invalidation = range break against thesis, objective = range projection only where structure gives one. |
| `wyckoff_effort_result` | MEASURED | Effort (volume/range) vs result (price progress): absorption, no-supply/no-demand, climactic action, divergence; percentile vs own history. |
| `volume_indicators` | MEASURED | OBV/MFI/CMF/A-D as readings: confirmation vs divergence with price structure, regime-aware (trend vs range). |

### WP-C Strategy-family readings (T1) — compose method readings into the way a trader uses them
| Key | Level | Full reading must include |
|---|---|---|
| `trend_following` | MEASURED | Trend state + pullback depth/quality vs own history, continuation trigger, trend efficiency, invalidation = trend-defining swing, objective only from structure, trail anchors. |
| `momentum_breakout` | MEASURED | Acceleration, volatility expansion, breakout acceptance vs failure (holds beyond level N completed bars? — derive N from the instrument's own breakout history, not a constant), retest behaviour. |
| `mean_reversion` | MEASURED | Z-score/mean distance percentile, regime filter (does reversion work in this state? measured from own history of reversions), objective = mean/VWAP/range midpoint, invalidation = acceptance beyond extreme. |
| `event_driven` | MEASURED | See WP-E. |

### WP-D Intermarket, relative strength, relative value (T2 — needs bars for several symbols in one call)
| Key | Level | Full reading must include |
|---|---|---|
| `smt_divergence` | UNMEASURED | Correlated-instrument swing divergence at a liquidity sweep (one makes a new extreme, the other does not); pair selection from measured correlation, not a fixed table. |
| `cross_asset` | MEASURED | Rolling correlation/beta/lead-lag stability, breakdown of a relationship as a signal, relative-strength ranks. |
| `currency_strength` | MEASURED | Currency/commodity strength ranks and dispersion across all enabled pairs; pair-level implication = strong vs weak. |
| `relative_value` | MEASURED | Cointegration/residual z-score, half-life, stationarity of spread, invalidation = residual regime break. |

### WP-E Events and macro (T3 — needs a verified calendar feed with actual/forecast/previous and timestamps)
| Key | Level | Full reading must include |
|---|---|---|
| `event_driven` | MEASURED | Pre-event positioning risk, surprise size vs the event's own history, post-release reaction (initial move vs acceptance), volatility jump, fade vs continuation evidence. |
| `macro_rates` | UNMEASURED | Policy rate path, real yields, curve, inflation surprise; per-currency effect measured against historical co-movement, not hardcoded sign rules. |
| `macro_growth` | UNMEASURED | GDP/CPI/PCE/NFP/PMI surprise z-scores and their measured instrument reactions. |
| (lens) `news_event_reaction`, `event_macro` | — | Compose the three above. |

### WP-F Carry, derivatives, crypto, synthetics (T2/T3)
| Key | Level | Full reading must include |
|---|---|---|
| `carry` | MEASURED | Swap/financing by direction and tenor from the broker spec, carry vs expected move, rollover timing — as cost/benefit evidence. |
| `derivatives` | MEASURED | Futures expiry/roll, days-to-expiry, option IV/skew/OI where a feed exists; otherwise `unknowns`. |
| `crypto_microstructure` | UNMEASURED | Funding, OI, liquidation clusters, exchange basis (needs exchange API feed). |
| `synthetic_market` | UNMEASURED | Deriv synthetic contract mechanics (volatility index tick behaviour, spike/crash cadence, provider regime) from the contract spec and own history; no external-news logic. |

### WP-G Microstructure and order flow (T3 — needs tick bid/ask and, where the broker exposes it, DOM)
| Key | Level | Full reading must include |
|---|---|---|
| `tick_microstructure` | MEASURED | Tick/quote rate, spread dynamics, micro-price, volatility-of-quotes; readiness-to-trade evidence. |
| `order_book` | MEASURED | Depth imbalance, queue imbalance, liquidity pulling; availability depends on broker DOM. |
| `delta_cvd` | MEASURED | Bid/ask delta and CVD with price-vs-delta divergence (tick-rule estimate if no true aggressor flag; label it). |
| `footprint` | UNMEASURED | Footprint imbalance (needs aggressor-side volume per price). |
| `absorption_exhaustion` | MEASURED | Absorption at levels, exhaustion, replenishment/depletion. |
| `market_impact` | UNMEASURED | Expected slippage/adverse selection for the intended size from own fill history. |

### WP-H Quantitative and self-knowledge (T1/T2)
| Key | Level | Full reading must include |
|---|---|---|
| `time_series` | MEASURED | Autocorrelation, stationarity, regime of dependence per timeframe → which concept families have been working. |
| `clustering` | MEASURED | Market-state embedding and nearest historical states with their outcomes. |
| `anomaly_detection` | UNMEASURED | "This state is unlike any seen" (novelty score vs history) → widen `unknowns`, never act as a signal. |
| `bayesian` | MEASURED | Prior/likelihood/posterior and credible interval for a concept's hit rate in the current state. |
| `monte_carlo` | UNMEASURED | Bootstrap/Monte Carlo of the trade plan and portfolio (drawdown, tail quantile) as evidence for sizing/viability. |
| `calibration` | MEASURED | Brier/reliability of Apex's own stated strengths per concept (feeds WP-B4). |

### WP-I Execution, instrument and portfolio facts (T1) — evidence for the Brain, not rules
`instrument_economics`, `execution_cost`, `portfolio_risk`: readings of viability (cost vs expected move, min-lot vs
risk, correlated exposure, expected shortfall, risk of ruin). They inform Brain judgement; hard limits stay in the owner
limits (WP-B7).

### WP-J Strategy lenses (36 composed lenses; 18 FULL and 18 DATA-GATED at the last green baseline)
PR #823 implements the composition reader and Brain-facing assessment contract. The 18 remaining specialist lenses (`crt` and `market_regime_transition`) are read by their dedicated modules; 18 composed lenses are currently FULL and 18 are DATA-GATED. WP-J acceptance completed in PR #823: current-cutoff freshness, no-look-ahead, mirror-symmetry, Brain challenge and full CI gates passed. The composition layer remains bounded by its explicit method and feed prerequisites.

A lens is the *style view* of its methods. Implement each as a composition module (no new measurement): which method
readings it consumes, the market states in which that style is appropriate (from measured regime evidence), how that style
builds stop/target and manages, its known failure modes, and an explicit `unknowns` list. Mapping (lens → methods):

```
ict_smc: ict_smc, order_blocks, market_structure, inducement, displacement
trend_following: trend_following, roc_momentum, momentum_breakout       momentum: momentum_breakout, roc_momentum, displacement
breakout: momentum_breakout, displacement                               failed_breakout: failed_auction, liquidity_sweep, inducement, break_retest
mean_reversion: zscore_mean, mean_reversion                             range_auction: range_rotation, vwap
candle_price_action: candlestick_patterns, failed_auction, break_retest order_flow_auction: wyckoff_effort_result, volume_profile, volume_indicators, order_book
volatility_regime: realized_volatility, bollinger, momentum_breakout    wyckoff: wyckoff, volume_indicators, power_of_three
session_intraday: opening_range, vwap, power_of_three, range_rotation   event_macro: macro_rates, macro_growth, event_driven
cross_asset: cross_asset, currency_strength                              statistical_quant: zscore_mean, time_series, bayesian
microstructure: tick_microstructure, execution_cost                      support_resistance: support_resistance, market_structure, pivot_points, crt
moving_average: moving_averages                                         oscillator_divergence: divergence, stochastic, rsi, macd
trend_strength: adx_directional, trend_following                        fibonacci_geometry: fibonacci, dealing_range, ote
gap_opening_range: opening_range, gap_analysis, session_calendar        volatility_breakout: realized_volatility, bollinger, keltner_donchian
seasonality_cycles: seasonality                                         relative_value: relative_value, cross_asset
carry_funding: carry                                                    news_event_reaction: macro_growth, event_driven
correlation_exposure: portfolio_risk                                    execution_costs: execution_cost, market_impact
risk_of_ruin: portfolio_risk, monte_carlo                               auction_market_profile: volume_profile, range_rotation
wyckoff_effort_result: wyckoff_effort_result, wyckoff                   pattern_discovery: anomaly_detection, clustering
execution_order_types: execution_cost, market_impact                    learning_calibration: calibration
derivatives_context: derivatives, instrument_economics
```
(Mapping was derived from anchor overlap; the lens owner confirms it against `cognition/strategy_knowledge.py`.)

## 4. Data channels (prerequisites for T2/T3 packages)
Each channel delivers **source-timestamped** data with a `source_timestamp_verified` flag and degrades to "unknown".
1. **Multi-symbol bars in one call** (SMT, cross-asset, currency strength, relative value) — extend
   `analyze_market_frames` so `run_concepts` can receive every enabled symbol's frames.
2. **Tick bid/ask + aggressor estimate** (microstructure, delta/CVD) — from `tick_store`.
3. **Order-book/DOM** where the broker exposes it (MT5 `market_book`); otherwise unknown.
4. **Economic calendar with actual/forecast/previous** and event windows (event/macro).
5. **Rates/yields/inflation series** (macro) and **derivatives/crypto feeds** (funding, OI, liquidations, IV).
6. **Instrument spec + swap/financing** (already partly present): contract size, tick value, stops level, swap long/short.

## 5. Quality gates (every concept PR)
1. **Canonical tests** (copy `tests/test_concept_crt.py`): bullish, bearish mirror, cancelled/failed, ambiguous, no-signal,
   short history ⇒ `None`/unknown, evidence shape (`polarity=0`, completed bars, `independence_group`).
2. **No-look-ahead harness**: for each module, truncating the last `k` bars must not change any reading that is reported
   on the shorter history (add to a shared `tests/test_concept_no_lookahead.py` parametrised over `CONCEPT_MODULES`).
3. **Mirror symmetry property test**: transform prices `p → c − p`; LONG↔SHORT readings must swap with mirrored levels.
4. **Replay degeneracy test**: run on ≥ 1 year of real candles per asset class; assert the reading is neither always on
   nor never on, and report frequency (record, don't tune).
5. **Integration test** (copy `tests/test_concept_integration.py`): candles → evidence → hypotheses → geometry.
6. **Register in sync** (`python scripts/concept_coverage.py --check`), full suite no new failures vs `main`.
7. **Code review checklist**: no config numbers; no symbol branches; every `None` is reported in `requires`/`unknowns`.

## 6. Brain integration work packages (single owner; these make the concepts *usable*)
- **WP-B1 Context selection and composition.** Today every applicable implication becomes its own hypothesis (noisy);
  PR #811 keeps competing trend and mean-reversion hypotheses alive, which this layer must preserve.
  Build the layer that decides which concepts apply in the current market state (regime/volatility/session from readings'
  `applicability`), merges readings that share an `independence_group`, scores **confluence and conflict** across
  concepts, and produces one thesis per direction with the strongest consistent invalidation/objective. Concept
  relevance learned from outcomes (WP-B4) is *consulted*, never a gate.
- **WP-B2 Geometry — typed entry instruction propagation merged (PR #825).** Concept-supplied entry price,
  zone bounds and MARKET/LIMIT/STOP order type now travel through geometry, opportunity, campaign translation and the
  shared execution-intent contract. Conflicting or incomplete instructions fail closed. Broker pending-order placement,
  reconciliation, cancellation and lifecycle remain deliberately unsupported until adapters implement them; LIMIT/STOP
  are rejected rather than converted to market. The older fixed payoff-geometry settings were removed in PR #824.
  Multi-concept structural stops/targets and ordered milestones remain evidence-derived; WP-B2 acceptance is still
  bounded by the missing broker pending-order lifecycle.
- **WP-B3 Management — core implementation merged (PRs #826 and #827); acceptance remains open for data/lifecycle gates.**
  The native manager now handles concept milestone partials, completed-close invalidation exits, objective exits,
  structural trailing anchors and holding-horizon reassessment. Milestone actions use durable write-ahead campaign
  progress: ambiguous outcomes stay pending until broker-size reconciliation, while explicit sink rejection can clear
  the marker. Brain action IDs propagate into close/partial/stop execution intents; the aggregator collapses exact
  duplicates and rejects conflicting action-ID reuse. Portfolio reallocation emits candidates only; the Brain must
  author the actual management plan and any partial fraction. The static reallocation trim fraction was removed.
  Protective-stop validation consumes an explicit concept/structural price and does not synthesize a fraction or
  breakeven. News-window management remains DATA-GATED until a verified economic-calendar feed with source-time
  attestation exists. A broker-side acknowledgement/reconciliation lifecycle is still required to resolve ambiguous
  pending actions beyond observed position-size change. Full CI passed on Python 3.11 and 3.12 (4,170 tests each),
  lint, GitGuardian and both Gold desk jobs; no broker orders were sent.
- **WP-B4 Calibration without a cold start — OHLC hindcast foundation merged (PR #828).** The shared concept
  reader now powers both live evidence conversion and offline replay. The replay validates SHA-256 manifests, explicit
  UTC source timestamps and bar-open/bar-close semantics; runs every registered concept reader and the native Brain
  with completed bars only at each decision cutoff; labels concept implications from their own objective/invalidation
  levels; treats same-bar double touches as ambiguous; and reports per-concept/timeframe/direction/explicit-state
  outcomes, reader faults, unlabelable implications and coverage. Brain confidence samples require selected
  market-order opportunities, explicit stop/target geometry, visible geometry evidence IDs and non-overlapping
  decision windows. Calibration seeding is opt-in and idempotent per dataset/protocol. PR #828 passed 4,184 tests on
  Python 3.11 and 3.12, Ruff/lint, GitGuardian and identity/coverage/import checks.
  **WP-B4 is not accepted yet.** The manifest's source attestation is caller-declared, not independently authenticated;
  OHLC labels exclude spread/slippage/financing/latency and cannot create System 4's verified bid/ask quote artifacts.
  The per-concept × market-state summaries are report outputs, not yet producer-registered artifacts consulted by the
  live Brain. Remaining: connect verified quote-history events to the existing `CalibrationProducer` for System 4
  development/holdout/walk-forward eligibility; make concept track records producer-owned and available to live Brain
  as non-gating context; validate on owner-supplied history; and move the 0.25 calibration size floor into WP-B7.
- **WP-B5 Performance — first implementation layer merged (PR #829).** Pure OHLC concept readings are cached
  per module instance and symbol against a digest of the full completed-bar history; historical corrections invalidate
  the entry and contextual readers (ticks, depth, portfolio, execution costs and reading-composition modules) still
  refresh each cognition pass. Broker frames are materialized once while preserving the existing bounded raw-lens
  window and list-source semantics. Confirmed fractal swings update incrementally for appended/rolling windows;
  MarketStructure and Liquidity share the symbol/timeframe swing state, including prefix-only pool reads. Untouched
  swing-pool status now uses suffix extrema in linear time. Per-module elapsed time, total per-symbol elapsed time,
  bar counts and cache hit/miss metrics are exposed in structured debug logs. PR #829 passed 4,196 tests on Python
  3.11 and 3.12, Ruff/lint, GitGuardian and both Gold desk suites.
  **WP-B5 is not accepted yet.** No production full-universe benchmark has been run, so no latency reduction or
  per-symbol budget is claimed. Remaining: measure the real configured instrument/timeframe universe, set an
  evidence-based per-symbol budget, and extend incremental computation to the other costly structures/pools while
  preserving source-time and concept semantics.
- **WP-B6 Remove remaining analysis constants.** `min_evidence_domains = 3`, observer wake thresholds, compliance
  thresholds that are analysis rather than execution safety — classify each as (owner limit | definition | remove).
- **WP-B7 Owner-limits surface.** One explicit place for the limits the account owner sets (risk per trade, daily
  loss, max exposure, margin), which Apex may *tighten* from its analysis but never loosen. Everything else is Apex's
  judgement.

## 7. Tech debt to clear alongside
- `tests/test_silent_swallow_observability.py`: 72 unclassified silent exception handlers; this is the sole current full-suite failure and must be classified individually without weakening the audit.
- `tests/test_api_endpoints.py`: the earlier audit sandbox lacked `email-validator`; GitHub Actions collection now passes on Python 3.11 and 3.12.
- Stale docs: `docs/APEX_FULL_REDESIGN_ROADMAP.md` completion register for Systems 1–4 (it still describes #798 as an
  unmerged draft).
- Rename the `gold_desk` package (cosmetic; the observer is instrument-agnostic).

## 8. Order and parallelism
1. **Now, in parallel (T1, no new data):** WP-A, WP-B, WP-C, WP-H (calibration/time_series/bayesian), WP-I, plus all of §5's shared
   harnesses (no-look-ahead, mirror symmetry).
2. **Prerequisite channels:** §4.1 → WP-D; §4.2/4.3 → WP-G; §4.4/4.5 → WP-E, WP-F.
3. **Brain integration (one owner, starts immediately):** WP-B1 → WP-B2 → WP-B3; WP-B4 in parallel; WP-B5–B7 as
   gates before any live use.
4. **Lenses (WP-J)** after the method modules they compose exist.
5. **Acceptance:** register shows 79/79 methods and 38/38 lenses `FULL` or explicitly `DATA-GATED` with the missing feed
   named; replay degeneracy report published; a paper/demo run across several asset classes with the
   full cycle (observe → analyse → open → manage → close → learn) recorded.

## 9. PR protocol
One work package per PR from a branch off the latest `main`; run the full suite and compare to `main`; regenerate the
register; no force-push to `main`; do not edit another package's files without coordinating (the register and
`CONCEPT_MODULES` are the merge hot-spots — keep those edits minimal).
