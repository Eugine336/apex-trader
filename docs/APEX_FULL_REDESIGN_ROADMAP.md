# Apex Trader — Full Redesign Roadmap and Acceptance Gates

**Status:** planning baseline; not a completion claim.  
**Canonical identity authority:** `cognition/identity.py::IDENTITY_STATEMENT`, with the Constitution and identity documentation binding all implementations.  
**Scope:** end-to-end architecture, market reasoning, execution, capital protection, learning, operations and release verification.

## Operating rules

1. The canonical identity is the sole identity source. No subsystem may redefine Apex as one strategy, indicator, timeframe, broker or instrument.
2. The CognitiveBrain is the sole market decision authority. Analysts emit evidence; execution does not reinterpret the thesis.
3. Unknown, stale, unverified or missing data remains unknown. It is never silently replaced by a convenient default.
4. Fail-closed behavior applies to new exposure when identity, data provenance, broker constraints, costs, sizing, risk state or execution state cannot be verified. Risk-reducing management and emergency close paths must remain available.
5. No threshold or safeguard may be weakened merely to pass tests or enable a trade.
6. No live broker order is permitted during engineering verification. Test adapters must be unmistakably synthetic and unable to connect to a real account.
7. A merge is not acceptance. Each system requires evidence from source review, focused tests, identity audit, integration checks, and explicit unresolved-risk reporting.
8. Never claim profitability or predictive edge from descriptive outcomes, in-sample fit, synthetic data or incomplete validation.

## System map

This plan defines **10 implementation systems**. They are acceptance stages, not ten independent trading bots. System 1 and System 2 have prior implementation work; System 2 remains open until its gates below are proven. Later systems may have existing components, but must be re-audited against these acceptance criteria.

### System 1 — Canonical identity, contracts and architecture
**Purpose:** establish one coherent, testable language and authority model across the repository.

Deliverables:
- One canonical identity statement/version; remove or quarantine conflicting legacy identity definitions.
- Explicit contracts for evidence, market state, hypotheses, decisions, campaigns, opportunities and execution intents.
- Traceable evidence IDs, source timestamps, freshness, uncertainty and decision provenance.
- Enforced ownership boundaries: analysts measure; Brain reasons; portfolio/compliance qualify; executor executes; management manages; learning measures; governance authorizes and contains.
- Import and dependency checks that prevent hidden alternate decision authorities.

Acceptance:
- Repository-wide identity audit passes.
- Contract/schema and boundary tests pass.
- No route can create new exposure outside the canonical decision and shared execution path.

### System 2 — Market intelligence and evidence integrity
**Purpose:** provide the Brain with trustworthy, time-aligned, instrument-aware market context.

Deliverables:
- Capture immutable decision-time evidence snapshots and link them to decisions, accepted entry intents, broker fills and closed-trade outcomes.
- Record feature values and their original source timestamps; do not reconstruct decision features from later market data.
- Validate chronology, unique sample IDs, split separation, outcome horizons and untouched holdout integrity.
- Verify broker instrument economics, tick/point conversion, volume constraints, stop/freeze distances and trading mode.
- Implement broker-specific session calendars only when timezone, DST, holidays and per-instrument schedules have authoritative provenance.
- Calculate transaction costs from verified spread, commission, slippage assumptions and financing/swap data; represent unknown costs as unknown and reject net-EV qualification when required cost data is unavailable.
- Keep cross-timeframe and cross-asset comparisons timestamp-aligned and descriptive unless predictive validity is independently demonstrated.

Acceptance:
- Focused System 2 tests and `scripts/identity_audit.py` execute successfully in a supported runtime.
- Tests prove no look-ahead leakage and that decision snapshots are immutable and linked correctly through close.
- Each supported connector passes its own verified-specification tests; unsupported connectors remain blocked for new exposure.
- Session, cost and historical-validation gaps are either closed with evidence or listed as blockers; no unknown is represented as verified.
- No predictive-performance claim until chronological walk-forward and untouched holdout evaluation is complete.

