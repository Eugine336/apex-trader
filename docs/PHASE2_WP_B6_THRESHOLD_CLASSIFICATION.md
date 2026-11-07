# WP-B6 — threshold classification and replacement audit

**Audit baseline:** `main` at `9c1c95a2c6e5c76def1b8e29c4547654c1f19e97` (2026-11-07).
**Status:** runtime replacements implemented on the WP-B6 branch; Python 3.11/3.12 full CI and focused Gold Desk tests pass. PR acceptance checks and merge remain pending.
**Safety boundary:** remove incorrect analytical gates only after replacing their intended protection with evidence-linked, campaign-specific qualification. Do not weaken broker, owner-risk, exposure, data-integrity, or fail-closed execution controls.

## Governing rule

A threshold is not a safety control merely because it blocks an action. Classification follows its actual effect:

- **Analytical threshold:** a fixed count, score, confidence, ATR/spread multiple, or delta that controls what the Brain sees, considers, or qualifies. Remove it or replace it with context-specific evidence and concept requirements.
- **Concept definition:** a value intrinsic to a named concept. Retain it, document its source, and test its definition.
- **Owner/account limit:** risk per trade, daily/weekly loss, total exposure, margin budget, leverage, and position capacity. Centralize in the typed owner-limits surface. Apex may tighten owner limits, never loosen them.
- **Broker/execution constraint:** tick/lot precision, stop distance, trading mode, margin feasibility, quote validity, and broker capabilities. These are verified adapter facts and remain enforced.
- **Operational scheduling limit:** queue coalescing, polling, compute budgets, and provider-call throttles. It may limit when work runs, never what evidence means or whether a protective management action is available.
- **Unclassified:** do not change until the complete call path is proven.

## Source-grounded findings

### 1. `min_evidence_domains = 3` is an analytical gate, not a valid universal safety limit

**Call paths:**
- The former `CognitiveBrain._from_opinion()` gate required `consolidation["market_domain_count"] >= 3` before advisor-assisted origination.
- The former `CognitiveBrain._originate_independent()` path independently required at least three informative market domains.
- The former `NativePositionManager.assess()` path required three market domains for campaign invalidation EXIT, in addition to two invalidation assessment domains.
- `CognitionConfig`, `core/system_context.py`, Brain status, and tests propagated this value. The constructor/config/env/status surfaces and all decision call sites have now been removed.

**Why the current implementation is wrong:**
- Domain labels are not source independence. Several readers can interpret the same OHLC lineage; counting their domains as independent corroboration is false diversity.
- A valid single-domain concept can be blocked even when its own activation, invalidation, and market-derived geometry are complete.
- The same arbitrary breadth floor can delay a justified campaign exit. It is especially inappropriate as a blanket veto on risk reduction.
- Native origination already has stronger candidate-specific requirements available: a directional native hypothesis, fresh evidence lineage, activated market-derived geometry, producer-verified fixed-horizon expected return, verified current execution costs, positive net EV, and shared portfolio/execution admission.
- The evidence contract already carries `independence_group`, verified source time, provisional status, data basis, lineage IDs, and measurement version. Concept synthesis already deduplicates shared provenance rather than summing concept counts.

**Disposition: REMOVE the universal three-domain floor.** Replace it with candidate-specific evidence qualification:
1. A new campaign must have a first-class directional opportunity in an actionable lifecycle state; direction/confidence alone never authorizes exposure.
2. The selected opportunity's supporting and geometry evidence IDs must resolve to current, same-symbol evidence at the decision cutoff. Missing or stale IDs fail closed.
3. Each supporting item must carry actual measurements and source identity; source-time verification and completed/non-provisional requirements are enforced wherever the producer contract requires them.
4. Count distinct `independence_group` values only when describing corroboration. Multiple concepts/readers sharing one group count once. Unknown provenance is explicitly unknown, never an independent confirmation.
5. Do not impose a universal minimum number of domains or independent groups. A single-domain concept can qualify only when its own complete activation/confirmation requirements, invalidation, geometry, return evidence, cost evidence, and shared risk admission all pass.
6. Preserve contradictions and uncertainty in the hypothesis comparison. Missing information is not positive evidence.
7. Protective risk reduction and campaign management must not be held behind an entry-analysis breadth threshold. EXIT requires campaign-linked invalidation/geometry evidence; uncertain or conflicting assessments remain HOLD, while valid protective actions remain available.

