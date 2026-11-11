# Apex Trader — Full Redesign Roadmap and Acceptance Gates

**Status:** living planning baseline; implementation status must be reconciled against current mainline commits. This document is not a completion or production-acceptance claim.  
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
- Generate competing hypotheses with supporting and opposing evidence, decomposed reasoning-strength metadata explicitly marked uncalibrated (not a probability), uncertainty, invalidation conditions and time horizon.
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
- Use a producer-issued, direction/family/horizon-scoped calibration artifact for probability only when the Brain resolves and verifies its provenance against the exact symbol, side, opportunity family and horizon; otherwise probability remains unknown.
- Qualify directional opportunities using producer-verified fixed-horizon expected return and fresh verified execution costs, plus structural geometry, liquidity, duration and portfolio context. Never substitute uncalibrated reasoning strength for win probability or expected return.
- Do not use reasoning confidence to authorize, veto, reverse, exit or scale a campaign. Reasoning strength is descriptive metadata only: it is not a probability, expected-return input, qualification gate or position-sizing input.
- Keep flat/no-trade as a valid decision when evidence is insufficient.

Acceptance:
- Scenario tests cover trending, ranging, reversal, low-liquidity, conflicting and unfamiliar markets.
- Every accepted opportunity traces to a canonical decision and immutable evidence.
- No uncalibrated confidence score, fixed indicator, strategy, timeframe, arbitrary score or AI response independently triggers or vetoes an order.
- Missing calibration remains unknown; directional origination must fail its producer-verified expected-return/net-cost qualification rather than inventing a probability.

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