### System 3 — Native market reasoning and hypothesis formation
**Purpose:** make the native Brain reason from evidence rather than follow a fixed strategy or external model.

Deliverables:
- Consolidate current evidence into a coherent market state with missingness, freshness and contradictions preserved.
- Generate competing hypotheses, supporting and opposing evidence, confidence, uncertainty, invalidation conditions and time horizon.
- Distinguish observed fact, inference, assumption and unknown.
- Add self-criticism and counter-hypothesis review before a decision.
- Ensure external AI is optional advice only; native reasoning continues if an external model is absent, rate-limited, wrong or unavailable.
- Remove duplicate or parallel components that can independently authorize direction.

Acceptance:
- Deterministic contract tests cover evidence disagreement, stale data, missing domains, conflicting hypotheses and advisor outage.
- No external model response can bypass or replace canonical Brain validation.
- Every actionable decision has auditable evidence lineage and explicit invalidation.

### System 4 — Opportunity discovery, qualification and payoff geometry
**Purpose:** identify defensible opportunities across market conditions without one mandatory strategy or zone.

Deliverables:
- Discover continuation, reversal, breakout, range, liquidity and other opportunities when supported by evidence; do not force a trade in every regime.
- Treat zones and indicators as evidence, not universal entry permission or direction authority.
- Produce Brain-owned entry reference, invalidation/stop, target, horizon, expected payoff and supporting evidence IDs.
- Validate geometry directionally and against current market structure; reject missing or internally inconsistent geometry.
- Apply confidence, probability, expected-value and cost qualification using the canonical thresholds and verified cost model.
- Keep flat/no-trade as a valid decision when evidence is insufficient.

Acceptance:
- Scenario tests cover trending, ranging, reversal, low-liquidity, conflicting and unfamiliar markets.
- Every accepted opportunity traces to a canonical decision and immutable evidence.
- No fixed indicator, strategy, timeframe, score or AI response independently triggers an order.

### System 5 — Compliance and portfolio capital allocation
**Purpose:** decide whether a qualified opportunity is permitted and how much capital it may use.

Deliverables:
- One compliance permit boundary for market status, spread, news restrictions, broker health, duplicate exposure, account limits, daily loss, drawdown, portfolio heat and position count.
- One portfolio sizing/allocation authority using verified balance/equity, instrument economics, stop distance, exposure, correlated positions and account silos.
- Correct volume-step rounding and minimum/maximum limits without rounding risk upward.
- Fail closed when account identity, open-book state, instrument spec or risk state is unresolved.
- Preserve the distinction between Brain conviction and risk allocation; confidence must not silently bypass portfolio limits.

Acceptance:
- Boundary tests cover all permit vetoes, missing-state behavior, concurrent entries and correlated exposure.
- Independent calculations reconcile proposed risk, account currency and broker lot constraints.
- No alternate sizing or compliance path can dispatch new exposure.

### System 6 — Execution integrity and broker adapters
**Purpose:** make the broker boundary deterministic, idempotent, auditable and connector-correct.

Deliverables:
- One serialized shared execution gateway for all opens, closes and modifications.
- Last-mile validation of canonical identity, decision lineage, quote freshness, order side, volume, price precision, stop/target geometry, minimum distance, trading mode, margin and connector-specific constraints.
- Idempotency and duplicate-order prevention; explicit acknowledgement, fill, partial-fill, rejection, timeout and reconciliation state machines.
- Separate MT5 and Deriv validators based on actual provider API semantics. Never treat a Deriv multiplier list as complete instrument economics.
- Retry only operations known to be safe; never blindly replay an ambiguous order submission.
- Broker truth remains authoritative for positions, orders and realized deals.