The old constructor/config argument, environment setting, status field, and obsolete count-based tests are removed. No compatibility argument remains in the decision API.

**Required regression matrix:**
- one valid single-domain concept with complete current evidence can qualify;
- several concepts on the same `independence_group` do not manufacture confluence;
- duplicated evidence IDs and repeated readings do not increase independent support;
- missing, stale, future-dated, wrong-symbol, provisional, or unverified required evidence cannot qualify;
- conflicting source evidence remains conflicted and cannot be collapsed into one direction;
- LONG/SHORT mirror fixtures produce equivalent qualification;
- campaign-linked invalidation and concept geometry can trigger protective management without the three-domain floor;
- missing campaign identity or incomplete invalidation context cannot trigger EXIT;
- provider outage does not disable native reasoning or risk reduction.

### 2. Observer ATR/spread wake thresholds are analytical scheduling leakage

**Location:** `gold_desk/apex_observer.py`, per-symbol `_last_brain_wake_state` decision.

The former wake path used `max(0.25 * M5 ATR, 3 * spread, tiny price floor)` and a 50% spread-change threshold. Those constants decided whether a market snapshot change woke the Brain.

**Disposition: remove the fixed ATR/spread wake thresholds.** Keep snapshot construction and evidence publication independent of wake coalescing. Every changed factual snapshot should request a Brain wake; the loop's bounded per-symbol queue and operational cadence may coalesce repeated requests. Do not discard or mutate evidence because a wake is coalesced. Add tests for small price moves, spread widening/narrowing, unchanged snapshots, repeated ticks, queue saturation, and bounded wake latency. Confirm position management still receives its required periodic cadence.

### 3. Event confidence-delta is an analytical wake gate; event minimum interval is scheduling

**Locations:** `config.py::CognitionConfig.event_confidence_delta` and `cognition/loop.py::maybe_reason_on_change()`.

The former delta suppressed a wake unless a scalar change magnitude moved by a configured amount. That determined which changes were analysed. The per-symbol minimum interval is a separate rate limit on queued work.

**Disposition:** remove the confidence-delta condition. A valid change notification should enqueue cognition regardless of its magnitude; the existing per-symbol pending set and minimum interval can coalesce/rate-limit execution. Preserve periodic cycle and independent position-management cadence. Prove that queue throttling cannot alter evidence, freshness, or management safety. Instrument queue delay, coalesced notifications, and missed/late wake counts.

### 4. Remaining analysis constants: remove binary tuning, retain actual definitions

The second source scan found three additional analysis cutoffs that are not concept definitions or account/execution limits:

- `CognitiveBrain._synthesize_from_evidence()`: the advisor/evidence gap and uncertainty cutoffs (`0.15`, `0.60`) created discontinuous confidence changes. Replaced with continuous attenuation proportional to advisor overstatement versus the market evidence mean, amplified by uncertainty; it can only reduce, never increase, advisor confidence.
- `BrainReasoner._classify_regime()`: the `0.67` trend vote ratio rejected a two-of-three strict majority due to floating-point representation. Replaced with a strict majority among unconflicted timeframe readings; ties remain mixed.
- `BrainReasoner.self_criticize()`: the `0.70` agreement threshold and fixed `1.04` multiplier rewarded a hypothesis merely because no criticism was found. Removed. Lack of criticism is not new positive evidence; confidence remains unchanged.
- `NativePositionManager.assess()`: the `0.65` confidence floor suppressed counter-hypotheses even when fresh native contradictions were present. Removed. The counter-hypothesis still requires an opposing direction and an actually observed contradicting pattern; this path can only reduce risk, never EXIT or add exposure.

