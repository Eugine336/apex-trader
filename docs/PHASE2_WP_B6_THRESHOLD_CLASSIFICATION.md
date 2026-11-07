# WP-B6 — threshold classification and replacement audit

**Audit baseline:** `main` at `9c1c95a2c6e5c76def1b8e29c4547654c1f19e97` (2026-11-07).
**Status:** source-path audit; runtime replacement is the next implementation step.
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
- `CognitiveBrain._from_opinion()` requires `consolidation["market_domain_count"] >= min_evidence_domains` before advisor-assisted origination.
- `CognitiveBrain._originate_independent()` independently requires at least three informative market domains and repeats the same requirement against the fresh evidence set. It also describes those domain labels as independent confirmation, although different domains are not necessarily independent sources.
- `NativePositionManager.assess()` requires three market domains for a campaign-linked invalidation EXIT, in addition to two invalidation assessment domains.
- `CognitionConfig`, `core/system_context.py`, Brain status, and tests propagate and assert this value.

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

Keep the old constructor/config argument temporarily only as a deprecated compatibility input if required by callers; it must not affect decisions. Remove the config field, environment setting, status field, and obsolete tests after call-site migration is complete.

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

The current wake path uses `max(0.25 * M5 ATR, 3 * spread, tiny price floor)` and a 50% spread-change threshold. Those constants decide whether a market snapshot change wakes the Brain. The observer does not itself submit orders, but a wake filter can delay analysis of real changes.

**Disposition: remove the fixed ATR/spread wake thresholds.** Keep snapshot construction and evidence publication independent of wake coalescing. Every changed factual snapshot should request a Brain wake; the loop's bounded per-symbol queue and operational cadence may coalesce repeated requests. Do not discard or mutate evidence because a wake is coalesced. Add tests for small price moves, spread widening/narrowing, unchanged snapshots, repeated ticks, queue saturation, and bounded wake latency. Confirm position management still receives its required periodic cadence.

### 3. Event confidence-delta is an analytical wake gate; event minimum interval is scheduling

**Locations:** `config.py::CognitionConfig.event_confidence_delta` and `cognition/loop.py::maybe_reason_on_change()`.

The delta suppresses a wake unless a scalar change magnitude moves by a configured amount. That is not merely compute scheduling: it determines which changes are analysed. The per-symbol minimum interval is a separate rate limit on queued work.

**Disposition:** remove the confidence-delta condition. A valid change notification should enqueue cognition regardless of its magnitude; the existing per-symbol pending set and minimum interval can coalesce/rate-limit execution. Preserve periodic cycle and independent position-management cadence. Prove that queue throttling cannot alter evidence, freshness, or management safety. Instrument queue delay, coalesced notifications, and missed/late wake counts.

### 4. Remaining analysis constants: remove binary tuning, retain actual definitions

The second source scan found three additional analysis cutoffs that are not concept definitions or account/execution limits:

- `CognitiveBrain._synthesize_from_evidence()`: the advisor/evidence gap and uncertainty cutoffs (`0.15`, `0.60`) created discontinuous confidence changes. Replaced with continuous attenuation against uncertainty-adjusted evidence support; it can only reduce, never increase, advisor confidence.
- `BrainReasoner._classify_regime()`: the `0.67` trend vote ratio rejected a two-of-three strict majority due to floating-point representation. Replaced with a strict majority among unconflicted timeframe readings; ties remain mixed.
- `BrainReasoner.self_criticize()`: the `0.70` agreement threshold and fixed `1.04` multiplier rewarded a hypothesis merely because no criticism was found. Removed. Lack of criticism is not new positive evidence; confidence remains unchanged.
- `NativePositionManager.assess()`: the `0.65` confidence floor suppressed counter-hypotheses even when fresh native contradictions were present. Removed. The counter-hypothesis still requires an opposing direction and an actually observed contradicting pattern; this path can only reduce risk, never EXIT or add exposure.

Retained and classified rather than deleted:
- `opportunity_geometry.py` range-edge fraction (`0.15/0.85`) is part of the explicit range-rotation concept definition, not a universal entry-confidence gate. It remains a concept-level semantic boundary pending a source-defined range-edge contract.
- `brain.py` calibration artifact requirements (minimum sample count, walk-forward folds, selection coverage, holdout gap) are statistical artifact-integrity requirements. They determine whether historical-return evidence is admissible; they do not select trade direction or pattern. Do not relax them to make an opportunity pass.
- `loop.py` advisor consultation-depth bands are operational compute scheduling. They change the number of optional advisors consulted, not market evidence, candidate direction, or native Brain qualification.
- Positive net expected value, finite/positive geometry, verified costs, broker lot/tick/stops/freeze constraints, and account drawdown/exposure caps are capital/execution validity boundaries, not analytical tuning constants.

### 4. Advisor quorum is not market evidence and must not become a hidden dependency on optional AI

**Locations:** `CognitionConfig.min_advisors_for_action`, `CognitiveBrain._from_opinion()`, and native-only fallback.

Advisor count is infrastructure/council metadata. It must not be added to market evidence counts. Apex's native Brain is required to operate without any LLM. When advisor coverage is absent or insufficient, the system must use its native qualification path rather than treating missing advisors as market evidence or silently allowing a weak advisor opinion. Protective management must not be blocked by advisor quorum.

**Disposition:** retain quorum only as a policy for trusting an advisor-assisted response. Verify that an unmet/unknown quorum falls back to native assessment and that native-only decisions can still pass the complete candidate-specific gates. Do not require a fixed number of advisors for native reasoning.

### 5. Owner limits and execution constraints

**Existing surfaces:** `config.py::RiskConfig`, `governor/models.py::GovernorConfig`, `adaptive/risk_manager.py`, portfolio sizing, and broker adapters.

Risk limits currently exist in multiple structures. This is a consolidation problem, not a reason to delete protections.

**Disposition for WP-B7:** inventory every live risk value and consumer; establish one typed, validated owner-limits contract; route every exposure-increasing path through it; preserve broker-imposed constraints separately; and prove no downstream component can loosen the owner's limit. Do not silently change effective deployed values during migration. Do not edit `.env`.

## WP-B6 implementation order

1. Implement candidate-evidence lineage qualification and remove the three-domain gate from advisor and native entry paths.
2. Replace native management's three-domain EXIT gate with campaign-specific, provenance-aware invalidation/geometry qualification; retain HOLD on ambiguity and keep protective reductions available.
3. Remove observer ATR/spread wake constants; use evidence-change notification plus bounded queue coalescing.
4. Remove `event_confidence_delta`; keep `event_min_interval_seconds` only as an operational scheduler limit.
5. Remove obsolete config/env/test/status surfaces for deleted analytical constants after all call sites migrate.
6. Add the regression matrix above, including deterministic fixtures and mirror tests.
7. Run focused cognition, observer, management, portfolio, and execution suites; then coverage synchronization, canonical identity/safety audit, imports, lint, GitGuardian, and full Python 3.11/3.12 CI.
8. No live broker orders, credential changes, runtime database changes, or `.env` edits.

## Acceptance

WP-B6 is complete only when the old analytical gates are absent from all live call paths, their replacements are tested end-to-end, all protective/account/broker controls remain enforced, and full CI passes on both supported Python versions. A green documentation-only CI run is not WP-B6 acceptance.

## Explicit non-claims

This audit does not claim WP-B6 acceptance, WP-B7 owner-limits completion, WP-B5 production benchmark acceptance, or Phase 2 completion.