Acceptance:
- Contract tests cover every broker result and failure mode using fake adapters.
- No open bypasses the shared gateway; invalid or ambiguous requests are rejected or quarantined.
- Broker reconciliation handles partial failures and restart recovery without double-booking or duplicate orders.
- Live-order testing remains disabled during engineering.

### System 7 — Position operations and trade lifecycle
**Purpose:** manage open risk continuously without corrupting state or violating the original thesis.

Deliverables:
- Track each position to its originating decision, opportunity, campaign, entry snapshot and broker ticket.
- Re-evaluate thesis health as new evidence arrives while respecting closed-bar/freshness constraints.
- Manage stop tightening, target changes, partial closes, full closes and reversals through the same execution gateway.
- Never widen risk through a protective action; preserve close/exit capability when cognition or external services fail.
- Reconcile manual/external closes, broker-side stop/target exits, partial fills and unclean shutdowns.
- Make lifecycle transitions idempotent and recoverable.

Acceptance:
- State-machine tests cover entry through close, partial close, failed modify, timeout, manual close, restart and reconciliation.
- Event ledger, local state and broker truth reconcile without phantom positions or duplicate outcome booking.
- Position management never depends on a fresh entry signal to protect existing capital.

### System 8 — Learning, calibration and statistical validation
**Purpose:** measure what works without allowing contaminated evidence to rewrite live behavior.

Deliverables:
- Store immutable trade outcomes and link them to the exact decision-time features, costs, execution conditions and exit reasons.
- Separate descriptive reporting, calibration, hypothesis testing and live parameter recommendations.
- Use chronological walk-forward, purged/embargoed splits where outcome horizons overlap, multiple-testing controls and an untouched holdout.
- Report sample size, confidence intervals, regime/market coverage, drawdown, tail loss, costs and uncertainty.
- Keep learning observational by default. Parameter changes require explicit governance, validation and rollback.
- Do not label a learner predictive merely because it has trade outcomes or in-sample fit.

Acceptance:
- Data-leakage and split-contamination tests pass.
- Reproducing a validation run from its dataset, code version and configuration is possible.
- No learner mutates live thresholds or execution behavior without approved, versioned promotion.

### System 9 — Governance, safety containment and observability
**Purpose:** keep the system accountable, diagnosable and recoverable during faults.

Deliverables:
- Health monitoring for market feeds, broker connectors, decision loop, event queues, persistence, execution latency and reconciliation.
- Explicit degraded modes and circuit breakers for stale feeds, broker outage, repeated rejects, state divergence and loss limits.
- Durable audit trail linking evidence, decision, permit, sizing, order request, broker response, position lifecycle and outcome.
- Secrets/configuration hygiene; redact account identifiers and credentials; validate startup configuration without logging secrets.
- Recovery procedures for unclean shutdown, database inconsistency and reconnect/reconciliation.
- Alerts distinguish warning, degraded, blocked, halted and recovered states with actionable reasons.

Acceptance:
- Fault-injection tests cover connector outages, stale data, storage failure, queue overload, process restart and mismatched broker state.
- Safety halts persist and recover according to explicit policy; unknown state never silently becomes healthy.
- Operational logs provide end-to-end traceability without secrets or fabricated status.

### System 10 — Integration, cleanup and release readiness
**Purpose:** prove the whole repository behaves as one system, not a collection of individually passing modules.

Deliverables:
- Remove dead, duplicate, contradictory and unreachable code only after call-site and behavior audits.
- Verify configuration defaults, environment parsing, database schemas/migrations, seed data and runtime paths; preserve actual secrets and live settings unless explicitly authorized.
- Run import/compile checks, lint, focused tests, full test suite where dependencies permit, identity audit and static searches for alternate order paths.
- Execute deterministic end-to-end simulations from evidence → decision → permit → sizing → intent → broker acknowledgement → position management → close → outcome.
- Publish a final implementation matrix: implemented, tested, partially implemented, blocked, unsupported, and not implemented.
- Document deployment/rollback, supported broker/instrument matrix, known limitations and manual acceptance checklist.