- **System 2 remains IMPLEMENTED / INTEGRATED but NOT ACCEPTED.** Its open gates remain decision-time feature snapshots linked through broker close, authoritative connector specifications and calendars, verified financing costs, and independent chronological walk-forward plus untouched-holdout evidence. These must not be inferred from downstream implementation or a green unit suite.
- **System 3 implementation is merged but NOT ACCEPTED** (PR #796, commit `94a01c793d7418357f81adf7c8989f965c2c3e5b`). **System 4 implementation is merged but NOT ACCEPTED** (PR #797, merge commit `b141881641746f9f64e94cbe246d59684bb3c3f5`). Their implementation records below retain their separate validation and data gates.
- Later-system work may be implemented or audited in parallel only where it does not assume unaccepted upstream evidence. No downstream system may be marked accepted while System 2 and its required dependencies remain open.
- **Completed engineering increment (2026-11-11):** PR #902, exact-ticket Brain management and campaign lineage, merged to `main` at `c3d2f1c1a900a70f6c1baecee77b7c74304e6481` (PR head `2159f443f49bb8e2eb541e3d6cdfcf484c0b5f4c`). Its implementation covers exact broker identity, durable management idempotency, campaign multi-ticket lifecycle correctness, confidence isolation from management/economic ranking, retirement of the no-op breakeven re-entry path, production hard-disable of discretionary mechanical exits, and removal of disconnected cross-instrument heuristics and uninitialized queue controls. Validation recorded by the merge commit: 4,485 tests passed on Python 3.11 and 3.12; lint, identity/safety audit, generated concept coverage, imports, Gold Desk and frontend build passed. This is implementation validation, not ten-system or live-broker acceptance.
- No broker orders are to be sent during engineering. Do not access or change actual `.env`, runtime databases, credentials, account selection or broker settings.

## Current mainline reconciliation — 2026-11-11

- Mainline is `c3d2f1c1a900a70f6c1baecee77b7c74304e6481`, the merge commit for PR #902. The earlier PR #902 open/unmerged statements in historical records are superseded.
- PR #902 final validated head: `2159f443f49bb8e2eb541e3d6cdfcf484c0b5f4c`. Merge-recorded validation: Python 3.11 and 3.12 each 4,485 passed; lint, generated concept-coverage verification, canonical identity/safety audit, import checks, Gold Desk and frontend build passed.
- No current open PR was returned by the repository's open-PR listing at this checkpoint. No live-broker acceptance is claimed; no live orders were sent.
- **Immediate priority remains System 2 acceptance**, not declaring downstream systems complete: decision-time snapshot-to-close lineage and restart recovery, authoritative broker/connector calendars and instrument capabilities, verified financing costs, inherited silent-exception audit, and independent chronological walk-forward plus untouched-holdout evidence. The dedicated `apex-trader-data` repository remains an owner setup dependency; its topology guard must not be weakened.
- Systems 3 and 4 remain implemented/integrated but not accepted. PR #902 advances Systems 1, 3, 4, 5, 6, 7 and 10 but does not satisfy their complete system-level acceptance gates. Systems 5–10 require explicit source audits and acceptance evidence; do not infer completion from the PR scope.

## Concurrent engineering reconciliation — 2026-11-11

**Current mainline:** `c3d2f1c1a900a70f6c1baecee77b7c74304e6481` (PR #902 merge).
**Active implementation:** PR #904, `engineering/systems2-10-concurrent-acceptance-20261111`. Its latest implementation is not merged and its full CI matrix is still running. Do not treat the entries below as accepted until CI and the remaining owner-host gates pass.

### System 2 — provenance, data lineage and restart recovery
- Final OPEN admission now requires a one-to-one, duplicate-free mapping between the snapshot evidence IDs and feature rows. Every feature must be source-timestamp verified, non-provisional, and match the exact intent symbol. Rejection tests cover unverified, provisional, cross-symbol and duplicate rows.
- Provider session observations now drive the session state instead of being exposed only as side metadata. Acceptance requires complete provider provenance, canonical instrument identity, receipt/publication/revision timestamps no later than the analysis cutoff, a valid timezone, session identity, and explicit DST, holiday and tradeability status.
- Decision-to-trade recovery test now closes and reopens the SQLite event store and constructs a new bootstrap object before recovering the exact-ticket entry snapshot and linking its close outcome.
- Calibration candidate/outcome replay now rejects future-dated outcomes relative to the producer's explicit as-of cutoff and rejects feature rows missing source module, domain, data basis or measurements.
- The read-only broker capability audit now treats source-time verification, venue timezone/holiday calendar, broker swap-term completeness and source-attested account-currency cash-financing projection as separate acceptance checks. A live `symbol_info` response alone cannot certify these capabilities.
- **Still data-gated:** independently authenticated source manifests, broker-history samples, verified full-history coverage, independent holdout and walk-forward evidence, a provider-sourced timezone/holiday calendar, and a source-attested position-horizon cash-financing model. Current MT5 `symbol_info` supplies raw swap terms but not an authoritative spec timestamp/timezone/holiday source or cash projection; Deriv exposes contract constraints but no verified financing schedule.

### System 5 — compliance and portfolio allocation
- PR #904 adds a durable portfolio admission reservation ledger for Brain-approved but not-yet-broker-confirmed OPEN intents.
- Origination check→size→reserve→submit is serialized. Reservation-ledger lazy initialization is itself guarded so concurrent first-use cannot create separate locks/ledgers. Reservations participate in duplicate-pair and max-open-position checks, account heat, daily-loss remaining-room calculations, and portfolio pair/broker concentration plus correlation sizing.
- Reservations survive event-store restart, link to broker pending-order IDs, release on broker-confirmed position visibility or explicit rejection, and release after broker-confirmed terminal pending-order reconciliation. Ambiguous broker outcomes retain the reservation.
- **Still not accepted:** full supported-account owner-limit matrix, concurrency stress, portfolio correlation/whole-book fault injection, and owner-connected evidence.

### System 6 — execution integrity and broker mutation boundaries
- The static broker mutation audit now covers entry, pending entry, close, modify and cancel operations rather than entry calls alone.
- Broker position snapshots retain per-connector identity. The execution lifecycle maps MT5 tickets to `mt5_<connector_index>` so close, partial-close and SL/TP mutation use the connector that actually owns the ticket; generic `mt5` routing is no longer used for these management actions.
- Final OPEN validation rejects unverified, provisional, cross-symbol, duplicate or unreferenced decision features before the broker call.
- **Still not accepted:** complete per-adapter order/modify/close/cancel mutation matrix, acknowledgement ambiguity, reconnect, restart and broker-history reconciliation across every supported platform.

### System 7 — position lifecycle and reconciliation
- A broker-cancelled pending order that still carries a broker position ID is no longer discarded merely because the pending order is terminal. A partial fill is also retained when the broker has not yet supplied a position ID: release now requires explicit broker volume evidence that the order was wholly unfilled, or exact fill attribution.
- A durable reservation linked to a pending order is released only after terminal broker reconciliation; unresolved partial fills retain both lineage and reserved capacity.
- **Still not accepted:** full market/pending/partial-fill/manual-close/partial-close/restart/reconciliation matrix against owner broker history.

### System 8 — learning and statistical validation
- Calibration producer now enforces the explicit as-of cutoff on both outcome observation and outcome quote timestamps, and requires source module, domain, data basis and non-empty measurements for every point-in-time feature.
- The chronological 80/20 development/holdout cut is now persisted as a durable event-sourced manifest per symbol/direction/family/horizon. The manifest freezes its dataset digest and start/end epochs; late historical additions or corrections invalidate the manifest, while post-cutoff data cannot silently roll the holdout forward. Artifacts carry the manifest key and holdout window.
- Calibration outputs remain historical fixed-horizon summaries, not calibrated probabilities or demonstrated current-regime edge.
- **Still not accepted:** independently authenticated data manifests, complete selection coverage on real provider history, immutable untouched holdout, multiple-testing controls and independent walk-forward reproduction.

### System 9 — governance and observability
- Existing silent-exception AST audit remains a mandatory CI gate. The broker mutation audit is broadened to management as well as entry routes.
- **Still not accepted:** complete fault-injection evidence for event-store degradation, queue failures, broker ambiguity, clock skew, restart and partial storage failure across all systems.

### System 10 — integrated acceptance
- PR #904 currently integrates Systems 2, 5, 6 and 7 corrections with regression tests; its CI matrix is pending.
- No production or live-broker acceptance is claimed. No live broker orders were sent.
- Final release gate remains the integrated ten-system matrix, canonical identity/legacy-path audit, supported broker validation, owner-host data-capability report and documented owner approval.

## Per-system completion record

For every system, record: branch/PR and merge SHA; changed files; acceptance criteria; commands/tests actually run; pass/fail counts; identity-audit result; manual review performed; known gaps; safety implications; and explicit acceptance decision. If execution could not be performed, state that directly and leave the gate open.

## System 2 progress update — 2026-10-30

PR #792 merged to main at 74e0b845390cebe8385b2812003da8344b59e0cc. It implements decision-time feature capture in both Brain origination paths, snapshot propagation through campaign translation and the shared OPEN intent, an execution-boundary snapshot gate, and ticket-linked entry/outcome events. The implementation remains NOT ACCEPTED: focused tests and scripts/identity_audit.py have not been executed, exact-ticket restart recovery has not been integration-tested, and calendar, connector, financing and independent historical-validation gates remain open. Follow-up branch redesign/system2-durable-provenance adds synchronous commits for entry/outcome provenance, exact parent-ticket querying and detached deep copies of snapshots into intents. Its tests are added but not executed. Do not start System 3 before System 2 acceptance.

## Historical roadmap progress register (2026-10-30; superseded by the 2026-11-11 mainline reconciliation below)

| System | Status | Acceptance evidence |
|---|---|---|
| 1. Canonical identity and contracts | **Foundation present; audit required** | Canonical identity and contract code exist; current whole-repository conformance still requires a fresh executed audit. |
| 2. Market intelligence and evidence integrity | **IN PROGRESS — NOT ACCEPTED** | PR #793 merged; durable entry/outcome provenance and exact-ticket recovery are on main. Full acceptance remains open: inherited silent-exception audit, broker-agnostic calendar/connector capabilities, verified financing and independent historical validation. |
| 3. Native market reasoning | **IMPLEMENTED AND MERGED — ACCEPTANCE GATES TRACKED** | PR #796 merged at 94a01c7. Python 3.11/3.12 each reported 3,963 passed and the same inherited 180-handler audit failure as the pre-merge baseline. No new test regression in the baseline comparison. |
| 4. Opportunity discovery and payoff geometry | **IMPLEMENTATION MERGED — NOT ACCEPTED** | PR #797 merged to `main` at `b141881`. Geometry and qualification changes are integrated. Lint and Gold desk Python 3.11/3.12 pass; full suites each report 3,990 passed and one inherited failure: 180 unclassified silent exception handlers, identical to the pre-merge `main` baseline. Acceptance remains blocked on the durable selection-aware outcome/calibration producer, producer attestation, and full identity/adversarial audit. |
| 5. Compliance and portfolio allocation | **PARTIALLY IMPLEMENTED / INTEGRATED — NOT ACCEPTED** | PR #902 changes management/campaign allocation boundaries; complete permit, sizing, concurrency and correlated-exposure acceptance remains unproven. |
| 6. Execution integrity and broker adapters | **PARTIALLY IMPLEMENTED / INTEGRATED — NOT ACCEPTED** | Exact-ticket management dispatch and durable idempotency are integrated by PR #902; full broker adapter, acknowledgement, restart and reconciliation matrix remains open. |
| 7. Position operations and trade lifecycle | **PARTIALLY IMPLEMENTED / INTEGRATED — NOT ACCEPTED** | Campaign multi-ticket lineage, exact close binding and management lifecycle hardening are integrated; complete entry-to-close/manual-close/restart acceptance remains open. |
| 8. Learning and statistical validation | **IMPLEMENTED COMPONENTS / DATA-GATED — NOT ACCEPTED** | Snapshot and outcome provenance, quote provenance and calibration safeguards exist; verified independent walk-forward/untouched-holdout evidence remains absent. |
| 9. Governance, observability and recovery | **PARTIALLY IMPLEMENTED — NOT ACCEPTED** | Safety/degradation, event durability and reconciliation controls exist; full fault-injection, restart, storage/queue failure and operational acceptance remains open. |
| 10. Integration and release readiness | **IN PROGRESS — NOT ACCEPTED** | PR #902 integrated and validated on main; full ten-system matrix, supported-broker acceptance, owner-host evidence and release review remain outstanding. |

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

**Status: IMPLEMENTED AND MERGED — PR #796, commit `94a01c793d7418357f81adf7c8989f965c2c3e5b`, 2026-10-30.** System 3 implementation is on `main`. This does not retroactively accept System 2 or waive its separate broker-capability, financing and historical-validation gates.

### Native reasoning changes in this pass
- Native analysis now records a point-in-time evidence index, original source IDs/timestamps/domains, raw observed measurements, pattern-to-source lineage, and separate observed-fact / inference / assumption / unknown collections.
- Every native hypothesis carries a stable ID, explicit hypothesis family, supporting and contradicting evidence IDs, assumptions, unresolved unknowns, distinguishing evidence, source-declared horizon, and decomposed reasoning-strength metadata. Pattern links explicitly disclose domain-level candidate-source granularity; unfamiliar measurement hypotheses retain exact source IDs.
- Confidence is explicitly marked as uncalibrated reasoning strength, not a probability. No predictive or profitability claim is inferred from this score.
- Counter-hypotheses now challenge up to the three strongest directional explanations, each linked to the primary hypothesis ID; when no directional explanation exists, the strongest market-state interpretation is still challenged. Self-criticism identifies counter-hypotheses by type, penalizes missing attributable support, and challenges hypotheses when independent sources conflict.
- The leading-hypothesis selector compares non-directional and no-trade explanations against directional candidates; direction no longer receives automatic priority.
- Counter-hypotheses are challenge records only and are excluded from primary direction selection; a weak primary cannot be replaced by its own generated counter merely because the counter score is larger.
- Market coverage is measured from informative observations in the six core market domains. Empty high-confidence telemetry, advisor metadata, portfolio data and execution metadata cannot inflate market-evidence completeness.
- Relevant analytical lenses from the complete strategy-lens registry now produce separate, non-directional hypothesis candidates with their required evidence, source lineage and limitations. Volatility-regime and unfamiliar-measurement hypotheses are also retained without inventing direction.
- Conflicting displacement sources, order-flow signs, same-timeframe trend readings, momentum states, volatility states, statistical z-scores and M1 interpretations remain explicitly conflicted instead of being collapsed by input/arrival order.
- Native evidence freshness uses the same decision-time cutoff as market-state consolidation. Native-only origination, verified execution-cost evidence and opportunity geometry now use that identical cutoff as well.
- Native open and observe outcomes retain full hypothesis records, the selected hypothesis ID and the epistemic record; sparse or rejected decisions do not discard the reasoning that led to standing down.
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

### Validation result and remaining global blocker
- Focused Gold desk tests passed on the final System 3 revision.
- Full Python 3.11 and 3.12 suites each reported **3,963 passed, 1 failed**. The only failure is `tests/test_silent_swallow_observability.py::test_no_silent_exception_handlers`, which reports 180 unclassified handlers.
- The pre-merge `main` baseline at `590db018c4ea414a3122be3948e90585558c27ee` reported **3,948 passed, 1 failed** on both runtimes, with the same 180-handler audit failure. System 3 added 15 passing tests and introduced no new failing test in that comparison.
- Lint passed. The silent-exception audit was not suppressed, weakened or reclassified to obtain a green result.
- The post-merge main workflow is queued; its result remains to be checked.
- Unknown time horizons, missing domains and unverified facts remain explicit. No strategy-family catalog is a guarantee of competence.

No live broker orders, runtime database changes, credential changes, `.env` access or destructive cleanup are part of this work.



## System 4 implementation record — 2026-10-30

**Status: IMPLEMENTATION MERGED — NOT ACCEPTED.** PR #797 was merged by the repository owner into `main` at `b141881641746f9f64e94cbe246d59684bb3c3f5` on 2026-10-31. Merge is integration, not System 4 acceptance.

### Existing architecture audit
- `cognition/opportunity_geometry.py` already derived entry/stop/target from fresh market-lens structural observations and supported breakout, trend/momentum, range rotation and sweep/reclaim activation. The audit found activation could be borrowed from any still-fresh older structure, momentum was not timeframe-aligned, missing evidence symbols inherited the state symbol, sweep-side labels could qualify without a measured reclaim, and same-time structural conflicts could be resolved by arrival order.
- `CognitiveBrain` already built a ranked opportunity set and used market-derived geometry for the preferred directional opportunity, but conditional directional alternatives did not each receive their own geometry.
- The legacy `cognition.expected_value` path interpreted confidence as win probability and is retired. The execution-side `qualify_net_ev` contract now accepts producer-verified fixed-horizon expected return in R, the account-currency value of 1R for the exact size, and fresh broker-truth round-trip costs; it accepts no confidence or inferred probability.

### Implemented in PR #797
- Geometry requires exact instrument identity, declared timeframe, `data_basis=completed_ohlc`, and the original `source_bar_epoch`. Source-bar age is checked at the decision-time cutoff independently of evidence arrival time; late-arriving stale bars and future bars are rejected. Explicitly provisional/incomplete observations are excluded.
- The latest structural observation alone can establish breakout/trend activation; same-time/same-timeframe incompatible structure readings fail closed.
- Trend/momentum and range-response activation require matching timeframes and non-future evidence.
- Sweep/reversal activation requires a counter-side sweep, explicit reclaim confirmation, measured swept level and a completed close back across that level.
- Every directional opportunity receives its own independently derived entry, stop, target, risk/reward distances and supporting geometry evidence IDs.
- Advisor-reported and native reasoning strength remain uncalibrated descriptive metadata only. They are not win probabilities, expected-return estimates, eligibility inputs or sizing factors. Action qualification uses a producer-verified fixed-horizon expected-return artifact with exact-symbol, direction, family and horizon provenance, verified source, walk-forward/holdout controls, leakage/selection/overfit checks, sample sufficiency and bounded development/holdout return gap.
- Without verified fixed-horizon expected-return provenance, EV is unavailable, canonical `DecisionPackage.expected_value` remains zero, and directional exposure is blocked in both advisor-assisted and native-only origination. Fresh execution-cost qualification also requires exact symbol identity and attributable broker/source lineage. Reasoning-strength calibration diagnostics are observational and excluded from independent market-domain coverage.
- Provisional exposure is derived from verified net expected return and payoff geometry. Final executable volume is independently constrained by shared portfolio sizing, broker economics, whole-book risk and owner limits; confidence and calibration-reliability metrics do not scale it.

### Deterministic tests added
- Older breakout cannot activate a newer non-breakout.
- Cross-timeframe momentum cannot confirm a structure trend.
- Same-time structural conflicts and missing instrument identity fail closed.
- Reversal sweep requires measured reclaim; sweep without reclaim does not activate.
- Unfamiliar-only / low-liquidity evidence cannot manufacture geometry.
- Uncalibrated confidence is rejected as a probability, expected-return, qualification or sizing input. Wrong-symbol/direction/family/horizon artifacts, unverified source lineage, insufficient samples and invalid development/holdout return evidence are rejected.
- Calibration metadata does not inflate market-evidence coverage.

### Outstanding acceptance gates
- The runtime calibration producer is not yet demonstrated end-to-end from durable decision/outcome history into direction-specific, symbol-scoped calibration evidence. Until that producer and its provenance are integrated, new directional exposure remains blocked by design.
- Focused Gold desk tests and lint pass on Python 3.11/3.12. Full Python 3.11/3.12 suites each report 3,990 passed and one failure: the repository-wide silent-exception audit finds 180 unclassified handlers. The pre-merge `main` baseline had the same 180-handler failure; this is inherited, not introduced by PR #797. The full identity audit did not run after the test step failed; adversarial trend/range/reversal/low-liquidity/conflict/unfamiliar coverage still requires a separate full audit.
- Verify every opportunity's stable identity, lifecycle, geometry lineage and qualification status through the canonical decision and downstream campaign record.
- Verify opportunity discovery across brokers and instruments without broker-specific assumptions; this remains dependent on System 2 connector capability gates.

No broker orders, runtime database changes, credential changes, `.env` access or destructive cleanup were performed in this implementation pass.

### Calibration-producer baseline audit — 2026-10-30 (superseded by the 2026-10-31 branch record below)
- `CampaignMemoryStore.find_analogues()` queries only completed campaigns (`closed=1`), so it is a selected-execution sample. It excludes rejected, deferred and conditional opportunities. Turning its win ratio into a calibrated probability would preserve the selection bias that the calibration gate explicitly requires us to test for.
- The durable `DECISION_TRADE_ENTRY_SNAPSHOT` / `DECISION_TRADE_OUTCOME` pair records opened trades and realized P&L, but the outcome currently marks predictive validity as unvalidated and false. It does not provide outcomes for the full candidate universe or a verified independent holdout.
- The existing `CalibrationEngine` learns instrument characteristics (ATR, spread, zone/structure outcomes); it is not a direction-specific win-probability calibrator and does not establish walk-forward/untouched-holdout predictive validity.
- At the time of this baseline audit, no production dataset or complete producer existed to satisfy the calibration contract. The concurrent implementation record below documents the subsequent durable candidate ledger, fixed-horizon outcome producer, family-scoped artifacts and producer-owned registry. That implementation remains unaccepted until runtime validation proves the full path.
- At the time of this baseline audit, the Brain still relied on caller-declared calibration metadata. The branch implementation below replaces that path with exact producer-registry resolution bound to symbol, side, candidate family, horizon and validation metadata. A source-verification boolean alone remains insufficient.



## Concurrent System 1 + System 4 implementation record — 2026-10-31

**Working branch:** `redesign/system1-system4-unified-intelligence`  
**Pull request:** [#798](https://github.com/Eugine336/apex-trader/pull/798) — draft, not merged.  
**Status:** substantial implementation in progress; **neither system is accepted**.

### System 1 — canonical contracts and authority enforcement
- The canonical identity statement, version and non-negotiable principles remain defined only in `cognition/identity.py`. Cognitive contract constructors normalize their identity metadata to that source, including Evidence, MarketState, Hypothesis, Opportunity, DecisionPackage and CampaignSpecification.
- MarketState now exposes the canonical identity at its top-level serialization boundary.
- `scripts/identity_audit.py` now checks the single-definition rule for all three identity constants, identity fields on cognitive contracts, Brain-only construction of DecisionPackage/CampaignSpecification, pure-analyst import boundaries, the existing broker-mutation boundary, Intent identity propagation and the canonical advisor-prompt composition.
- `timestamp_epoch` is ingestion/observation time only. It is no longer promoted to verified source time. Producers must supply `source_timestamp_epoch`; decision snapshots, calibration and execution-cost qualification require that source-time field to be explicitly verified.
- Decision feature snapshots no longer substitute ingestion time for missing source timestamps. The shared execution boundary therefore rejects an incomplete or unverified point-in-time feature snapshot instead of treating a newly ingested stale cache as decision-time evidence.
- The full evidence inventory is retained for audit. The calibration feature lineage is a separate list containing only fresh market-domain evidence at the exact decision cutoff.

### System 4 — complete candidate lineage and conditional payoff validation
- Durable candidate snapshots include opportunities and native hypotheses retained in observe/reject decisions, including serialized native hypothesis records that are not stored in the DecisionPackage's typed hypothesis list.
- Candidate dispositions are recorded for each opportunity/hypothesis. The producer selects the latest durable disposition by event sequence rather than allowing an older Brain-stage disposition to overwrite a later portfolio-stage disposition.
- Fixed-horizon outcome records retain the exact reference quote, outcome quote, source timestamps, evidence IDs, symbol, direction, candidate family, risk distance and realized return. The producer recomputes the return and label from the two verified prices and rejects mismatched lineage, family, direction, symbol, horizon, timing or quote values.
- Candidate geometry must be attributable to point-in-time market evidence. A distant/unfilled entry, missing stop, mismatched quote, missing geometry evidence ID or incomplete source-time record cannot enter calibration.
- Native directional hypotheses are added to the candidate ledger with their own market-derived geometry where available, even when they were not the lead hypothesis or did not pass admission. This reduces the prior bias toward only the single selected native opportunity.
- Calibration is scoped by exact symbol, side, explicit candidate family and fixed horizon. The controlled family vocabulary is defined in the contracts; unclassified families remain observable but cannot consume calibration. The advisor prompt now requires an explicit family field.
- The producer requires complete durable snapshot/outcome/disposition queries, verified fresh non-provisional market evidence, exact quote lineage, at least 150 eligible decision groups, at least 30 untouched holdout samples, three chronological walk-forward folds, at least 95% selection coverage, no more than 5% missed observation windows, Brier score no greater than 0.25, reliability gap no greater than 0.15, and a bounded development/holdout expected-return gap.
- Brain EV uses the conservative lower of development and untouched-holdout expected return, less verified round-trip cost. It does not reinterpret favourable-direction probability as target-before-stop probability, and it does not use an unconditional symbol-side base rate as a candidate edge. A missing family-specific artifact or verified cost keeps directional origination blocked.
- Producer-issued artifacts are held in a process-owned registry and must resolve against the exact symbol, side, family and horizon. Self-declared booleans or caller-supplied artifact IDs are insufficient.

### Validation and release status
- Deterministic tests were added/updated for canonical contract identity, source-time semantics, native hypothesis candidate capture, family-specific calibration, cross-family rejection, exact quote/outcome lineage, and fixed-horizon expected return.
- These tests have **not** been executed in this work session. The PR head has no GitHub Actions workflow runs or commit status checks reported. A local checkout could not be obtained in this environment, so syntax, lint and runtime behavior remain unverified.
- Do not mark System 1 or System 4 accepted and do not merge PR #798 until the repository owner has run the focused tests, supported Python matrix, lint and the repository-wide identity audit, then reviewed the actual failures and corrected them without weakening controls.
- The new producer is an auditable family-conditional historical estimator, not proof of profitability or universal conditional prediction. Any predictive claim still requires sufficient untouched data and external statistical review.
- No live broker orders were sent. No `.env` file, runtime database, credentials or broker configuration was modified.


---

## Mandatory agent and delivery governance — 2026-11-09

This roadmap is binding on every human contributor, coding agent, delegated agent and reviewer. Before any work, read the repository-root `AGENTS.md` and `docs/APEX_ENGINEERING_DELIVERY_CONTRACT.md`. Agent-specific entrypoints (`CLAUDE.md`, `.github/copilot-instructions.md` and `.cursor/rules/apex-engineering.mdc`) point to the same contract; they do not create separate rules.

Every work item must follow recover → scope → trace → implement → prove → validate → review → record → accept. The release register must distinguish IMPLEMENTED, INTEGRATED, VALIDATED, DATA-GATED, BLOCKED and PRODUCTION-ACCEPTED. A merge, passing subset, mock fixture or code presence is not acceptance.

**Numbering clarification:** this architecture roadmap is organized into ten systems. Any delivery plan using Phase 1–8 is a separate execution grouping and must be explicitly mapped to these systems in the release register. Do not renumber, collapse or silently replace the ten-system architecture or its acceptance gates.

### Evidence-backed cross-reference for the numbered identity implementation phases

`docs/APEX_IDENTITY_IMPLEMENTATION_AUDIT.md` defines numbered implementation phases 1–7. Their scope maps to the ten systems below; the mapping is many-to-many and does not change system acceptance status. The same audit has no Phase 8 heading, and repository Markdown search found no Phase 8 definition. Phase 8 therefore remains unmapped until an authoritative delivery document defines its scope.

| Numbered phase in identity audit | Ten-system scope supported by the source headings and deliverables |
| --- | --- |
| Phase 1 — unified observation plane | Systems 1–2: evidence contracts/provenance, source freshness, provisional observations, and degraded-input handling |
| Phase 2 — strategy knowledge and dynamic method selection | Systems 2–3: evidence prerequisites and Brain selection/composition of analytical methods |
| Phase 3 — native hypothesis breadth | Systems 3–4: competing native hypotheses, counter-hypotheses, and separating hypotheses from actionable opportunities |
| Phase 4 — all-horizon opportunity set and continuous reassessment | Systems 4 and 7: conditional opportunity discovery and ongoing campaign management |
| Phase 5 — execution/risk consolidation | Systems 5–7: portfolio permission/sizing, shared broker execution, and recoverable position lifecycle |
| Phase 6 — learning/governance | Systems 8–9: statistical validation, controlled promotion, safety containment, and observability |
| Phase 7 — docs, full regression and demo gate | System 10, with validation evidence required across Systems 1–9 |

This is a scope cross-reference, not a claim that any phase or system is complete. Use Systems 1–10 for acceptance decisions; do not infer Phase 8 work from this table.

The configuration contract is binding: all supported operator-tunable startup/deployment/service/integration configuration must be validated and documented in `.env.example`. The real `.env` remains protected and must not be accessed or modified during engineering without explicit authorization. Dashboard runtime controls use authenticated, authorized APIs and durable audited state; they must never write `.env`. Canonical identity, Brain authority and mandatory safety invariants are not operator-overridable configuration.

CI must validate the agent-governance entrypoints and delivery-contract document. The complete environment-variable inventory and reconciliation against `.env.example` remain an explicit acceptance task; do not claim template completeness until that audit is executed.

### Cross-platform reliability follow-up — 2026-11-09

Continuation work started from PR #864 head `0406f14823110a2a2a4153c7561651922ccd21b7` and is recorded in commit `721cd6def05e6771eb2c5cb2f9de88ad696f2952`.

### Windows suite follow-up — 2026-11-09

- **IMPLEMENTED:** structured log paths escape literal braces before entering Loguru templates, so Windows `sandbox.{GUID}` paths work; Git object-store size is measured at byte resolution so small repositories do not appear to be 0 MiB; data safety accepts Windows directory junctions; watchdog stall age is returned without 0.1-second rounding.
- **TESTED:** Windows-specific fixtures close SQLite connections before temp cleanup, compare paths with platform-native `Path` semantics, and provide the complete `Tick` shape consumed by pollers. ShadowStore test instances are explicitly closed. The market-desk test allows independently sampled timestamps to agree within the test's captured clock window.
- **VALIDATED:** Python 3.12.10 full suite: **4,440 passed, 4 skipped, 165 warnings**. Ruff, identity audit, concept coverage and `git diff --check` passed. The four skips are three local-bare-remote push tests blocked by Windows AppContainer `NtCreateDirectoryObject` (`0xC0000022`) and one symlink creation test blocked by WinError 1314. A separate junction-marker test exercises the Windows path decision without requiring symlink privilege.
- The isolated dependency directory's `ddtrace` telemetry thread caused Windows native exception `0xc0000008` when enabled; the final run disabled tracing and telemetry for the test process. This is test-runner configuration, not an application change.
- The passing GitHub Actions results still refer to the earlier PR head `0406f14823110a2a2a4153c7561651922ccd21b7`; CI for these local commits remains outstanding until they are published. Do not treat local Windows tests as substitutes for the Python 3.11/3.12 CI matrix or provider/broker owner evidence.

- **IMPLEMENTED:** WAL checkpoint connections close even when a corrupt database raises during checkpointing; retention includes records exactly on the cutoff; execution and tick-coalescing intervals use the high-resolution monotonic performance counter on Windows; the observer uses only the documented `APEX_GOLD_DESK_*` settings instead of undocumented aliases.
- **VALIDATED:** an automated check compares literal Python environment reads with `.env.example`; source-reading tests specify UTF-8. Focused regression run: 118 passed. Concept coverage check, identity audit, compilation and Ruff passed.
- **Full local suite:** Python 3.12.10 on Windows reported 4,405 passed and 15 failed, with no collection/setup errors. Remaining failures involve Windows sandbox/process and symlink restrictions, SQLite file locking during test cleanup, a Windows log-sink setup issue, and Windows path expectations. The Linux GitHub Actions runs at the starting PR head passed; CI for this follow-up commit is still required.
- **Status:** the fixes above are IMPLEMENTED and focused VALIDATED. This does not close the owner/provider evidence gates for WP-B3/B4/B5/B7, change any concept coverage classification, or production-accept Phase 2. The roadmap retains its ten-system acceptance model; no unverified Phase 1–8 mapping is inferred.

### Read-only MT5 partial portfolio integration — 2026-11-09

- **IMPLEMENTED:** `SystemContext` registers a read-only MT5 portfolio adapter. It sanitizes current account and position fields, excludes account identifiers/credentials/tickets, preserves typed read errors, and marks the combined observation PARTIAL with no verified source timestamp.
- **INTEGRATED:** the portfolio snapshot assembler preserves a single partial observation; `PortfolioRiskReadings` passes its sanitized facts and unknowns to Brain evidence but does not use partial equity or positions in verified portfolio-risk metrics. `applicable` remains false and full historical returns/equity/correlation evidence remains DATA-GATED.
- **VALIDATED:** focused data-source, concept coverage, and SystemContext tests: **23 passed**; Ruff, compileall, identity audit, concept coverage synchronization, and `git diff --check` passed.
- **Full-suite result:** Python 3.12.10 on Windows first reported **4,418 passed, 4 skipped, 1 failed** in 409.54 seconds. The one failure (`test_strategy_knowledge_assesses_lenses_without_authorizing_trades`) passed in isolation and targeted runs. A complete rerun on the updated tree passed **4,420 tests, 4 skipped, 0 failed, 159 warnings** in 168.01 seconds. The transient failure did not recur; its cause remains unconfirmed.
- The four skips remain three local-bare-remote push tests blocked by Windows AppContainer `NtCreateDirectoryObject` (`0xC0000022`) and one symlink creation test blocked by WinError 1314. The junction test covers the Windows path decision without symlink privilege. The suite disabled ddtrace tracing and telemetry to avoid the previously observed Windows native telemetry-thread crash.
- **CI / publication:** current PR #864 remote head is still `0406f14823110a2a2a4153c7561651922ccd21b7`. This local work has not been published, so there is no remote CI result for it. The known Windows Git/AppContainer push issue remains an owner/environment blocker.
- **Acceptance:** no change to concept FULL/DATA-GATED classifications or production acceptance. Provider entitlement, complete verified portfolio history, owner-connected validation, and other Phase 2 evidence gates remain open.

### Durable owner controls and Brain entry pause — 2026-11-09

- **IMPLEMENTED:** owner-limit updates use a compare-and-set transaction that writes the user's config and before/after audit event atomically; stale concurrent edits return HTTP 409. Authenticated audit reads are scoped to the current user. Dashboard pause/resume and manual FROZEN state are stored in local SQLite with an append-only action history; startup restores controls before engine threads begin.
- **INTEGRATED:** the event-driven Brain origination sink enforces the restored pause before an OPEN reaches the shared intent aggregator, while position management and the close-intent path stay available. The API `ProcessManager` loads per-user overrides when starting each instance; engine configuration creates the canonical `OwnerLimits` consumed by `SystemContext` and risk authorities.
- **VALIDATED:** focused dashboard, API, Brain-path and observability regressions: 79 passed. The complete Python 3.12.10 Windows suite, including the final restore fail-closed regression, passed **4,428 tests, 4 skipped, 0 failed, 166 warnings** in 387.47 seconds. Repository Ruff CI command, `compileall`, `git diff --check`, identity audit and concept-coverage synchronization passed.
- **DATA-GATED / acceptance:** deterministic simulations do not substitute for an owner-connected broker/account trace. Provider feed entitlements, authoritative sessions, complete portfolio history and host full-universe benchmarks remain open. No broker order or position change was issued, and no change is made to concept coverage classifications or production acceptance.

### Provider-neutral economic calendar through ComplianceDivision — 2026-11-09

- **IMPLEMENTED / INTEGRATED:** `SystemContext` supplies the shared read-only provider registry to `NewsGuard`, and the same guard instance is passed to `ComplianceDivision`. Registered economic-event providers are normalized only from complete observations with verified source timestamps, fresh receipt, point-in-time publication/revision vintages and explicit currency/impact. Provenance is retained on the normalized event. Configured provider faults or incomplete evidence block entries rather than silently switching source.
- **VALIDATED:** deterministic registry events, malformed/incomplete evidence, provider/capability failure, reversed-cutoff cache rejection, actual SystemContext-to-ComplianceDivision wiring, and the repository silent-handler audit: 54 focused tests passed. Full Python 3.12.10 Windows suite: **4,433 passed, 4 skipped, 0 failed, 166 warnings** in 298.16 seconds; Ruff, compileall and `git diff --check` passed.
- **DATA-GATED:** no economic-event adapter is currently registered by the runtime; the existing legacy calendar remains the fallback. Provider entitlement, event coverage and source-authentication acceptance remain outstanding. This integration does not establish calendar production acceptance or close WP-B3/System 2.

### Local branch delivery checkpoint — 2026-11-09

- Starting `00187b659f430d08a1ed1df207c7f3eda44c1571` and preserving the six existing unpublished commits, the validated continuation commits are `58bd10bd25a507d5f335a6a84f2410955c1dc1d3` (durable dashboard controls and owner-limit audit) and `24390be3d6199b9e00a959fdbc1bd69f0e634e9e` (provider-neutral NewsGuard registry path). At this checkpoint the local branch contains eight commits ahead of `origin/phase2/concurrent-data-capabilities`.
- The new full-suite result is **4,433 passed, 4 skipped, 0 failed, 166 warnings** on Python 3.12.10/Windows. Ruff, compileall, identity audit, concept coverage synchronization and whitespace checks passed.
- **Initial publish blocker (resolved below):** standard push could not start Git for Windows' MSYS credential helper (`NtCreateDirectoryObject ... 0xC0000022`); at that time remote HEAD was `0406f14823110a2a2a4153c7561651922ccd21b7` and no Actions run existed for local HEAD `24390be3d6199b9e00a959fdbc1bd69f0e634e9e`.
- Git Credential Manager device authentication completed with its DPAPI store. A Windows-native askpass bridge retrieved the stored credential directly from GCM without displaying or writing the token, allowing an ordinary fast-forward push. No force-push, broker order, position modification or `.env` access occurred.
- Provider entitlements and verified calendar events, owner-connected broker/account lifecycle evidence, complete authoritative portfolio history/session inputs, and full-universe host benchmarks remain DATA-GATED. Phase 2 and overall production qualification are not accepted.

### Actions evidence after device authentication — exact head `806b911783b4f9b42b27d0871decd4ad3e7b0097`

- The ordinary fast-forward push succeeded; remote branch HEAD exactly matched local HEAD.
- **Gold desk focused tests:** success. **APEX TRADER CI:** GitHub reports failure after 51 seconds, but its run page shows the lint and Python test matrix still waiting and provides no job records/logs. The sole annotation is `Internal server error` (correlation ID `b49398b1-f114-46d8-9eb3-b949e858684d`). GitHub's rerun-failed-jobs endpoint rejected a retry with HTTP 403, “This workflow run cannot be retried.” No CI test or lint failure was reported; the full CI result remains unverified because the workflow failed before jobs ran.

### Ambiguous partial-close lifecycle hardening — 2026-11-10

- **IMPLEMENTED / INTEGRATED:** the shared `ActionExecutor` correctly classifies a lost partial-close acknowledgement as ambiguous and refuses an immediate retry, but the Brain result callback previously rolled back `partial_closed`/`tp1_hit`. That made the same Brain path eligible to submit another reduction after the executor's short dedup window. The evaluator now persists `PARTIAL_CLOSE_PENDING` before execution; successful acknowledgement clears the pending marker, ordinary rejection restores the prior state, and an ambiguous result persists `RECONCILIATION_REQUIRED` plus an error-severity lifecycle event. Restart promotes any unacknowledged pending partial to reconciliation-required. The event-driven evaluator updates a normal-state volume baseline and resolves an ambiguity only after a later position-book snapshot shows a lower remaining size; unchanged or missing size stays blocked.
- **VALIDATED:** executor, result/restart/reconciliation, minimum-lot and event-driven suites: **130 passed**. Full Python 3.12 Windows suite: **4,459 passed, 4 skipped, 0 failed, 172 warnings** in 135.91 seconds. A preceding full run on the initial fix had one strategy-lens context failure that passed in isolation, in its containing file, and on its full rerun. Repository Ruff, `compileall`, identity audit, concept-coverage synchronization and `git diff --check` passed.
- **PUBLISHED CI before this change:** branch head `f11ceee50f5d0bb9d7672f202e52133ca14ad708` equaled the remote tracking head. GitGuardian passed. GitHub Actions runs #3652 (`42673792419`, APEX TRADER CI) and #1815 (`42673792406`, Gold desk) both failed at workflow service level with no Actions job records; CI code results remain unverified.
- **PUSH STATUS:** after the default WinCred lookup failed, the existing DPAPI credential store at the task's `work/git-credential-store` path was explicitly selected for GCM. The normal fast-forward push then succeeded without printing or copying the credential. Local and remote branch heads exactly match `e2be767becd7cf9e1363736e7ca4049718b87e41`; no force-push was used.
- **CI on the exact published tree:** head `ce34674512663e8fdfa4eb160c9d9487687efb9f` completed APEX TRADER CI #3654 (run `42677382260`) and Gold desk #1817 (run `42677382605`) successfully. APEX lint and both Python 3.11/3.12 test, coverage, identity/safety and import jobs passed; both Gold desk Python-version jobs passed.
- **WP-B5 synthetic harness check:** the read-only `run_benchmark`/`analyze_market_frames` path completed exact cold/warm coverage for 10 deterministic synthetic symbols × 4 timeframes × 500 bars with zero failures or execution mutations. Cold total/p95 per symbol: 12,146.030/1,911.445 ms; warm: 2,278.419/282.391 ms. These synthetic timings exercise the harness and cache path only; they are not broker-host, full-universe or production performance evidence.
- **DATA-GATED:** broker-history evidence for unchanged/closed positions, owner-connected account lifecycle acceptance, provider/session/portfolio evidence and full-universe host benchmarks remain outstanding. No broker orders or position modifications were sent, and production acceptance is unchanged.

### Local runtime and analytical hot-path follow-up — 2026-11-10

- **IMPLEMENTED:** the quantitative-state reader builds each timeframe's historical state vectors once and shares them between analogue and novelty readings. Population volatility uses an `fsum`-based calculation instead of the standard-library Fraction-heavy path. Equivalence and one-build-per-timeframe regression tests were added.
- **IMPLEMENTED:** structured default log files resolve under `runtime_paths.log_dir()`, so `APEX_LOG_DIR` actually isolates them. The startup log now reports platform connection status without claiming that Apex is live before an engine is started.
- **INTEGRATED / LOCALLY VALIDATED:** `main.py --dashboard` was launched with dotenv loading and broker discovery disabled, broker connections stubbed offline, and data/log paths isolated under a temporary directory. The dashboard bound to loopback, `/api/health` returned HTTP 200 with `attached=false` and `running=false`, mutating endpoints were disabled without an API key, and both broker statuses were OFFLINE. The isolated data topology was rejected and clean-start sync/purge were skipped. No broker orders or position modifications were possible in this run.
- **VALIDATED:** Python 3.12.10 full suite: **4,463 passed, 4 skipped, 0 failed, 172 warnings**. Repository Ruff, compileall, canonical identity audit, concept-coverage synchronization, constitutional audit (0 violations across 10 invariants) and `git diff --check` passed.
- **PUBLISHED CI:** commit `e3176960cc2a2f00fef727e80304a5d37e42a2be` is the exact remote branch head. APEX TRADER CI #3657 (run `42685289477`) passed, including lint and Python 3.11/3.12 jobs; Gold Desk #1820 (run `42685289700`) passed on Python 3.11/3.12. No force-push was used.
- **SYNTHETIC ONLY:** a local cProfile of `analyze_market_frames` over one symbol × four timeframes × 1,000 bars per timeframe dropped from about 4.85 s / 12.49 million calls to 3.67 s / 10.19 million calls after state-row reuse and the volatility calculation change. This is a profiling fixture, not a production latency result; WP-B5 remains DATA-GATED on full-universe owner-host measurements.
- **DATA-GATED:** provider and authoritative-session data, owner-connected broker lifecycle evidence, account/owner-limit acceptance and full-universe host benchmarks remain outstanding. This local run does not production-accept any system or Phase 2.

### MT5 terminal selection hardening — 2026-11-10

- **IMPLEMENTED:** optional `MT5_TERMINAL_PATH` pins the Python connector to one exact terminal executable. A configured path is checked before initialization and fails closed if missing; blank retains the existing auto-discovery behavior. The key is documented in `.env.example` and covered by the literal environment-read audit.
- **VALIDATED:** terminal path, absolute-path enforcement, missing-path fail-closed, auto-discovery compatibility, PlatformManager wiring and environment-template security tests: **10 passed**. Full Python 3.12 suite: **4,468 passed, 4 skipped, 0 failed, 172 warnings**. Ruff, compileall, identity, concept-coverage and constitutional audits passed.
- **LIVE CONNECTIVITY NOT VALIDATED:** the current logged-in JustMarkets demo terminal is visible in the desktop, but the MetaTrader5 Python `initialize()` call from the attached shell hung even when pinned to `C:\Program Files\MetaTrader 5\terminal64.exe`; the read-only preflight was stopped without placing orders. Apex's broker connection and demo account snapshot therefore remain unverified in this environment. The terminal window remained open; no second MT5 window remained visible.

### Runtime log remediation — 2026-11-10

- **IMPLEMENTED:** the Deriv tick adapter now requests prices through an explicit Deriv-only route. It no longer uses the generic PlatformManager route that prefers MT5 for Platform.BOTH, so MT5 quotes cannot be relabelled as Deriv ticks. The adapter waits while Deriv is offline and resumes after authentication; it does not fall back to MT5.
- **IMPLEMENTED:** live per-platform broker balances are reconciled into RiskEngine after connector startup and before EventDrivenSystem construction. Missing broker account balance truth marks startup safety-degraded; a confirmed zero balance is preserved rather than leaving the configured cold-start balance in place. The PnLTracker fallback denominator is synchronized to the first broker-confirmed balance, so a configured backtest default cannot remain as the live fallback.
- **IMPLEMENTED:** removed the obsolete ctx.adaptive_weight_provider argument from AdaptiveSchedulerLoop construction; the current scheduler does not expose or consume that provider. Corrected Loguru formatting in execution-cost source diagnostics.
- **VALIDATION ADDED:** regression coverage for explicit Deriv routing, offline Deriv behavior, initial/zero broker-balance reconciliation, and a read-only MT5 clock diagnostic (python scripts/diagnose_mt5_clock.py).
- **CLOCK BASIS FINDING:** the consistent +7,200-second offset is consistent with JustMarkets broker-server time (JustMarkets documents GMT+2/GMT+3 schedules). Recent MQL5 moderator clarifications say returned market-data timestamps are broker-server time despite conflicting UTC wording in parts of the Python documentation. A fixed offset is unsafe because the broker changes between GMT+2/GMT+3.
- **IMPLEMENTED IN PR #868 — VALIDATION PENDING:** production MT5 clock acquisition no longer reads the terminal-local MQL5 publisher file. It estimates the broker timestamp offset from an advancing live tick and at least two agreeing independent network UTC sources (NTP, with HTTPS Date-header fallback), binds state to the exact account server, anchors it to monotonic time, and includes source/tick uncertainty. Tick freshness and completed-bar checks apply conservative uncertainty margins; market and pending entry preflights remain fail-closed. No fixed GMT+2/GMT+3 or workstation wall-clock substitution is used.
- **OWNER ACTION REMOVED:** no EA compilation, chart attachment, or publisher file is required. The existing MQL5 publisher remains an optional diagnostic/reference utility only. PR #868 still requires MT5 regression, Python 3.11/3.12, lint, Gold desk, and integrated safety validation before merge.
- **STILL BLOCKED BY OWNER SETUP:** the dedicated apex-trader-data repository is absent or not mounted as the required separate Git repository. Clean-start correctly refuses synchronization and purge. Provision the expected data repository; do not weaken the topology guard.
- **CONFIGURATION DEPENDENCIES:** Deriv remains offline without DERIV_ACCESS_TOKEN. Local Ollama/vLLM advisor connection refusals do not disable native Brain reasoning. Gemini budget limits require owner-selected provider quotas and are not guessed.
- **HISTORICAL DATA-GATED:** live ticks and short live tick-history windows use the current network-anchored broker-offset estimate with explicit uncertainty; completed-bar filtering uses a conservative lower bound of the synchronized broker clock. Historical OHLCV, older tick history, deals and order-history timestamps still need a historical offset series to normalize across DST changes; today's offset is not applied retroactively. No broker order or position mutation was issued.

- **SAFETY ENFORCEMENT CORRECTION:** a critical SystemContext degradation now trips CognitionLoop.broker_book_verified=False, both when the failure occurs before the loop is constructed and when it occurs afterward. This is the actual new-exposure admission breaker; market observation and existing-position management remain available. The previous status-only flag was insufficient.
- **ACCOUNT DATA VALIDATION:** non-finite broker balances (NaN/Infinity) are excluded from PlatformManager account snapshots and RiskEngine reconciliation. A malformed account value cannot poison pooled capital or PnL denominators.


### Trading controls and configuration coverage — 2026-11-10

- **FIXED:** `TRADING_MODE` was present in `.env.example` but was not consumed by `AppConfig`; the application therefore retained the default full universe. It now resolves `gold` to exactly XAUUSD, `full` to the configured categories, and `selected`/`single` to the explicit `TRADING_SYMBOLS` list. Persisted per-user universe preferences retain precedence by default; `TRADING_MODE_LOCKED=true` makes the process-level universe authoritative. `TRADING_ENABLED=false` disables new Brain origination and is rechecked at the ActionExecutor boundary to reject already-queued market/pending OPEN intents, without disabling existing-position management or risk-reducing closes.
- **DOCUMENTED:** the root server `.env.example` now documents the active universe and new-exposure controls. `docs/CONFIGURATION_REFERENCE.md` identifies the settings, precedence and security boundary. Frontend Vite build variables remain in their own `frontend/.env.example` and `dashboard/frontend/.env.example`; private credentials must not be placed in `VITE_*` variables.
- **REGRESSION GUARDS ADDED:** literal Python runtime environment reads are checked against the root `.env.example`; Vite build-time reads are checked against each frontend's own template. This prevents future literal environment reads from silently bypassing the documented template.
- **CONFIGURATION INVENTORY STILL OPEN:** the field inventory records 716 typed fields across 43 configuration dataclasses. These are not automatically 711 operator settings: analytical definitions, data-quality rules, derived values, internal scheduling, owner limits and non-overridable safety invariants must be classified before exposure. The remaining task is a source-backed field-by-field inventory and migration of legitimate operator controls, without making analytical thresholds or constitutional safety rules arbitrary environment overrides.
- **VALIDATION:** full APEX Python 3.11/3.12 CI and focused Gold Desk CI for the latest PR #868 head are running; no result is claimed until they complete. No `.env` file was read or modified, and no broker order or position mutation was issued.


### Environment configuration expansion — 2026-11-10

- **FIXED:** the environment coverage CI failure was caused by removing two still-consumed event-driven switches. `APEX_ED_POSEVAL_PARALLEL` and `APEX_ED_EVENT_REACTIVE_MGMT` are restored; unused `APP_ENV` was removed from the template and README.
- **ADDED:** `config_env.py` provides an explicit typed allowlist of 101 operational settings across performance/cache, ops/logging/reconnect, backup, backtest, decision trace, outcome feedback, signal ledger, post-close tracking, tuning, counterfactual, interaction, calibration, developing analysis, campaign, adaptive tuner and cross-instrument runtime sections. Empty values retain typed defaults; malformed values and invalid operational ranges fail startup.
- **ADDED:** `APEX_LOG_LEVEL` controls the root console log level. `APEX_OWNER_LIMITS_JSON` bootstraps the canonical OwnerLimits object through its existing editable-field allowlist and immutable hard ceilings. It cannot modify internal-only hard stops.
- **EXPLICITLY NOT EXPOSED:** legacy duplicate RiskConfig/ScoringConfig fields, analytical cutoffs, statistical model thresholds, broker-derived facts, data-purge switches, and constitutional safety invariants. They are classified separately rather than turned into ungoverned environment variables.
- **REGRESSION TESTS:** added typed parsing, malformed-value rejection, operational-range validation, OwnerLimits hard-ceiling checks, and template/allowlist parity.
- **VALIDATION STATUS:** the last completed full CI run (before these changes) had 4,489 passing tests and two failures in the environment-template audit. Both causes were corrected, then the typed environment surface was added. No fresh workflow run has yet been reported for the current branch head; the current branch must not be merged until fresh Python 3.11/3.12 and lint checks pass.
- No real `.env` was read or modified, no broker orders were sent, and no safety gate was weakened.


### WP-B4 quote provenance hardening — 2026-11-10

- **ADDED:** adapter-specific quote provenance validation in `cognition/quote_provenance.py`. A bare `source_timestamp_verified=true` no longer suffices for a calibration quote.
- **MT5:** the quote must carry its original broker timestamp, the normalized UTC epoch, the measured offset (which must reconcile raw broker time to normalized UTC within the uncertainty bound), at least two independent clock-source identifiers, and offset uncertainty no greater than the clock bridge's 2.5-second acceptance ceiling.
- **Deriv:** the quote must retain the provider's original `history.times` epoch, explicitly identify Unix-UTC semantics, and record local receipt time. Deriv's one-price response remains `mid_only`; it is not represented as an executable bid/ask spread.
- **Pipeline:** MT5 and Deriv adapters preserve raw timestamp provenance through `Tick`, market snapshot evidence, candidate reference quotes and fixed-horizon outcome quotes. `CalibrationProducer` validates both decision-time and outcome quotes against the adapter-specific contract before issuing artifacts.
- **TESTS ADDED:** adapter provenance validation, missing/ambiguous source-time rejection, and a calibration regression proving that deleting the raw provenance while leaving the old boolean flag true produces no artifact.
- **LIMITATION:** this is an application-side adapter attestation, not a cryptographic signature from MetaQuotes or Deriv. It authenticates the expected in-process adapter data path and detects missing/contradictory provenance; it cannot independently prove that a broker's upstream quote is truthful. Broker-export authenticity and owner-supplied historical data acceptance remain separate WP-B4 evidence gates.


- **SAFE TEMPLATE DEFAULT:** `.env.example` now sets `TRADING_ENABLED=false`. The operator must explicitly set it to `true` to allow new exposure; position reconciliation and risk-reducing management remain available.


### WP-B5 benchmark correctness — 2026-11-10

- **FIXED:** the live concept benchmark requested `count` rows and then called `analyze_market_frames(source_includes_forming_bar=True)`, which deliberately excludes the final forming bar. It therefore reported raw rows as though they were completed bars and could claim the requested depth with one fewer completed candle.
- The benchmark now fetches `count + 1` raw rows, reports both `raw_timeframes` and completed `timeframes`, and makes the requested bar depth refer to completed candles. The pass gate requires the full requested completed-bar count across every requested symbol/timeframe in both cold and warm passes.
- **TESTS ADDED:** exact fetch depth, raw-versus-completed counts, and complete cold/warm coverage.
- Actual broker-universe timing remains an owner-host, read-only measurement. CI validates the benchmark logic; it cannot substitute synthetic data for the broker-connected performance acceptance.


### Full-universe category correction — 2026-11-10

- **FIXED:** the default full-universe category list omitted `synthetic`, even though the instrument registry classifies Deriv synthetic instruments under that category. The default `AppConfig.enabled_categories` and `.env.example` now include `synthetic` alongside forex, commodity, index and crypto. Gold mode remains XAUUSD-only. Full mode still requires a non-empty list of valid registered category names.


### WP-B5 incremental volume-profile computation — 2026-11-10

- **ADDED:** `VolumeProfileConcept` now caches weekly OHLCV profiles by symbol, timeframe, ISO week and a content fingerprint. On a new candle, unchanged closed weeks reuse their prior profiles while only the changed active week is recomputed. A historical correction invalidates only the affected week.
- The cache is bounded (4,096 entries), thread-safe, and reports per-reading weekly-profile cache hits/misses. A public clear function supports deterministic tests and explicit data reloads.
- Session metadata is now evaluated once per bar rather than repeatedly while selecting the active session profile.
- **TESTS ADDED:** unchanged-history reuse, active-week append invalidation, and historical correction invalidation.
- This complements the shared incremental swing state and liquidity-pool work. Owner-host full-universe cold/warm timing is still required to quantify production impact.


### Validation update — 2026-11-10

- APEX TRADER CI runs **3841** and **3842** completed successfully: lint, Python 3.11 and Python 3.12 all passed on the code head containing the MT5 clock changes, typed environment allowlist, quote provenance validation, completed-bar benchmark correction, incremental volume-profile cache and its tests.
- Gold Desk focused runs **2001** and **2006** passed on Python 3.11 and 3.12.
- The subsequent documentation-only environment inventory/test-parity change was manually checked against all 293 root keys and all frontend keys; every key is present in the reference. A new full Apex run **3847** is validating the current head.
- The current PR remains draft pending final run completion and owner-host acceptance.


## Exact-ticket Brain management and campaign lifecycle — 2026-11-11

**PR #902:** `engineering/exact-ticket-brain-management-final`; validation snapshot head `172a2ce6e6cabceb9d024b95a375780b06be2544`. This is implementation evidence, not system acceptance.

- **Exact management identity:** live management action IDs are deterministic over Brain decision ID, broker platform, exact position ticket, action kind and milestone. Missing decision/ticket/platform provenance suppresses dispatch. Management joins use exact confirmed broker identity; ambiguous matches fail closed.
- **Durable execution lifecycle:** write-ahead milestone state is persisted before dispatch. Queue acceptance is not broker acknowledgement. Ambiguous outcomes remain non-replayable until verified broker reconciliation; explicit rejection is distinct from timeout/unknown.
- **Campaign lineage:** campaign tickets are bound only after an exact confirmed broker snapshot. Terminal close removes only the matching platform/ticket. A campaign remains live while any other confirmed ticket or unresolved unbound open leg remains; open-leg counts now reflect remaining active/unresolved legs.
- **Unknown broker status:** dictionary-shaped pending-order responses are handled explicitly and remain pending; they are not treated as filled positions.
- **Confidence isolation:** entry confidence is no longer included in the Brain's position-management context. Campaign reallocation ranking uses expected-value metadata only, and opportunity-vs-book comparison returns `unranked` when comparable expected-return/risk evidence is absent. The uncalibrated hypothesis reasoning-strength field remains descriptive as permitted by System 3; it is not an economic probability, expected-value substitute, management input or sizing input.
- **Legacy cleanup:** removed the no-op breakeven ReEntryManager, dormant cooldown state, `re_entry_eligible` plumbing, `RE_ENTRY_ARMED` event and runtime/package wiring. A closed campaign returns to ordinary Brain observation.

**Validation:** on head `172a2ce`, lint passed and Gold Desk focused tests passed on Python 3.11 and 3.12. The full Python 3.11/3.12 suites were still running when this record was written. An earlier pre-correction run had two incorrect test expectations (4,514 passed); those assertions were corrected. Do not mark this increment validated until the full suite, generated concept-coverage check, identity/safety audit and import check all pass on the final PR head.

**Safety boundary:** no live broker orders, .env access, credentials, runtime databases or broker settings were changed. PR #902 remains open and unmerged. Owner-connected broker acceptance and the remaining System 2 evidence gates are still separate requirements.


## Production management and disconnected heuristic retirement — 2026-11-11

PR #902 has expanded the exact-ticket management increment to close additional identity and legacy-path findings:

- **V-009 production discretionary fallback:** removed `RiskConfig` fields and environment parsing for scan-directional and legacy mechanical exits. The live bootstrap hard-sets `discretionary_exits_enabled=False` and `scan_directional_exits_enabled=False`. The PositionWorker retains only the independent hard-stop, weekend/session/spread and absolute-profit safety floor in production. Explicit worker discretionary fixtures remain isolated to offline tests/backtests.
- **V-012 disconnected cross-instrument helpers:** repository call-site audit found no runtime decision consumers for `CrossInstrumentRanker`, `OpportunityQualitySizer` or `PositionDisplacer`; the sizer also used confidence as a size multiplier. Their modules, tests, SystemContext wiring, dashboard/frontend status, config fields and env switches were removed. The advertised global opportunity queue was never initialized and its controls were removed. Only the proactive scanner remains, and it is observational only.
- **Configuration reconciliation:** removed retired controls from `.env.example`, `config_env.py`, the operator environment reference and typed configuration inventory. The inventory now records 697 fields across 43 classes, including the three retained CrossInstrumentConfig fields.
- **Runtime identity:** no re-entry manager, no production-configurable mechanical discretionary-exit fallback, no confidence-based management comparison, no confidence-based reallocation ranking, and no disconnected sizing/displacement helper remains in the live SystemContext.

**Validation snapshot:** final implementation head `cf53f109aa89213d8d8e6c2d664b10499c895d61` passed 4,485 tests on Python 3.11 and 3.12, lint, generated concept-coverage verification, canonical identity/safety audit and import checks. Gold Desk focused tests and frontend build also passed. This head includes exact-ticket lifecycle, confidence isolation, removal of no-op re-entry, production hard-disable of mechanical discretionary exits, retirement of disconnected cross-instrument helpers and queue controls, removal of the unused proximity setting, and removal of the unconsumed execution-quality sizing toggle.

**Still not accepted:** System 2's broker/calendar/financing and independent holdout gates; owner-connected broker execution acceptance; full ten-system acceptance. No live broker orders, .env access, runtime database changes or credential changes were made.
