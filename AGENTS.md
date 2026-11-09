# Apex Trader — mandatory agent instructions

This file applies to every human contributor, coding agent, autonomous agent, reviewer and delegated engineering task operating in this repository. Read it before inspecting or changing code. These rules remain binding until the full redesign has passed production qualification.

## Authority and source of truth

1. `cognition/identity.py::IDENTITY_STATEMENT` is the one canonical identity source. Never rewrite, paraphrase into a competing identity, or introduce another identity authority.
2. `docs/APEX_Constitution.md` is binding engineering law.
3. `docs/APEX_FULL_REDESIGN_ROADMAP.md` defines the architecture systems and their acceptance gates.
4. `docs/APEX_CONCEPT_IMPLEMENTATION_PLAN.md` and `docs/APEX_CONCEPT_COVERAGE.md` define concept implementation and coverage status.
5. `docs/APEX_ENGINEERING_DELIVERY_CONTRACT.md` defines the mandatory delivery workflow and release evidence.
6. If documents appear inconsistent, do not silently choose or rewrite one. Record the conflict, preserve the stricter safety requirement, and reconcile it explicitly in the roadmap before claiming acceptance.

## Non-negotiable engineering invariants

- The CognitiveBrain is the sole market decision authority. Analytical modules emit evidence; they do not independently authorize direction or execution.
- Completed bars only for closed-bar reasoning. No look-ahead, fabricated history, or future-dated evidence.
- Unknown, missing, stale, incomplete or unverified data remains unknown. Do not manufacture a fallback and label it verified.
- Preserve provenance, source identity, timestamps, freshness, uncertainty and evidence lineage.
- Keep analysis instrument-agnostic. Do not hardwire a trading style, strategy, timeframe, symbol or directional bias.
- AI/LLM services are optional advisers. Apex must continue its native reasoning and safe operation when they are absent or failing.
- New exposure must pass the canonical Brain, portfolio/risk and shared execution boundaries. No direct broker route, alternate decision authority or bypass.
- Preserve risk-reducing position protection and emergency close capability. Never weaken execution, risk, reconciliation, owner limits or test assertions to obtain a green build.
- Do not claim profitability, predictive edge, production readiness or a completed capability without the required measured evidence.
- No live broker order placement, modification or cancellation during engineering or validation.

## Protected files and data

- The real repository `.env` is strictly off-limits. Do not open, read, search, print, copy, materialize, rename, recreate, overwrite, delete, stage, commit or otherwise access it. Do not run commands that may display or mutate it.
- Never change broker credentials, tokens, account selection, live trading settings, runtime databases, owner limits or production state without explicit owner authorization.
- `.env.example` is the safe, version-controlled configuration template. Update it whenever a supported operator-configurable environment variable is added, renamed, removed or changes meaning. Never put secrets in it.
- Do not overwrite or discard uncommitted user changes. Inspect Git status before any operation that could affect the worktree. Do not force-push, reset, clean, rebase destructively or rewrite history.

## Configuration contract

- Every operator-tunable startup, deployment, integration, observability and service configuration must be read from validated environment configuration and documented in `.env.example`.
- Do not introduce undocumented magic environment variables or user-facing configuration hidden in code, dashboard bundles or deployment scripts.
- Keep immutable identity rules, analytical definitions and mandatory safety invariants out of operator override controls. They are not tuning knobs.
- `.env` is startup/deployment configuration, not a live dashboard control channel. Runtime dashboard actions must use authenticated, authorized, validated APIs and durable state, with audit records. The dashboard must never edit or rewrite `.env`.
- Secret values must be redacted from logs, API responses, diagnostics and generated reports. Blank template values are placeholders, not evidence that a provider is configured.
- Do not add environment keys for features or providers that are not implemented. Inventory existing environment reads before claiming the template is complete.

## Mandatory workflow for every task

1. Recover repository, branch, HEAD, remote and working-tree state. Preserve all existing work.
2. Read this file and the relevant Constitution, roadmap and implementation plan sections.
3. State the exact acceptance criteria and dependencies. Reuse existing implementations; do not restart completed investigations.
4. Trace the actual call path and data provenance before changing behavior.
5. Implement the smallest complete change. Remove incorrect implementations only when the replacement is correct, tested and preserves safety.
6. Add regression tests for the behavior, failure modes, restart/reconciliation and relevant platform differences.
7. Run focused tests, relevant static/identity/safety audits, lint, compile checks and the full required CI matrix. Compare failures against the base branch; do not hide inherited or new failures.
8. Inspect the final diff for secret exposure, `.env` access, unintended deletions, bypasses, unsafe defaults and unrelated changes.
9. Update the relevant roadmap, coverage register, configuration template and evidence record.
10. Report exact commit/PR, changed files, commands actually run, pass/fail counts, audit results, unresolved dependencies and acceptance status. If a command was not run, say so. If external owner data is required, leave that gate DATA-GATED rather than fabricating acceptance.

## Completion and handoff rules

- “Implemented”, “integrated”, “validated”, “data-gated” and “production-accepted” are distinct states. Never collapse them.
- A merged PR or passing subset of tests is not production acceptance.
- Every phase/system must have an explicit evidence-backed acceptance record. Do not mark a downstream phase accepted while its upstream contracts are unverified.
- Work may proceed concurrently only where dependencies permit. Integration must respect the roadmap dependency order.
- Do not stop after writing a plan or listing findings when an actionable implementation is available. Continue through implementation and validation within the available access.
- Do not promise that all work will finish by a calendar deadline. Report blockers that genuinely require owner credentials, licensed feeds, broker history, infrastructure or explicit release authorization.
- Keep PRs unmerged until required checks pass and review confirms the safety invariants.