Acceptance:
- All critical paths have reproducible test evidence and no unresolved identity or safety bypass.
- All remaining failures are triaged with root cause and severity; no failures are hidden by weakening assertions.
- No live-trading release is declared safe based only on static review or a green subset of tests.
- Release authorization is a separate, explicit decision after all engineering gates pass.

## Dependency order

```text
System 1: identity/contracts
        ↓
System 2: market evidence integrity
        ↓
System 3: native reasoning
        ↓
System 4: opportunity + geometry
        ↓
System 5: compliance + portfolio
        ↓
System 6: shared execution + broker adapters
        ↓
System 7: position lifecycle
        ↓
System 8: learning + statistical validation
        ↓
System 9: governance + recovery
        ↓
System 10: integration + release readiness
```

Some work can be audited in parallel, but integration and acceptance follow these dependencies. A downstream system may be reviewed early, but it must not be declared accepted while its upstream contracts are unverified.

## Current baseline and immediate priority

- System 2 is merged into `main` via merge commit `475ad4b0c3214d2a6a737cf6526e13ccaeeb50cd`, but the focused tests and `scripts/identity_audit.py` were not run at the time of the merge.
- The known System 2 blockers are decision-time feature snapshots linked to trades; complete broker timezone/DST/holiday calendars; full verified constraints for each supported connector; realistic financing costs; and independent walk-forward/untouched-holdout results.
- First task: close System 2 with source changes, focused tests and an auditable validation report. Do not start implementation work for System 3 until the System 2 acceptance gate is met.
- No broker orders are to be sent during engineering. Do not access or change actual `.env`, runtime databases or broker settings without explicit authorization.

## Per-system completion record

For every system, record: branch/PR and merge SHA; changed files; acceptance criteria; commands/tests actually run; pass/fail counts; identity-audit result; manual review performed; known gaps; safety implications; and explicit acceptance decision. If execution could not be performed, state that directly and leave the gate open.

## System 2 progress update — 2026-10-30

PR #792 merged to main at 74e0b845390cebe8385b2812003da8344b59e0cc. It implements decision-time feature capture in both Brain origination paths, snapshot propagation through campaign translation and the shared OPEN intent, an execution-boundary snapshot gate, and ticket-linked entry/outcome events. The implementation remains NOT ACCEPTED: focused tests and scripts/identity_audit.py have not been executed, exact-ticket restart recovery has not been integration-tested, and calendar, connector, financing and independent historical-validation gates remain open. Follow-up branch redesign/system2-durable-provenance adds synchronous commits for entry/outcome provenance, exact parent-ticket querying and detached deep copies of snapshots into intents. Its tests are added but not executed. Do not start System 3 before System 2 acceptance.

## Roadmap progress register (updated 2026-10-30)

| System | Status | Acceptance evidence |
|---|---|---|
| 1. Canonical identity and contracts | **Foundation present; audit required** | Canonical identity and contract code exist; current whole-repository conformance still requires a fresh executed audit. |
| 2. Market intelligence and evidence integrity | **IN PROGRESS — BLOCKED** | PR #792 merged at 74e0b845; follow-up branch redesign/system2-durable-provenance adds durable provenance commits and exact-ticket recovery lookup. Tests/audit remain unexecuted. Calendar, Deriv constraints, verified financing estimates and independent historical validation remain open. |
| 3. Native market reasoning | **NOT STARTED — gated on System 2** | No acceptance evidence recorded. |
| 4. Opportunity discovery and payoff geometry | **NOT STARTED — gated on System 3** | No acceptance evidence recorded. Existing code must be audited when reached. |
| 5. Compliance and portfolio allocation | **NOT STARTED — gated on System 4** | No acceptance evidence recorded. Existing code must be audited when reached. |
| 6. Execution integrity and broker adapters | **NOT STARTED — gated on System 5** | Some shared executor protections already exist; system-wide acceptance is not recorded. |
| 7. Position operations and trade lifecycle | **NOT STARTED — gated on System 6** | No end-to-end lifecycle acceptance evidence recorded. |
| 8. Learning and statistical validation | **NOT STARTED — gated on System 7** | Validation guards exist; no verified point-in-time dataset or independent performance evidence recorded. |
| 9. Governance, observability and recovery | **NOT STARTED — gated on System 8** | No full fault-injection/recovery acceptance evidence recorded. |
| 10. Integration and release readiness | **NOT STARTED — gated on Systems 1–9** | No final integrated acceptance decision. |

