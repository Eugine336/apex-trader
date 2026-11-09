# Apex Trader — Engineering Delivery and Production Qualification Contract

**Status:** Binding delivery contract for every engineering agent and contributor.  
**Applies until:** The complete redesign has passed all engineering, owner-connected and production release gates.  
**Repository:** `Eugine336/apex-trader`

## 1. Purpose

This contract turns the full-redesign roadmap into an enforceable engineering operating process. It is not a replacement for the canonical identity, Constitution, ten-system architecture roadmap or concept implementation plan. It governs how every agent executes that work and how completion is evidenced.

The required outcome is a coherent, instrument-agnostic, evidence-driven Apex Trader whose native CognitiveBrain is the sole market decision authority; whose execution and capital-protection boundaries are deterministic; and whose production claims are supported by reproducible evidence.

## 2. Binding document hierarchy

- Canonical identity: `cognition/identity.py::IDENTITY_STATEMENT`.
- Binding architecture and safety law: `docs/APEX_Constitution.md`.
- Architecture scope and dependencies: `docs/APEX_FULL_REDESIGN_ROADMAP.md` (Systems 1–10).
- Analytical method/lens scope: `docs/APEX_CONCEPT_IMPLEMENTATION_PLAN.md` and generated `docs/APEX_CONCEPT_COVERAGE.md`.
- Agent behavior and task workflow: repository-root `AGENTS.md`.
- This document: delivery evidence, status semantics and release gates.

The roadmap currently expresses architecture as **ten systems**, while execution conversations may refer to delivery phases. These labels must not be treated as interchangeable. Maintain an explicit mapping in the roadmap/release register before changing phase numbering or declaring a phase complete. Do not silently replace the existing ten-system acceptance model with a shorter summary.

## 3. Mandatory delivery lifecycle

Every work item follows this sequence:

1. **Recover:** record branch, HEAD, base, working-tree state and active PR. Preserve uncommitted work.
2. **Scope:** identify the exact roadmap system, acceptance criteria, dependencies, affected call paths and owner inputs.
3. **Trace:** inspect the actual implementation, Brain integration, data lineage, broker boundary and failure behavior.
4. **Implement:** deliver complete behavior, not stubs, disconnected modules, fake adapters or documentation-only completion.
5. **Prove:** add focused regression tests, negative/failure tests, restart/reconciliation tests and platform-specific coverage where applicable.
6. **Validate:** run compile, lint, static architecture/identity/safety audits, coverage synchronization, focused suites and full CI matrix.
7. **Review:** inspect the complete diff and verify no secrets, protected files, unrelated deletions, unsafe fallbacks or alternate authority paths were introduced.
8. **Record:** update the roadmap, implementation/coverage register and evidence record with actual command output and commit references.
9. **Accept:** mark the item accepted only when every applicable criterion has reproducible evidence and required owner-connected checks have passed.

A task may be implemented but not accepted. External prerequisites must remain explicitly DATA-GATED. A green subset of tests is not a substitute for the required full validation.

## 4. Mandatory architectural invariants

- CognitiveBrain alone forms market decisions. Analysts return evidence, not executable instructions.
- Completed bars only; no look-ahead.
- Unknown remains unknown. Missing, stale, incomplete, contradictory or unverified data cannot be silently defaulted to healthy or valid.
- Preserve source identity, provenance, timestamps, cutoff, freshness, uncertainty and decision lineage.
- Instrument-agnostic analysis; no forced strategy, timeframe, symbol or directional bias.
- Optional AI cannot be required for native reasoning or safe operation.
- All new exposure passes one canonical Brain admission and shared execution/risk boundary.
- Risk-reducing management and emergency close remain available during cognition/provider failure.
- Owner limits are distinct from analysis and cannot be weakened by agents.
- No live broker order, modification or cancellation during engineering validation.
- No claim of profitability or predictive edge without independent chronological and untouched holdout evidence.

## 5. Phase and system completion semantics

Use these statuses consistently:

