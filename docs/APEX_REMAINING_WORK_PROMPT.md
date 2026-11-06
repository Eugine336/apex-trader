# MASTER PROMPT — remaining Apex implementation (hand this to every implementer)

You are implementing part of **Apex Trader**: a native "Brain" that trades like one experienced discretionary trader. Read this
whole prompt, then `docs/APEX_CONCEPT_IMPLEMENTATION_PLAN.md` (your work package), `docs/APEX_TARGET_TRADING_CYCLES.md`
(the behaviour you are building toward) and `cognition/identity.py` (the canonical identity statement). Repository:
`Eugine336/apex-trader`, default branch `main`.

## 1. The mission in the owner's words
Apex has all the trading knowledge and intelligence about every tool, concept (ICT, SMC, CRT and the rest), strategy,
indicator (RSI, EMA, ...), price action and instrument. It looks at the full market state like a normal trader and decides
*this state needs this knowledge*, analyses with it, and trades and manages the position to profitability. It is not
limited to rules; it is not told which concept or strategy to use; it has **no threshold or configuration values for
analysis**; it is not scalping or swinging; it carries **no direction bias**; the **market is the only source of truth**.
The account owner may set execution limits; Apex may decide within them from its analysis. After this work the native Brain
must be excellent *without* depending on an external AI provider (providers have budgets and can be unavailable; they are
advisory only).

## 2. What exists (do not rebuild)
- Concept framework `cognition/concepts/` (`base.py` contract; registered modules in `CONCEPT_MODULES`). Coverage register:
  `docs/APEX_CONCEPT_COVERAGE.md` — **41/79 analytical methods and 2/38 lenses read in full** at the time of writing.
- Integration already in the live path: concept readings → evidence (`market_lenses.analyze_market_frames`) → hypotheses
  (`brain_reasoning`) → geometry (`opportunity_geometry`: a satisfied reading supplies stop/target) → management
  (`native_management`: first milestone passed ⇒ `protect_profit`).