A status can move to **COMPLETE** only when the acceptance evidence is linked in the table or accompanying audit, all critical tests have actually run, and unresolved limitations are either closed or explicitly accepted by the owner. A merge alone never changes the status to COMPLETE.



### System 2 durability follow-up — 2026-10-30

- PR #793 (draft, not accepted): redesign/system2-durable-provenance, based on merge commit 74e0b845390cebe8385b2812003da8344b59e0cc.
- Critical entry-snapshot and trade-outcome events now use a synchronous SQLite commit path (emit(..., durable=True)) instead of relying on the best-effort telemetry queue. A database failure raises to the producer and marks the store degraded.
- Event queries accept an exact parent_id filter; restart recovery now queries by the broker ticket instead of scanning up to 5,000 same-symbol events and filtering in Python.
- Intent.open() deep-copies the nested snapshot payload to detach it from the caller's mutable source object. This is defensive copying, not a claim that the nested intent payload is intrinsically immutable.
- DecisionPackage now records a precise created_epoch; Brain snapshots use it as the source-time cutoff rather than sampling time.time() after the decision. Missing/invalid decision timestamps produce incomplete snapshots. Added regression tests for post-decision evidence and missing chronology.
- Added EventStore tests for commit-before-return and exact ticket filtering. These tests have not been executed. Callback-level entry-to-close recovery, ticket-shape compatibility, database failure injection, identity audit, calendar/connector/cost and independent historical-validation gates remain open.



## System 2 acceptance follow-up — 2026-10-30

- PR #793 is merged. Its durable entry/outcome provenance, exact broker-ticket query, decision-time evidence cutoff, detached intent snapshot, and callback-level restart-recovery test are now on `main`. Merge is not acceptance.
- PR #795 (draft): `redesign/system2-provenance-hardening`. It adds a fail-stop gate for subsequent OPENs after EventStore degradation and corrects MT5 partial-close realized-cost attribution: all position deals contribute profit/commission/swap/fees, while the latest exit deal supplies final exit reason/price/time.
- System 2 item 1 (inherited test failures): several concrete baseline defects have been corrected across PR #793 and #795; the latest full-suite result must be reviewed before declaring the inherited failure set closed. Do not weaken safety assertions or suppress the silent-handler audit.
- System 2 item 2 (entry-to-close restart recovery): callback-level test now exercises a durable entry event, clears in-memory entry context, recovers by exact ticket after restart, and checks the durable outcome link. It still requires a passing supported-runtime CI run.
- System 2 item 3 (calendar and connector constraints): the implementation requirement is broker-agnostic and capability-driven. Each connector must supply its own verified trading sessions, server timezone/DST rules, holiday schedule, instrument constraints and source/validity metadata through a common adapter contract. No broker is hardcoded or assumed; missing required capabilities fail closed for that instrument. Deriv remains blocked until its adapter-specific specification and constraint validator are complete.
- System 2 item 4 (financing): unknown directional rates and invalid estimate inputs now return unavailable rather than modeled zero. MT5 realized swap is taken from broker deal history and summed across all position deals; partial-close exit attribution uses the latest closing deal. DealCloseInfo now represents missing commission/swap/fee values as `None`, including Deriv responses that do not report those costs, so the outcome ledger does not fabricate zero financing. The repository currently has no `data/swap_rates.json`, so no static financing estimate is available by default. Missing/invalid side-specific rates return unavailable rather than modeled zero. Static rate-table estimates remain estimates and are not broker-verified until the table carries current, authoritative broker rates and rollover rules.
- System 2 item 5 (independent historical walk-forward and untouched holdout) is reserved for the repository owner and has not been performed here.
- System 2 remains **IN PROGRESS — NOT ACCEPTED**. System 3 engineering is proceeding in parallel as directed; this does not waive System 2 release gates. System 2 still requires full CI, resolution of inherited audit findings, the broker-agnostic connector capability contract, verified financing data and all other acceptance evidence.