Retained and classified rather than deleted:
- The fallback range-rotation path previously used a fixed normalized-position band (`0.15/0.85`) as a proxy for a measured edge. That was not a reliable invalidation/activation event. It now requires a completed-bar failed auction (sweep beyond the actual prior high/low and close back inside) plus same-timeframe directional momentum. The 15% near-boundary flag remains descriptive for pattern context only; it cannot authorize opportunity geometry.
- `brain.py` calibration artifact requirements (minimum sample count, walk-forward folds, selection coverage, holdout gap) are statistical artifact-integrity requirements. They determine whether historical-return evidence is admissible; they do not select trade direction or pattern. Do not relax them to make an opportunity pass.
- `loop.py` advisor consultation-depth bands are operational compute scheduling. They change the number of optional advisors consulted, not market evidence, candidate direction, or native Brain qualification.
- Positive net expected value, finite/positive geometry, verified costs, broker lot/tick/stops/freeze constraints, and account drawdown/exposure caps are capital/execution validity boundaries, not analytical tuning constants.


### 4.1 Other numeric boundaries classified by their actual semantics

The scoped scan of concept, regime, portfolio, risk and execution modules found additional values. These are not universal decision floors and should not be deleted as a batch:

- **Named concept definitions:** candlestick morphology ratios in `cognition/concepts/price_action.py`; displacement/body/rank definitions in `price_delivery.py`; structural body-ratio definitions in `structure_patterns.py`; variance-ratio/autocorrelation regime definitions in `regime_switching.py`; and close-location midpoint semantics in `wyckoff_volume_readings.py`. Retain them as named concept definitions, with LONG/SHORT mirror tests and source-relative measurements. They do not constitute a universal minimum evidence count.
- **Range context:** `market_lenses.py`'s 15%/85% near-boundary flag is descriptive pattern context only. The geometry fallback no longer uses it as an activation gate; it requires an actual completed-bar failed auction and same-timeframe response. The multi-timeframe `conflict_score` labels are reporting semantics, not an exposure authorization gate.
- **Microstructure definitions:** `cognition/market_snapshot.py` uses relative velocity, spread and tick-rate ratios plus momentum/range semantics to label acceleration, widening, burst, exhaustion and absorption. These are descriptive microstructure definitions, not direct trade authorization. Keep them named and test them against instrument/timeframe-mirrored fixtures; do not promote them into a universal confidence gate.
- **Regime classification:** `adaptive/regime_detector.py` exposes trending/volatile/quiet thresholds as tunable regime definitions. They classify a regime; they are not account-risk limits. Their calibration is a separate regime-methodology work item.
- **Learning validation:** `adaptive/score_optimizer.py`'s validation split, minimum validation sample and per-class/per-symbol trade counts, plus `calibration_artifacts.py`'s selection coverage, missed-rate, Brier, reliability and holdout-gap checks, are statistical safeguards against overfit. Keep them as learning protocol requirements; do not lower them to make an entry pass.
- **Execution-cost data quality:** `cognition/execution_cost_model.py`'s quote-age bound and minimum slippage sample count qualify whether cost evidence is usable. These are data-quality safeguards, not market direction thresholds; stale/unverified cost data must remain unqualified.
- **Portfolio triage:** `risk/portfolio_risk_state.py`'s R-multiple and stagnation bands contribute to a position-weakness ranking. They are advisory ranking inputs, not a direct broker order gate. Review them with the portfolio allocation/lifecycle work rather than treating them as evidence-domain safety.
- **Account risk:** the live drawdown/exposure curves and limits in `RiskConfig`, `RiskManagementConfig`, `RiskManager`, and `GovernorConfig` are account-risk controls. They belong in WP-B7's typed owner-limits migration. Preserve effective deployed limits during migration.
- **Broker execution:** lot-step/minimum, tick-size/value, stop/freeze distance, margin, quote freshness and trading-mode checks must continue to come from the broker/instrument contract. They are feasibility constraints, not market-analysis thresholds.

The retired score-based risk-scaling helper in `risk/risk_engine.py` is marked deprecated and has no repository call sites in the scan; it is not used as evidence for a live gate. Remove it only with the legacy-path cleanup after the complete route inventory. The live drawdown scaling curve remains an account-risk control for WP-B7, not an analytical gate.

### 5. Advisor quorum is not market evidence and must not become a hidden dependency on optional AI