- Policy decided by merged PRs: calibration diagnostics scale risk and never veto a thesis (#809); no instrument-specific
  directional bias (#810); competing directional hypotheses are kept alive (#811); the executor and observer are
  instrument-agnostic; broker mutations go only through `ActionExecutor`; `ComplianceDivision` gates new exposure.
- Reference modules to copy: `crt.py`, `market_structure.py`, `liquidity.py`, `price_delivery.py`, `indicator_context.py`.

## 3. Hard rules (violating any = PR rejected)
1. **Brain is the only decision authority.** Concept code returns evidence with `polarity=0`; it never opens, sizes, exits,
   vetoes or places orders.
2. **Completed bars only; no look-ahead.** A swing/pattern is confirmed only by closed bars.
3. **No configured thresholds in analysis.** Strength = percentile rank against the instrument's own history
   (`percentile_rank`). Allowed constants are *definitions of the concept itself* (three-bar fractal, CRT's three candles,
   Fibonacci OTE levels) and must be commented as such. No magic numbers, no `config.py` knobs, no per-symbol branches.
4. **Unknown stays unknown.** Short history, missing feed, or no objective ⇒ `strength=None` / `objective_price=None` and an
   entry in `requires`/`unknowns`. Never default, back-fill or invent a level.
5. **Invalidation is a price or event** the market can falsify; objectives/milestones are earlier structural levels that exist
   in the data.
6. **Instrument-agnostic**, asset-class behaviour only via data the module reads and its own history.
7. **Faults are visible** (WARNING, not DEBUG); no silent `except: pass`.
8. **Never place or simulate live orders, never touch broker credentials, never commit secrets or tokens.** No live trading
   during engineering.
9. **Honesty about verification.** Report what you ran and what you did not. Do not claim behaviour you have not executed.
   Do not weaken a safeguard to make a test pass; do not delete a test to hide a failure.

## 4. How to work
1. `git fetch` and branch from the latest `main` (`git switch -c <wp-id>/<short-name> origin/main`). Pull again before
   opening the PR; if `main` moved, rebase/merge and re-run everything.
2. Read the existing reference modules and `base.py`; copy their structure. Put code in `cognition/concepts/<module>.py`,
   register it in `CONCEPT_MODULES`, declare `covers_methods` / `covers_lenses` (keys must exist in
   `cognition/analytical_knowledge.py` / `cognition/strategy_knowledge.py`).
3. For every concept write the **full reading** (applicability; per-side implication with strength vs own history, trigger
   state, invalidation, objective, milestones, `requires`, `conflicts_with`, management meaning). A measurement alone does
   not count.
4. Tests (plan §5): canonical positive, mirrored, cancelled/failed, ambiguous, no-signal, short-history⇒unknown, evidence
   shape; plus the shared no-look-ahead and mirror-symmetry harnesses; plus an integration test (candles → evidence →
   hypotheses → geometry) where the concept can supply geometry.
5. Regenerate the register: `python scripts/concept_coverage.py` (a test fails if it is stale).
6. **Run the full suite and compare with `main`.** `python -m pytest -q tests --continue-on-collection-errors` on a clean
   checkout of `main`, then on your branch; list any test that fails on your branch but not on `main` and fix it. Two recently
   merged PRs shipped broken syntax/tests because this was skipped.
7. One work package per PR. Keep `CONCEPT_MODULES` and the coverage doc edits minimal (they are merge hot-spots).
8. If a feed/channel you need does not exist (plan §4), build the channel first or return a reading whose `unknowns` name
   the missing data. Do not guess.

## 5. Work packages (details in the plan)
- **WP-A** session/time/auction: `opening_range`, `session_calendar`, `vwap`, `volume_profile`, `seasonality`, `range_rotation`.
- **WP-B** Wyckoff/effort-result/volume indicators: `wyckoff`, `wyckoff_effort_result`, `volume_indicators`.
- **WP-C** strategy-family readings: `trend_following`, `momentum_breakout`, `mean_reversion`.
- **WP-D** intermarket (needs multi-symbol bars): `smt_divergence`, `cross_asset`, `currency_strength`, `relative_value`.
- **WP-E** events/macro (needs calendar + rates feeds): `event_driven`, `macro_rates`, `macro_growth`.
- **WP-F** carry/derivatives/crypto/synthetic: `carry`, `derivatives`, `crypto_microstructure`, `synthetic_market`.
- **WP-G** microstructure/order flow (needs tick/DOM): `tick_microstructure`, `order_book`, `delta_cvd`, `footprint`,
  `absorption_exhaustion`, `market_impact`.
- **WP-H** quant/self-knowledge: `time_series`, `clustering`, `anomaly_detection`, `bayesian`, `monte_carlo`, `calibration`.
- **WP-I** execution/instrument/portfolio facts: `instrument_economics`, `execution_cost`, `portfolio_risk`.
- **WP-J** the 36 strategy lenses as composition modules (after their methods exist).
- **WP-B1…B7** Brain integration (single owner): context selection & composition; geometry (reading-supplied entries,
  multi-concept stops/targets, no static fallback fractions); management (partials, trailing, time decay, invalidation exits,
  concept levels instead of static fractions); hindcast replay + per-concept track record + eligibility artifact;
  performance budget with the full instrument universe; remove remaining analysis constants (`min_evidence_domains`, observer
  wake thresholds, static fractions); owner-limits surface.
- **Tech debt:** 72 unclassified silent exception handlers, audit-sandbox-only API dependency gap, stale roadmap register. The RL subsystem and its tests have been retired.

## 6. Definition of done (per PR)
- Register shows the item `FULL` (or `DATA-GATED` with the missing feed named).
- All plan §5 gates pass; no new failures versus `main`; no new silent exception handlers; no new config knob.
- PR description states: what is read in full, what remains unknown, how it was tested, the full-suite comparison
  (counts before/after), and anything not verified.

## 7. Reporting format for your final message
1. What you implemented (files, module keys, register delta).
2. Evidence: test names and counts, full-suite comparison with `main`.
3. What you did **not** do or could not verify, and why.
4. Decisions you need from the owner (only genuine policy questions).
5. The branch name and PR link.

## 8. Pitfalls already seen in this repo
Strength values are uncalibrated reasoning strength, not probabilities; evidence from the same OHLC shares an
`independence_group` and must not be counted as independent confirmation; random walks contain "patterns" by chance, so tests
must build deterministic fixtures; `analyze_market_frames` supplies completed bars only (the forming bar is dropped); stale
merged tests/syntax have broken `main` before; the observer is instrument-agnostic — never reintroduce a Gold-only path.
