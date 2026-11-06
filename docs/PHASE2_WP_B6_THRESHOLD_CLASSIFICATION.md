# WP-B6 — threshold classification and removal audit

**Audit baseline:** `main` at `9c1c95a2c6e5c76def1b8e29c4547654c1f19e97` (2026-11-07).
**Status:** classification audit; no runtime thresholds changed in this pass.
**Safety boundary:** this work must not weaken broker, account, exposure, data-integrity, or fail-closed controls.

## Decision rule

Every numeric constant found in cognition, compliance, and observer scheduling must be classified by its actual call path and effect, not by its variable name.

- **Market-analysis constant:** changes interpretation, applicability, evidence strength, hypothesis selection, direction, or trade qualification. Remove it or replace it with instrument/timeframe-specific observed evidence. Do not merely expose it as a tunable threshold.
- **Concept definition:** a value intrinsic to the named concept (for example, a three-candle pattern definition). Retain it, document its source, and test its definition.
- **Execution/account safety limit:** constrains broker validity, capital, exposure, loss, margin, or operational permission. Preserve it. Owner-controlled limits belong in the owner-limits surface; broker-imposed limits remain adapter facts.
- **Operational scheduling limit:** bounds polling, event coalescing, queueing, provider calls, or compute. It must not alter the market evidence or the Brain's conclusion. Retain only with explicit latency/coverage tests and observability.
- **Unclassified:** do not delete or change until the complete dataflow and safety impact are proven.

## Findings

### 1. Minimum market evidence-domain floor

**Locations:** `config.py::CognitionConfig.min_evidence_domains`, `cognition/brain.py::CognitiveBrain.__init__`, and the Brain's evidence-coverage qualification path.

**Observed behavior:** the configured value is read from `COGNITION_MIN_EVIDENCE_DOMAINS`, but the Brain applies a hard minimum of three even when constructed directly. Missing market-domain coverage is explicitly unconfirmed and blocks qualification. The tests assert both the floor and fail-closed behavior.

**Classification:** epistemic/action-admission safety control, not a market-analysis measurement. It is not safe to remove as a simple constants cleanup.

**Disposition:** preserve unchanged in this package. A separate redesign may replace the raw domain-count floor only if it proves equivalent or stronger qualification using source provenance, independent evidence groups, uncertainty, contradictory evidence, and valid single-domain concept cases. The replacement must not count advisor or execution metadata as market evidence. Missing provenance must remain unknown and must not authorize exposure.

**Required replacement tests before any future change:**
- one valid concept with one genuinely independent source is handled according to explicit policy, not silently promoted;
- duplicated readings from one source do not manufacture confluence;
- multiple concepts sharing one source do not manufacture independent domains;
- absent/unknown provenance fails closed for new exposure;
- contradictory and stale readings remain visible;
- LONG/SHORT mirror behavior is identical.

### 2. Observer materiality filter

**Location:** `gold_desk/apex_observer.py`, per-symbol wake decision.

**Observed behavior:** a fresh Brain wake is triggered by a price displacement relative to M5 ATR, current spread, and a tiny price-relative floor, or by a sufficiently large spread change. The observer callback queues cognition; the observer itself emits factual evidence and has no order authority.

**Classification:** operational scheduling/coalescing, not a directional or opportunity threshold.

**Disposition:** do not remove blindly. Prove that changing this filter cannot change the stored evidence snapshot or create/erase a market conclusion; prove the periodic cycle remains a backstop; prove per-symbol queueing and provider throttles remain bounded. Instrument suppressed-wake counts, wake latency, and the evidence changes accumulated between wakes. If the filter is retained, document its role as compute scheduling only and keep it outside concept readers and Brain qualification.

### 3. Event cadence and confidence-delta settings

**Locations:** `config.py::CognitionConfig.event_min_interval_seconds`, `event_confidence_delta`, and `cognition/loop.py`.

**Classification:** operational scheduling only if these values control when work is queued and do not filter or mutate the evidence passed to the Brain.

**Disposition:** retain until the event path is traced end-to-end and tested. Separate market evidence freshness from provider-call throttling. A throttle must never make stale evidence appear fresh or suppress required position-risk management.

### 4. Advisor quorum

**Locations:** `CognitionConfig.min_advisors_for_action` and `CognitiveBrain.min_advisors_for_action`.

**Classification:** cognitive-source coverage control, distinct from market evidence-domain breadth.

**Disposition:** preserve the current fail-closed behavior for advisor-assisted origination. Native-only reasoning must remain explicitly distinguishable from advisor-assisted reasoning; advisor count must not be fabricated when council metadata is absent. Do not count advisors as independent market evidence.

### 5. Compliance and execution thresholds

**Current audit boundary:** this search pass identified account-risk, portfolio-governor, broker-feasibility, spread/cost, and market-status controls, but did not establish a complete call-site inventory.

**Classification rule:**
- broker tick/lot/price precision, minimum stop distance, trading mode, margin, and provider constraints are execution facts and must remain enforced;
- account-owner risk per trade, daily loss, maximum exposure, leverage/margin budget, and position-count limits belong in one explicit owner-limits surface;
- spread/cost and net expected-value checks must use verified current data; unknown costs remain unknown;
- any numeric value that changes a strategy's analytical interpretation is not an execution safety limit and must be separately justified or removed.

**Disposition:** no compliance threshold is changed until every entry, pending-order, modification, scale-in, reversal, and recovery route is traced to the shared admission boundary.

## Acceptance still required for WP-B6

1. Complete a repository-wide inventory of numeric constants and threshold comparisons in cognition, observer, compliance, portfolio sizing, and execution.
2. For every item, record source file, call path, data dependency, effect on evidence/decision/execution, classification, owner, and tests.
3. Remove only proven market-analysis tuning constants; retain concept definitions and genuine execution/account safeguards.
4. Move owner-configurable capital limits to one typed, validated owner-limits surface without changing their effective protection.
5. Prove operational throttles affect scheduling only, not evidence contents, decision semantics, or risk-management availability.
6. Add regression tests for every changed boundary; do not update or weaken tests merely to match a new implementation.
7. Run coverage synchronization, identity/safety audit, imports, lint, focused cognition/observer/portfolio/execution suites, and full CI on Python 3.11 and 3.12.
8. No live broker orders, credentials, runtime database changes, or `.env` edits are part of this work.

## Explicit non-claims

This audit does not claim WP-B6 acceptance, completion of the owner-limits surface, production benchmark acceptance for WP-B5, or Phase 2 completion. It records the safe classification boundary before implementation so that a constants cleanup cannot accidentally weaken Apex.