**Locations:** `CognitionConfig.min_advisors_for_action`, `CognitiveBrain._from_opinion()`, and native-only fallback.

Advisor count is infrastructure/council metadata. It must not be added to market evidence counts. Apex's native Brain is required to operate without any LLM. When advisor coverage is absent or insufficient, the system must use its native qualification path rather than treating missing advisors as market evidence or silently allowing a weak advisor opinion. Protective management must not be blocked by advisor quorum.

**Disposition:** retain quorum only as a policy for trusting an advisor-assisted response. Verify that an unmet/unknown quorum falls back to native assessment and that native-only decisions can still pass the complete candidate-specific gates. Do not require a fixed number of advisors for native reasoning.

### 6. Portfolio sizing floor is an unsafe risk-inflation constant

**Location:** `portfolio/division.py`, `_SIZE_FLOOR = 0.15`.

The factor fold previously applied `max(0.15, min(1.0, raw_multiplier))`. A product of risk-reduction factors below 0.15 was therefore increased to 15% of base risk. The subsequent `max(_MIN_LOT, ...)` and `max(_MIN_STAKE, ...)` could inflate it again. This contradicts the layer's stated de-risking-only contract.

**Disposition: remove the 15% floor.** Apply the exact finite, positive factor product, capped at 1.0. A zero/non-finite product rejects. If the reduced amount is below the broker's executable minimum, reject rather than restoring risk. Recompute actual max loss after rounding and reject if it exceeds the requested post-factor risk budget. The daily-loss budget applies the same rule after re-sizing. Broker minimums remain feasibility constraints, never permission to increase owner risk.

### 7. Owner limits and execution constraints

**Existing surfaces:** `config.py::RiskConfig`, `governor/models.py::GovernorConfig`, `adaptive/risk_manager.py`, portfolio sizing, and broker adapters.

Risk limits currently exist in multiple structures. This is a consolidation problem, not a reason to delete protections.

**Disposition for WP-B7:** inventory every live risk value and consumer; establish one typed, validated owner-limits contract; route every exposure-increasing path through it; preserve broker-imposed constraints separately; and prove no downstream component can loosen the owner's limit. Do not silently change effective deployed values during migration. Do not edit `.env`.

## WP-B6 implementation status

1. **Complete:** candidate-evidence lineage qualification replaces the universal three-domain gate in advisor-assisted and native origination.
2. **Complete:** native management uses campaign-linked invalidation and verified concept geometry; generic invalidation requires distinct verified source lineages, while direct originating-concept geometry remains authoritative. Ambiguous/conflicting assessments HOLD; risk reduction is not held behind the old breadth floor.
3. **Complete:** observer ATR/spread wake thresholds removed. Changed factual quote snapshots request a wake; bounded queue coalescing and periodic cycles remain.
4. **Complete:** `event_confidence_delta` removed. `event_min_interval_seconds` remains an operational scheduler bound only.
5. **Complete:** obsolete domain-count and confidence-delta config/env/status/test surfaces removed.
6. **Complete:** removed discontinuous advisor confidence cutoffs, 0.67 regime vote ratio, no-criticism confidence boost, 0.65 counter-hypothesis floor, 15% portfolio sizing floor, and 15%/85% fallback geometry proxy. Replacements and regression fixtures are in this PR.
7. **Verified on current branch commit:** Ruff lint, focused Gold Desk tests on Python 3.11/3.12, and full test suites on Python 3.11/3.12 pass.
8. No live broker orders, credential changes, runtime database changes, or `.env` edits were performed.

Remaining acceptance work: verify all required repository checks on the current head, then merge. WP-B7 owner-limits consolidation remains a separate package.
## Acceptance

WP-B6 is complete only when the old analytical gates are absent from all live call paths, their replacements are tested end-to-end, all protective/account/broker controls remain enforced, and full CI passes on both supported Python versions. A green documentation-only CI run is not WP-B6 acceptance.

## Explicit non-claims

This audit does not claim WP-B6 acceptance, WP-B7 owner-limits completion, WP-B5 production benchmark acceptance, or Phase 2 completion.