## System 3 implementation record — 2026-10-30

**Status: IN PROGRESS — implementation branch `redesign/system3-native-reasoning`.** This is parallel downstream engineering requested by the repository owner; it does not retroactively accept System 2 or waive its outstanding gates.

### Native reasoning changes in this pass
- Native analysis now records a point-in-time evidence index, original source IDs/timestamps/domains, raw observed measurements, pattern-to-source lineage, and separate observed-fact / inference / assumption / unknown collections.
- Every native hypothesis carries a stable ID, explicit hypothesis family, supporting and contradicting evidence IDs, assumptions, unresolved unknowns, distinguishing evidence, source-declared horizon, and decomposed reasoning-strength metadata. Pattern links explicitly disclose domain-level candidate-source granularity; unfamiliar measurement hypotheses retain exact source IDs.
- Confidence is explicitly marked as uncalibrated reasoning strength, not a probability. No predictive or profitability claim is inferred from this score.
- Counter-hypotheses now challenge up to the three strongest directional explanations, each linked to the primary hypothesis ID; when no directional explanation exists, the strongest market-state interpretation is still challenged. Self-criticism identifies counter-hypotheses by type, penalizes missing attributable support, and challenges hypotheses when independent sources conflict.
- The leading-hypothesis selector compares non-directional and no-trade explanations against directional candidates; direction no longer receives automatic priority.
- Relevant analytical lenses from the complete strategy-lens registry now produce separate, non-directional hypothesis candidates with their required evidence, source lineage and limitations. Volatility-regime and unfamiliar-measurement hypotheses are also retained without inventing direction.
- Conflicting displacement sources, order-flow signs, same-timeframe trend readings, momentum states, volatility states, statistical z-scores and M1 interpretations remain explicitly conflicted instead of being collapsed by input/arrival order.
- Native evidence freshness uses the same decision-time cutoff as market-state consolidation.
- External advisor integration remains downstream of native analysis. Provider outage does not disable native reasoning; advisor outputs remain subject to Brain synthesis, opportunity geometry, cost and admission checks.

### New deterministic coverage
- Evidence lineage and epistemic-category serialization.
- Opposing structure/order-flow sources.
- Conflicting same-timeframe trend readings.
- Conflicting momentum and volatility states.
- Uncalibrated confidence semantics and hypothesis IDs.
- No-trade hypothesis comparison against directional hypotheses.
- Stale/future evidence exclusion at the exact decision-time cutoff.
- Existing native advisor-outage and native-only origination tests remain part of the supported-runtime suite.

### Acceptance still required
- Run focused System 3, cognition, provider-resilience, constitutional and identity-audit suites on Python 3.11 and 3.12.
- Run full repository CI; classify failures without suppressing assertions or weakening safety.
- Review all native hypothesis families against adversarial and unfamiliar market-state scenarios.
- Prove the advisor cannot bypass native validation and verify qualified opportunities continue through the common admission path.
- Keep unknown time horizons, missing domains and unverified facts explicit; no strategy-family catalog is a guarantee of competence.

No live broker orders, runtime database changes, credential changes, `.env` access or destructive cleanup are part of this work.