| Status | Meaning |
|---|---|
| NOT STARTED | No verified implementation work has begun |
| IMPLEMENTING | Code or integration work is underway |
| IMPLEMENTED | Behavior exists in source; integration is not yet proven |
| INTEGRATED | Connected to the intended live application/Brain/execution path |
| VALIDATED | Required automated and review evidence passes |
| DATA-GATED | Correct implementation exists but named owner/provider data or entitlement is missing |
| BLOCKED | A specific dependency prevents further safe progress |
| PRODUCTION-ACCEPTED | All engineering, owner-connected, operational and release criteria are satisfied |

A phase/system is complete only when:
- All deliverables and acceptance criteria are closed.
- Its upstream dependencies are accepted.
- Actual call paths and integration are proven.
- Focused and full validation are green, with base-branch comparison and no unexplained failures.
- Identity, safety and configuration audits pass.
- Data-gated requirements are either closed with verified evidence or explicitly excluded from the supported production scope through owner approval.
- The roadmap contains a signed-off evidence record and explicit acceptance decision.

## 6. External and owner-connected acceptance

Code cannot manufacture provider entitlements, credentials, broker history, authoritative calendars, instrument specifications, account state or full-universe host performance. Where these are required:

- Implement and test the provider-neutral contract and failure behavior.
- Document the exact provider/source, fields, timestamps, entitlements, rate limits, history depth and owner action required.
- Keep affected capabilities DATA-GATED until the source is connected and evidence passes.
- Never infer that an adapter, environment variable or mock fixture means a real provider is configured.
- Do not ask the owner to paste secrets into chat. Credentials belong only in the owner's private runtime configuration.

## 7. Configuration and dashboard control contract

### Startup/deployment configuration
Every supported operator-configurable startup, deployment, integration, service, logging, observability and provider setting must:
- Be parsed and validated through environment configuration.
- Have a documented key, type, default, allowed values, security classification and operational effect in `.env.example`.
- Have a safe default; secrets must have blank placeholders.
- Be covered by configuration parsing and validation tests.

Do not expose mandatory identity rules, concept definitions, or safety invariants as overrideable environment knobs. These are architectural constraints, not preferences.

### Dashboard runtime control
The dashboard must let an authorized owner inspect and control supported runtime operations through authenticated, role-checked, validated API commands and durable state. Actions must be auditable and idempotent where possible. `.env` is not a runtime control database: the dashboard must not write to it. Changes to startup configuration require an explicit controlled restart/redeploy path.

No dashboard control may bypass the Brain, owner limits, risk checks, shared execution gateway or emergency safety behavior.

### Template completeness
Every new environment read must update `.env.example` and tests in the same change. A configuration inventory must compare environment-variable reads in application code against the template; unexplained omissions are release blockers. Do not add keys for unimplemented features or provider integrations.

## 8. Required release evidence

The final release dossier must include:
- Exact source commit, PRs and deployment artifact identity.
- System/phase acceptance matrix with links to evidence.
- Full concept method/lens coverage and actual Brain call-path audit.
- Full supported broker/instrument capability matrix.
- Historical data provenance, calibration, walk-forward and untouched holdout methodology/results.
- Full-universe performance measurements and resource budgets.
- End-to-end deterministic simulation from source evidence through decision, admission, sizing, intent, broker acknowledgement, position lifecycle, close and outcome.
- Fault-injection, restart, reconciliation, observability and recovery results.
- Security/configuration/secrets audit and dashboard authorization tests.
- Deployment, rollback, incident and emergency-stop runbooks.
- Explicit unresolved limitations and owner sign-off.

## 9. Release decision

Production readiness is not inferred from merged code, a passing unit suite, paper trading, a shadow run or an attractive backtest. Engineering acceptance, owner-connected qualification, controlled live qualification and production authorization are separate gates. Live trading requires explicit owner authorization after all preceding gates pass.

## 10. Deadline discipline

Agents must work continuously through all safe, actionable tasks and avoid repeated audits, unnecessary redesigns and status-only loops. They must not promise completion by an arbitrary deadline when the evidence or owner dependencies make that impossible. A genuine external dependency is recorded precisely; it is not hidden, guessed or bypassed.
