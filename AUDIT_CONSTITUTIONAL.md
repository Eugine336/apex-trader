# APEX Constitutional Audit Violation Register

Audit date: 2026-08-15

> **Historical snapshot, not current-head sign-off.** This register captured the
> runtime and source state observed on 2026-08-15. Since then, the identity-led
> branch has changed the CognitiveBrain's native admission rules, evidence model,
> portfolio admission, runtime management sinks and execution-cost qualification.
> Some findings below are stale or partially resolved; others may still apply.
> Do not treat old line numbers, counts or CRITICAL/HIGH labels as a freshly
> verified verdict on today's branch. The current acceptance register is
> `docs/APEX_IDENTITY_IMPLEMENTATION_AUDIT.md`; every old finding needs a
> current-head call-graph/test recheck before it is marked resolved.

Scope audited:
- All Python files under `brain/`, `cognition/`, `llm/`, `management/`, `execution/`, `tick/`, `scanner/`, `adaptive/`, `decision/`, `trigger/`, `entry/`, `governor/`, `risk/`, and `planning/`.
- `dashboard/state_brain.py` was also inspected because the starting context identified legacy vote display there.

Method:
- Searched every scoped Python file for `LONG`, `SHORT`, `FLAT`, `vote`, `consensus`, `direction`, `lean`, and `bias`.
- Traced the reachable runtime paths through the cognition loop, Brain, LLM provider layer, management loop, execution worker, planning layer, scanner/candle-close path, and adaptive learning layer.
- Distinguished live/default behavior from initialized legacy/shadow code and dormant compatibility APIs.
- In the original 2026-08-15 audit session, this file was documentation only and no violations were fixed then. Subsequent fixes and remaining gates are tracked in the current identity audit linked above.

Severity rubric:
- CRITICAL: live runtime path can originate, manage, or reject opportunities from a constitutionally forbidden representation.
- HIGH: live or readily enabled path can influence execution, management, provider resilience, or opportunity selection in a forbidden way.
- MEDIUM: initialized, wireable, adaptive, or dormant legacy path preserves a forbidden concept and can affect behavior when invoked.
- LOW: semantic/display/schema debt that preserves forbidden vocabulary or mental models but is not current execution authority.

## PREMATURE_COMPRESSION

### V-001: Native Brain computes a directional lean before hypothesis formation
- **Severity**: CRITICAL
- **Philosophy Rule**: 1, 2, 3, 5, 6, 7, 8, 10, 20, 30
- **File(s)**: `cognition/brain_reasoning.py:91-94`, `cognition/brain_reasoning.py:260-326`, `cognition/brain_reasoning.py:361-368`, `cognition/brain_reasoning.py:372-451`, `cognition/brain_reasoning.py:570-578`, `cognition/brain.py:313-377`, `cognition/brain.py:1971-2087`, `cognition/loop.py:687-714`, `cognition/loop.py:764-787`, `cognition/loop.py:831-847`
- **Current Status**: RESOLVED for pre-hypothesis directional collapse. The native regime record preserves structure displacement, order-flow sign, sweep side, per-timeframe trend readings and conflicts independently. Hypothesis generation derives direction within each competing thesis; no global `directional_lean` is produced. Leading-hypothesis ranking uses uncalibrated reasoning-strength metadata only; native origination still requires activated opportunity evidence, verified expected return, costs, geometry and shared risk admission.
- **Actual Code Behavior**: `_classify_regime()` returns independent `structure`, `order_flow`, `liquidity_sweep_side`, volatility, momentum, and conflict fields. `_agreement()` measures independent evidence provenance and structural coherence without producing a direction. `generate_hypotheses()` forms continuation, liquidity-reversal, exhaustion and counter-hypotheses from the relevant evidence dimensions. `leading()` ranks the explanatory hypotheses; it does not create a pre-collapsed market bias.
- **Execution Path**: `CognitionLoop.run_once()` / `reason_symbol_now()` -> `EvidenceConsolidator.build()` -> `CognitiveBrain.reason()` -> native evidence analysis -> competing hypothesis formation and self-criticism -> selected hypothesis -> `_originate_independent()` -> verified opportunity/return/cost/geometry/quorum/portfolio gates -> possible `OPEN_CAMPAIGN`.
- **Why It Violates**: The Brain is supposed to reason from observations into hypotheses, opportunities, expected value, risk, and only then direction. Here a scalar directional lean is created before the Brain completes hypothesis formation, and that lean can become execution authority during provider degradation.
- **Information Lost**: The independent shapes of displacement, order flow, liquidity sweep behavior, momentum state, time horizon, contradictory evidence, and conditional alternatives are reduced to one net direction before opportunity discovery. Mixed evidence can be demoted to `FLAT` by score cancellation before a conditional opportunity is preserved.
- **Root Cause**: Legacy bias/vote machinery was moved inside "native Brain" code as a helper instead of being removed as a pre-cognition directional collapse.

### V-002: Legacy top-level `direction` and `confidence` still drive origination
- **Severity**: CRITICAL
- **Philosophy Rule**: 1, 5, 6, 7, 8, 9, 10, 16, 19, 20
- **Current Status**: RESOLVED for origination authority. `_from_opinion()` now requires a first-class actionable opportunity; a legacy top-level direction/confidence reply without that object remains observational and cannot open a campaign. Raw advisor confidence is metadata only.
- **File(s)**: `llm/reasoner.py:389-454`, `llm/reasoner.py:573-589`, `llm/reasoner.py:694-709`, `cognition/brain.py:390-417`, `cognition/brain.py:515-532`, `cognition/brain.py:647-704`, `cognition/brain.py:779-855`, `cognition/brain.py:857-907`
- **Actual Code Behavior**: `LLMOpinion` retains top-level `direction` and `confidence` for compatibility and descriptive metadata. `CognitiveBrain._from_opinion()` requires a first-class `driving_opp` and `driving_opp.is_actionable` before opening. A legacy direction/confidence-only reply remains observational. Confidence is neither aggregated nor used as probability, qualification, action permission or sizing.
- **Execution Path**: `CognitionLoop._reason_over_symbol()` -> `CognitiveBrain.reason()` -> `LLMReasoner.reason()` -> structured opportunity set -> `_from_opinion()` -> activated-opportunity, geometry, verified-return, cost, quorum and risk gates -> decision. A direction/confidence-only legacy reply cannot reach `OPEN_CAMPAIGN`.
- **Why It Violates**: The execution layer is allowed to carry direction, but the Brain's cognitive decision path still treats top-level direction/confidence as sufficient structure for action in the legacy format. That preserves "LONG 0.55" as a valid cognitive representation.
- **Information Lost**: Opportunity lifecycle, competing opportunity set, timing readiness, execution state, conditional hypotheses, and "opportunity exists but not yet" can be bypassed by a single direction/confidence pair.
- **Root Cause**: Backward compatibility with pre-harvesting LLM replies was kept as behavior-equivalent rather than being isolated from execution authority.

### V-003: Missing opportunity lifecycle state is treated as actionable
- **Severity**: HIGH
- **Philosophy Rule**: 6, 8, 9, 16, 19, 20
- **File(s)**: `cognition/contracts.py:151-187`, `cognition/contracts.py:513-526`, `cognition/contracts.py:547-580`, `cognition/brain.py:647-671`, `cognition/brain.py:698-704`, `llm/reasoner.py:423-448`
- **Actual Code Behavior**: `Opportunity.from_reply()` maps missing or unrecognized `state` to `OpportunityState.UNKNOWN`. `Opportunity.is_actionable` returns true for `UNKNOWN`, explicitly preserving legacy behavior. The Brain's activation gate then permits origination when the driving opportunity is unknown-state but directional.
- **Execution Path**: LLM returns an opportunity dict without a valid lifecycle state -> `LLMReasoner._build_opinion()` -> `CognitiveBrain._build_opportunity_set()` -> `Opportunity.from_reply()` -> `CognitiveBrain._select_driving_opportunity()` -> `activation_ok` passes -> action gate can open a campaign.
- **Why It Violates**: The constitution requires the Brain to distinguish an identified opportunity from an activated/executable opportunity. Treating unknown state as actionable collapses "I see something forming" into "execute now".
- **Information Lost**: Activation conditions, confirmation state, entry readiness, weakening/exhaustion state, and "wait" versus "act" are erased when state is omitted.
- **Root Cause**: Compatibility choice to keep old single-direction replies executable even though the newer philosophy requires explicit lifecycle reasoning.

### V-004: Vote and developing-bias adapters remain wireable into MarketState
- **Severity**: MEDIUM
- **Philosophy Rule**: 1, 2, 3, 6, 10, 11, 20
- **Current Status**: RESOLVED. `set_vote_source`, `set_developing_source`, `evidence_from_votes`, and `evidence_from_developing_bias` are absent from the live cognition surface. Forming-bar WorldModel updates are not admitted to `MarketState` and are no longer wired as cognition wake triggers. Concept evidence must be verified completed OHLC.
- **File(s)**: `cognition/loop.py:80-91`, `cognition/loop.py:116-132`, `cognition/loop.py:269-285`, `cognition/loop.py:603-623`, `cognition/evidence_adapters.py:136-167`, `cognition/evidence_adapters.py:201-213`, `cognition/evidence_adapters.py:246-286`, `cognition/evidence_adapters.py:299-344`, `event_driven_bootstrap.py:2445-2464`, `event_driven_bootstrap.py:2881-2920`
- **Actual Code Behavior**: The consolidator no longer exposes vote or developing-bar evidence setters. `evidence_from_votes()` and `evidence_from_developing_bias()` have been removed. Developing WorldModel updates are used only as non-directional wake triggers; they are not inserted into `MarketState`. The concept source admits verified completed-OHLC evidence, while current quote/execution observations retain their own provenance.
- **Execution Path**: The developing WorldModel remains isolated to the position-management advisory. It has no callback into the cognition loop. Brain wakes come from verified tick-change observations and completed candle-close events; reasoning consolidates completed OHLC and separately verified current-state evidence.
- **Why It Violates**: The live API surface still accepts a raw vote panel and can leak directional conviction magnitude as evidence confidence. Even though the sanitizer strips direction keys, the confidence/weight lineage remains vote-derived.
- **Information Lost**: If the source supplies only a vote confidence, the adapter cannot distinguish certainty in an observation from confidence in a directional prediction.
- **Root Cause**: Transitional compatibility path retained while moving from vote panels to observation evidence.

## DIRECTIONAL_AUTHORITY

### V-005: Planning layer recreates weighted directional agreement and emits BUY/SELL plans
- **Severity**: HIGH
- **Philosophy Rule**: 1, 3, 4, 6, 10, 16, 19, 20
- **Historical files**: `planning/models.py`, `planning/trade_planner.py`, `planning/calibrator.py`, `planning/outcome_logger.py`, and `core/system_context.py` planner wiring (all removed in PR #892).
- **Historical behavior**: the retired implementation used scanner direction, signed timeframe alignment and weighted directional agreement to produce BUY/SELL plans.
- **Current disposition (PR #892, CI passed; awaiting merge)**: the entire `planning/` package, `TradePlanner`, `PlannerConfig`, planner calibrator/outcome journal, `SystemContext` fields and initialization, `AppConfig.planner`, tuning adapter and expected-tunable registration were deleted. The constitutional guard now fails if any retired planning module is reintroduced and no longer excludes the planner from call scanning. No active execution route depended on the no-op planner. The shared Brain campaign and execution/portfolio/broker safety path remains.
- **Why It Violates**: This is a directional voting/weighted-agreement architecture. It asks whether scanner, decision engine, RL, and adaptive layers agree with a preselected side rather than asking what opportunities exist and whether any are actionable.
- **Information Lost**: The planner collapses separate market observations, RL state, and adaptive evidence into one directional agreement scalar and one confidence scalar, losing disagreement shape and alternative opportunity candidates.
- **Root Cause**: Pre-cognition trade-planning architecture remains instantiated for compatibility/shadow use after the Cognitive Brain became the intended authority.

### V-006: Within-cycle selector drops opposing opportunities by top candidate direction
- **Severity**: MEDIUM
- **Philosophy Rule**: 9, 10, 18, 20
- **File(s)**: `scanner/cycle_selection.py:1-10`, `scanner/cycle_selection.py:16-55`
- **Actual Code Behavior**: `select_cycle_candidates()` sorts candidates by score/EV, takes the top candidate's `direction` as `winning_direction`, keeps only candidates matching that direction, and drops every opposing-direction candidate in the same cycle.
- **Execution Path**: Dormant/compatibility path: any legacy caller using `select_cycle_candidates(items)` or the compatibility import path described in the module docstring executes the direction lock. A current in-repo production caller was not found during this audit.
- **Why It Violates**: The market can contain a LONG swing and SHORT scalp at the same time. Dropping all opposing candidates because one direction "wins" forces a single-cycle directional label where the constitution requires competing opportunities to coexist until portfolio/risk reasoning ranks them.
- **Information Lost**: Opposing-horizon opportunities, hedge/scalp opportunities, and conditional alternatives are discarded before portfolio-level opportunity comparison.
- **Root Cause**: Legacy over-trading guard preserved as a simple direction lock instead of being replaced by opportunity-level capital allocation.

### V-007: Currency-strength and liquidity modules still emit directional recommendations
- **Severity**: MEDIUM
- **Philosophy Rule**: 1, 3, 4, 6, 9, 10
- **File(s)**: `brain/currency_strength.py:50-68`, `brain/currency_strength.py:138-156`, `brain/currency_strength.py:218-257`, `brain/liquidity_mapper.py:25-32`, `brain/liquidity_mapper.py:219-242`, `brain/liquidity_mapper.py:286-300`, `brain/liquidity_mapper.py:396-445`
- **Actual Code Behavior**: `CurrencyStrengthMeter` produces `best_pair_long` and `best_pair_short`, documents "Always trade the strongest currency AGAINST the weakest", and exposes `get_pair_alignment(pair, analysis, direction)` that scores whether a requested `LONG` or `SHORT` is aligned. `LiquidityMapper` stores `liquidity_bias` and `classify_sweep_reaction()` returns `(kind, direction, confidence)` with explicit `LONG`, `SHORT`, or `NEUTRAL`.
- **Execution Path**: Module API path: callers invoking `CurrencyStrengthMeter.analyze()` receive best long/short pair recommendations; callers invoking `get_pair_alignment()` or `LiquidityMapper.classify_sweep_reaction()` receive directional scores. `get_pair_alignment()` and `classify_sweep_reaction()` did not have current in-tree production consumers in the audited runtime search, but the APIs remain available and tested as module outputs.
- **Why It Violates**: Analytical modules should report relative movement, sweep events, proximity, absorption, and reaction measurements, not produce trade direction or alignment scores.
- **Information Lost**: Relative currency rankings and liquidity interactions are compressed into side-specific recommendations instead of remaining observations for the Brain to interpret in context.
- **Root Cause**: Older modules were written as strategy components and still expose strategy-like outputs even where newer paths try to treat them as evidence.

## MANAGEMENT

### V-008: Cognitive Brain has a live legacy direction-based management tree
- **Severity**: HIGH
- **Current Status**: RESOLVED. Missing management thesis/opportunity/action fields now produce HOLD. The direction/confidence-based fallback has been removed; explicit reverse and scale-in requests require separate Brain origination and shared risk admission.
- **Philosophy Rule**: 6, 7, 17, 18, 20
- **File(s)**: `cognition/brain.py:1088-1145`, `cognition/brain.py:1140-1169`, `cognition/brain.py:1177-1230`, `llm/reasoner.py:843-860`, `llm/reasoner.py:987-1003`, `llm/reasoner.py:1065-1080`, `cognition/loop.py:1087-1105`, `cognition/loop.py:1327-1370`
- **Actual Code Behavior**: `CognitiveBrain.manage()` uses `_decide_management_thesis()` only when a management action, thesis state or opportunity status is present. If all are absent, it records degraded management data and returns HOLD. `_decide_management_legacy()` is not present. The thesis tree does not use confidence to authorize, veto, reverse or scale exposure; reverse and scale-in requests cannot create new exposure and require separate Brain origination.
- **Execution Path**: `CognitionLoop._manage_open_positions()` periodic backstop or `_manage_symbol()` event path -> `_manage_one()` -> `EvidenceConsolidator.build()` -> `CognitiveBrain.manage()` -> explicit thesis/action/opportunity fields -> thesis-based management decision; missing fields -> HOLD -> shared management sink.
- **Why It Violates**: Management is supposed to continuously re-evaluate the current opportunity and campaign thesis. The fallback still asks whether the current market opinion's direction aligns or opposes the held side.
- **Information Lost**: Thesis evolution, opportunity replacement, expected value over flat, execution feasibility, and risk-adjusted alternatives can be bypassed when a management reply is parsed as legacy direction/confidence.
- **Root Cause**: Backward-compatible management fallback was retained to handle older/minimal LLM replies.

### V-009: PositionWorker discretionary scan-direction and score exits
- **Severity**: HIGH
- **Status**: RESOLVED for the production event-driven runtime; offline test/backtest fixtures retain explicit constructor-only coverage.
- **Current Code Behavior**: RiskConfig no longer exposes or parses legacy mechanical-exit environment switches. The live bootstrap unconditionally sets `discretionary_exits_enabled=False` and `scan_directional_exits_enabled=False`; there is no operator setting that can reactivate deterministic TP/BE/trailing/stall/invalidation/conviction/HTF exits. Hard stop-loss, weekend/session/spread protection and the absolute-profit safety backstop remain active. The old discretionary methods are exercised only by isolated WorkerConfig fixtures in tests/backtests.
- **Execution Path**: Production `PositionEvaluator` -> `PositionWorker.evaluate()` with discretionary checks hard-disabled; independent safety-floor checks remain active.
- **Regression Evidence**: `tests/test_legacy_mechanical_fallback_config.py` sets both retired environment variables to true and proves they are ignored; it also asserts the live bootstrap hard-disables discretionary exits.
### V-010: Breakeven re-entry shortcut
- **Severity**: HIGH
- **Status**: RESOLVED in the exact-ticket Brain-management increment.
- **Resolution**: The no-op ReEntryManager and its M5/FVG/OB re-arm logic were removed. The manager never submitted an order; it only emitted an observational event after checking a separate directional setup. The SystemContext field/wiring, close-handler invocation, dormant cooldown state, RE_ENTRY_ARMED event, re_entry_eligible snapshot/trade-model flag, package export and `management/re_entry.py` module are deleted. A closed campaign returns to the normal CognitiveBrain observation cycle. No re-entry-specific direction or zone can authorize or suppress a new opportunity.
- **Regression evidence**: The SystemContext test now asserts that ReEntryManager wiring is absent; full CI remains required on the final PR head.

### V-011: Portfolio reallocation fails open when Brain is unavailable
- **Severity**: MEDIUM
- **Status**: RESOLVED in the current mainline implementation.
- **Resolution**: `_reason_reallocation()` returns no output with verdict `unavailable` when the Brain or its management entrypoint is absent. Exceptions return `fault` and no output. `_maybe_reallocate()` skips any candidate without a Brain-authored, translatable management action. The registry cannot synthesize a trim or fraction. Keep this fail-closed contract under regression coverage.

### V-012: Legacy TradeManager still embodies mechanical management
- **Severity**: MEDIUM
- **Philosophy Rule**: 17, 18, 20
- **File(s)**: `management/trade_manager.py:135-145`, `management/trade_manager.py:387-435`, `management/trade_manager.py:660-736`, `management/trade_manager.py:778-835`, `management/__init__.py:8`, `event_driven_bootstrap.py:1920`
- **Actual Code Behavior**: `TradeManager.update()` still runs stop loss, TP1, breakeven, trailing, TP2 adjustment, M5 structure exit, and time-based stall exit. `_check_structure_exit()` closes on M5 CHoCH/BOS against the trade after a time threshold. `_adjust_tp2()` extends/tightens target from M5 continuation/counter events. `_check_stall()` closes based on fixed per-timeframe stall windows and flat PnL.
- **Execution Path**: No current `SystemContext` initialization of `TradeManager` was found; `event_driven_bootstrap.py` explicitly sets `trade_manager` to `None`. The class is still exported from `management.__init__`, so any legacy caller using `TradeManager.update()` executes this mechanical management path.
- **Why It Violates**: If invoked, management is stop/target/structure/timer logic rather than continuous cognitive opportunity evaluation.
- **Information Lost**: Current opportunity state, better opposing opportunity, thesis evolution, and EV of hold/exit/reverse are not represented as first-class management inputs.
- **Root Cause**: Retired mechanical manager remains in the repository as an exported legacy component.

## PROVIDER_RESILIENCE

### V-013: Council degradation is observed but not consistently enforced
- **Severity**: HIGH
- **Philosophy Rule**: 12, 13, 21, 29, 30, 32, 33, 36
- **Current Status**: RESOLVED for action authority. Unknown or below-quorum advisor coverage cannot authorize the advisor-assisted path; the Brain instead attempts native qualification using its independently formed hypotheses. `CognitionConfig.min_advisors_for_action` defaults to 1 (`.env.example` agrees); deployments may require a larger quorum. Partial/unknown coverage is recorded, not converted into a confidence multiplier. The Brain no longer accepts a calibration tracker or applies a calibration adjustment.
- **File(s)**: `cognition/brain.py:452-471`, `cognition/brain.py:598-604`, `cognition/brain.py:1511-1543`, `config.py:3337-3349`, `config.py:3754-3757`, `llm/reasoning_orchestrator.py:667-702`, `cognition/loop.py:336-345`
- **Actual Code Behavior**: `_extract_advisor_counts()` returns unknown when no council metadata exists. The configured quorum defaults to 1 and is enforced against known `advisors_responded` counts. When coverage is unknown or below the configured quorum, `reason()` bypasses the advisor opinion for origination and invokes `_originate_independent()`; if native qualification also fails, it returns an observe decision. Partial/unknown coverage is recorded and logged, never translated into a confidence penalty. Multi-advisor consultation remains a separate provider-configuration concern.
- **Execution Path**: reasoning consultation evidence -> `_extract_advisor_counts()` -> quorum check -> advisor-assisted origination only when quorum is met; otherwise native hypothesis qualification -> verified expected return/cost/activation/geometry/shared-risk gates -> possible campaign, or observe. Partial and unknown coverage do not modify confidence.
- **Why It Violates**: The constitution requires explicit degraded capacity and confidence in the cognitive process. Missing council metadata or a partial panel must not be silently equivalent to healthy cognition.
- **Information Lost**: The system loses whether the decision was made by one advisor, several advisors, or no functioning council, and whether absent advisors represent quota exhaustion, timeout, or provider outage.
- **Root Cause**: Compatibility with single-reasoner and no-orchestrator deployments weakens provider/council guarantees.

### V-014: Provider budgets are optional and unmetered by default
- **Severity**: HIGH
- **Philosophy Rule**: 21, 22, 23, 24, 25, 27, 28, 34, 37
- **File(s)**: `config.py:3375-3385`, `llm/provider_budget.py:8-11`, `llm/provider_budget.py:76-99`, `llm/provider_budget.py:138-141`, `llm/client.py:454-475`, `llm/client.py:528-611`, `llm/client.py:655-663`, `llm/client.py:705-733`, `llm/reasoner.py:266`, `llm/reasoner.py:957-965`
- **Actual Code Behavior**: RPM/RPD/TPM/TPD defaults are all zero, which `ProviderBudget` treats as unlimited. `attach_budget()` returns without attaching any meter unless at least one positive limit is configured. When no budget is attached, `LLMClient.complete()` sends requests without quota admission. Prompt truncation is a fixed `_MAX_USER_PROMPT_CHARS = 60000` character slice, not a provider/model context-window or remaining-quota decision.
- **Execution Path**: `build_client(config)` -> `attach_budget(client, config)` -> limits all zero -> no budget -> `LLMClient.complete()` -> no `budget.admits()` check -> provider request. Prompt path: `LLMReasoner._build_user_prompt()` serializes evidence and slices to 60000 chars before the provider-specific request is built.
- **Why It Violates**: Provider limits are supposed to be first-class operating reality. With default zero limits, the system can treat an external free-tier provider as infinite unless the operator preconfigures exact limits.
- **Information Lost**: Remaining quota, reset time, request cost versus scarce capacity, provider-specific context limits, and why a provider was selected or rejected are absent from default routing.
- **Root Cause**: Quota accounting was added as opt-in configuration rather than mandatory provider capability modeling.

### V-015: Provider diversity and local foundation are optional, not architectural requirements
- **Severity**: HIGH
- **Philosophy Rule**: 13, 14, 21, 25, 30, 33, 35, 36, 37
- **File(s)**: `config.py:3315-3335`, `config.py:3337-3349`, `llm/client.py:85-90`, `llm/reasoning_orchestrator.py:667-702`, `llm/model_manager.py:167-205`, `cognition/brain.py:332-345`, `cognition/brain.py:357-370`, `cognition/brain.py:1971-2087`
- **Actual Code Behavior**: `LLMConfig` has a single primary provider/model and `extra_models` defaults empty. `consult_multi` defaults false. The orchestrator assembles and returns however many engines are configured, including one. Ollama/local is supported as a provider shape, but no local model is required as baseline. If providers fail, the Brain either returns `REASONER_UNAVAILABLE` or uses native independent origination, which is not local AI and is affected by V-001's directional lean.
- **Execution Path**: `build_client()` creates the primary client; `build_reasoning_orchestrator()` adds primary and optional extras; with no extras it still returns a single-engine router. If the reasoner is unavailable, `CognitiveBrain.reason()` either tries `_originate_independent()` or returns unavailable.
- **Why It Violates**: The constitution says APEX must be architected around a provider pool and local inference as the long-term foundation. The current code permits a one-provider deployment with no local baseline and no minimum diversity requirement.
- **Information Lost**: Cognitive capacity state cannot reliably express "pool health" when there may be no pool. A single provider's quota, outage, or auth failure can still remove external cognition.
- **Root Cause**: Multi-provider/local support exists as optional configuration rather than a required architecture invariant.

### V-016: Provider-unavailable state still carries `direction=FLAT`
- **Severity**: HIGH
- **Philosophy Rule**: 8, 13, 30, 31, 34
- **File(s)**: `cognition/brain.py:57-70`, `cognition/brain.py:2159-2179`, `cognition/loop.py:792-799`
- **Actual Code Behavior**: `_reasoner_unavailable()` correctly creates `DecisionType.REASONER_UNAVAILABLE`, but the returned `BrainOutput` still has `direction=FLAT`. The loop logs every output as `dir={output.direction}`, so provider unavailability is surfaced in logs as `dir=FLAT` unless the consumer also inspects `decision_type`.
- **Execution Path**: `CognitiveBrain.reason()` detects unavailable/degraded reasoner and cannot independently originate -> `_reasoner_unavailable()` -> `BrainOutput(direction=FLAT)` -> `CognitionLoop._reason_over_symbol()` logging and any downstream consumer reading only `output.direction`.
- **Why It Violates**: Provider failure must not equal `FLAT`. A separate decision type helps, but the top-level output still encodes infrastructure failure as a market direction field.
- **Information Lost**: Consumers that read `BrainOutput.direction` cannot distinguish "no opportunity", "continue observing", "provider unavailable", and "market flat".
- **Root Cause**: `BrainOutput` has a mandatory FLAT default direction field from the old direction-centric output contract.
- **Historical resolution (PR #890, pending CI):** the top-level `BrainOutput.direction` field, deprecation getter/setter, serialization key and all production constructor/loop writes were removed. Market side remains on the campaign/preferred-opportunity contract. Provider faults are represented by `decision_type` and provider-status metadata, never a synthetic FLAT direction.

### V-017: Cognition and management gates are configurable fail-open/off surfaces
- **Severity**: MEDIUM
- **Philosophy Rule**: 16, 30, 31
- **File(s)**: `config.py:3717-3739`, `config.py:3873-3878`, `cognition/gate.py:18-20`, `cognition/gate.py:86-106`, `cognition/gate.py:120-151`, `core/system_context.py:1582-1601`, `event_driven_bootstrap.py:1076-1090`
- **Actual Code Behavior**: `CognitionConfig.gate_mode` defaults to `off`. In `shadow` or `veto` modes, missing Brain, cold start, and gate faults are fail-open. The management gate helper returns true if no gate is wired; only `authoritative` mode fail-closes on absent cognition.
- **Execution Path**: Legacy entry or exposure-adding management path calls `CognitionGate.evaluate()` or `_management_gate_allows()` -> if gate is off/missing/soft and Brain has no read, allow can be true. In the current single-path Brain-origination mode this may be less central, but the fail-open gate remains a configurable execution surface.
- **Why It Violates**: If legacy execution surfaces are present, absent cognitive availability should be a distinct operational state, not permission to proceed without cognition.
- **Information Lost**: The reason an entry/scale was allowed can be "no Brain/gate off" rather than a reasoned opportunity decision.
- **Root Cause**: Shadow migration controls were kept available after the constitution moved to a single cognitive authority.
- **Historical resolution (PR #890, pending CI):** CognitionGate and ManagementGate had no production evaluate() callers; they were only constructed and surfaced as status. Both modules, their SystemContext/Governance/observability wiring, gate-mode, freshness and single-path configuration, and associated environment variables were removed. This did not remove an active safety check. The canonical Brain loop and shared execution/portfolio/broker/owner-limit controls remain. `TRADING_ENABLED=false` remains the new-exposure kill switch.

## COUNCIL

### V-018: Consultation ledger turns independent advisors into majority-direction scorecards
- **Severity**: MEDIUM
- **Philosophy Rule**: 12, 13, 29, 34
- **File(s)**: `cognition/consultation_ledger.py:1-30`, `cognition/consultation_ledger.py:42-85`, `cognition/consultation_ledger.py:109-126`, `cognition/consultation_ledger.py:170-214`, `cognition/loop.py:336-345`
- **Actual Code Behavior**: The ledger normalizes every advisor to `LONG`, `SHORT`, or `FLAT`, computes a council majority by direction count with confidence tie-breaks, records dispersion as "fraction not in majority", and grades each advisor by agreement with the majority.
- **Execution Path**: `EvidenceConsolidator.build()` runs a reasoning consultation -> adds reasoning evidence -> if `_consult_ledger` is wired, `ConsultationLedger.record(consult, ...)` -> `_council_majority()` -> per-advisor `agree_majority` scorecard and record storage.
- **Why It Violates**: The council should create independent cognition, not a majority vote. Grading an advisor by agreement with the majority rewards directional conformity rather than reasoning quality, opportunity identification, calibration, or useful dissent.
- **Information Lost**: Thesis quality, invalidation quality, useful minority hypotheses, and cases where the majority was directionally wrong are not first-class in the ledger's score.
- **Root Cause**: Observability/learning layer was built around old council-vote concepts and kept after the council philosophy changed.

### V-019: Reasoning orchestrator and LLMOpinion preserve vote-like direction/confidence surfaces
- **Severity**: MEDIUM
- **Philosophy Rule**: 6, 12, 20
- **File(s)**: `llm/reasoning_orchestrator.py:42-56`, `llm/reasoning_orchestrator.py:205-228`, `llm/reasoning_orchestrator.py:500-526`, `llm/reasoner.py:573-589`, `llm/reasoner.py:694-709`
- **Actual Code Behavior**: `EngineOpinion` stores advisor `direction` and `confidence`; `_consult_one()` extracts those fields from every advisor opinion; panel logging renders `name direction(conf)`. `LLMOpinion.as_evidence()` returns a "Vote-like evidence object" with `module`, `direction`, `confidence`, and `weight`.
- **Execution Path**: Orchestrator consultation -> each `ReasoningEngine` returns `LLMOpinion` -> `_consult_one()` extracts direction/confidence into `EngineOpinion` -> `_log_panel()` logs advisors as direction/confidence. Future or external panel paths can call `LLMOpinion.as_evidence()` and receive a vote-shaped object.
- **Why It Violates**: Even when comments say direction is observability only, the data model and log line still frame advisors as directional voters. The `as_evidence()` method explicitly preserves a vote-shaped interface.
- **Information Lost**: Advisor-specific hypotheses, uncertainty, opportunity state, and invalidation can be overshadowed by the direction/confidence shorthand.
- **Root Cause**: Legacy observability and panel-integration contracts were not redesigned around thesis/opportunity objects.

## OPPORTUNITY_HARVESTING

### V-020: Candidate model still derives opportunities from votes and vote counts
- **Severity**: MEDIUM
- **Philosophy Rule**: 1, 9, 10, 18, 20
- **File(s)**: `brain/candidate_models.py:36-58`, `brain/candidate_models.py:69-105`, `brain/candidate_models.py:126-132`
- **Actual Code Behavior**: `Candidate` stores `direction`, `score`, `vote_count`, and contributing modules/timeframes. `Candidate.from_opportunity()` reads `opportunity.votes`, extracts module/timeframe provenance from votes, uses `opportunity.confidence` as score, and sets `vote_count=len(modules)`. `CandidatePosition` documentation says positions are linked back to modules/timeframes that "voted it open".
- **Execution Path**: Dormant/legacy path: any code constructing a `Candidate` from an opportunity-like object through `Candidate.from_opportunity()` converts vote clusters into candidate objects, which can then be consumed by candidate selection/allocation paths.
- **Why It Violates**: Candidate/opportunity identity remains tied to vote clusters and vote counts rather than a first-class discovered opportunity with independent thesis, lifecycle, EV, and invalidation.
- **Information Lost**: The reasons the modules observed something are reduced to which modules voted, how many voted, and a composite confidence.
- **Root Cause**: Opportunity/candidate models were layered on top of the old vote-cluster representation.

### V-021: SignalLedger and ModuleGovernor still train on directional module reads
- **Severity**: HIGH
- **Philosophy Rule**: 1, 3, 6, 12, 20
- **File(s)**: `adaptive/signal_ledger.py:1-20`, `adaptive/signal_ledger.py:67-80`, `adaptive/signal_ledger.py:113-121`, `adaptive/signal_ledger.py:250-313`, `adaptive/signal_ledger.py:610-633`, `adaptive/module_governor.py:1-25`, `adaptive/module_governor.py:60-74`, `adaptive/module_governor.py:413-420`, `core/system_context.py:551-580`, `event_driven_bootstrap.py:5424-5444`, `scanner/candle_close_handler.py:70-84`, `event_driven_bootstrap.py:1308-1322`
- **Actual Code Behavior**: `SignalLedger` stores every signal with `direction`, ignores non-`LONG`/`SHORT` signals, and grades whether price moved in the predicted direction. `ModuleGovernor` describes modules as directional voters and suppresses votes by mode. `SystemContext` initializes `SignalLedger`; the event-driven path attempts to feed it directional reads from world-model entry zones; `CandleCloseHandler` accepts `module_governor`.
- **Execution Path**: System startup initializes `ctx.signal_ledger`; candle/event path reads best world-model zone `direction` and `conviction` -> attempts `ctx.signal_ledger.record_signal(...)`; grading later computes signed move and `direction_correct`. Module governor can be passed into candle-close handling to suppress module votes. The observed call shape in `event_driven_bootstrap.py:5439-5444` appears inconsistent with `SignalLedger.record_signal(signal: SignalRecord)`, but the intended live path is still directional.
- **Why It Violates**: The adaptive layer continues to define module value by whether its directional read was right, reinforcing the old "modules vote LONG/SHORT and we reweight them" architecture.
- **Information Lost**: Module observational quality, causal usefulness, regime-specific contribution, and non-directional evidence accuracy are reduced to directional correctness.
- **Root Cause**: Learning/governance systems were built around module-vote performance before the observation-only constitution was adopted.

### V-022: Adaptive timeframe weighting used as a latent directional influence — FIXED
- **Status**: FIXED in PR #810.
- **Remediation**: Removed the XAUUSD-specific and generic weighted-direction hooks from `brain/decision_core.py`; removed SystemContext registration, scheduler recompute path, configuration fields, dashboard controls, and the provider implementation.
- **Current contract**: Timeframe observations remain available as evidence. They are not collapsed into a pre-Brain weighted directional score. The Cognitive Brain forms and challenges LONG, SHORT, and FLAT hypotheses from underlying measurements.
- **Regression coverage**: Removed provider-hook tests and added mirrored LONG/SHORT Wyckoff spring/upthrust tests.

## LEGACY

### V-023: Legacy vote terminology remains on live/deployed adaptive and dashboard paths
- **Severity**: LOW
- **Philosophy Rule**: 1, 3, 12, 20
- **File(s)**: `adaptive/module_governor.py:1-25`, `adaptive/module_governor.py:60-74`, `adaptive/module_governor.py:413-420`, `dashboard/state_brain.py:100-116`, `dashboard/state_brain.py:131-148`
- **Actual Code Behavior**: `ModuleGovernor` documentation and governed module list describe "voting modules" and "directional votes". `dashboard/state_brain.py` still iterates `votes`, stores per-vote `direction`, `confidence`, `weight`, and `signed`, then counts `long_count`, `short_count`, and `neutral_count` alongside consensus fields.
- **Execution Path**: Module governor is initialized/used by adaptive governance paths when enabled. Dashboard state building reads legacy result/vote objects and renders vote counts for operator visibility.
- **Why It Violates**: Even where this is observational/display behavior, it preserves the wrong operator and engineering model: modules vote, consensus counts, and direction counts explain state.
- **Information Lost**: The UI/adaptive vocabulary hides the actual observations, hypotheses, and opportunity conditions behind vote counts.
- **Root Cause**: Semantic cleanup did not reach all governance and dashboard surfaces.

### V-024: Retired legacy components remain exported and importable
- **Severity**: LOW
- **Philosophy Rule**: 16, 17, 20
- **File(s)**: `management/__init__.py`, `management/trade_manager.py`, `scanner/cycle_selection.py:1-10` (the `planning/` package was deleted in PR #892; the remaining management package export is handled by the management-retirement work package).
- **Actual Code Behavior**: Mechanical management, directional planning, and direction-lock selection remain importable package surfaces even when some are no longer default production paths.
- **Execution Path**: External or legacy internal imports can still instantiate and invoke these components without going through the Cognitive Brain.
- **Why It Violates**: The constitution requires one cognitive authority and downstream execution/risk constraints. Exported legacy authorities make it easy to reintroduce forbidden paths.
- **Information Lost**: Callers can bypass opportunity/thesis reasoning entirely.
- **Root Cause**: Backward compatibility exports were preserved during staged migration.

## SEMANTIC

### V-025: Sanitizers still encode forbidden vocabulary as known schema debt
- **Severity**: LOW
- **Philosophy Rule**: 1, 3, 6, 20
- **File(s)**: `brain/world_model.py:32-40`, `brain/world_model.py:366-379`, `cognition/contracts.py:286-314`
- **Actual Code Behavior**: `WorldModel` and `MarketState` sanitizers strip `direction`, `score`, `long_probability`, `short_probability`, `dominant`, `bias`, `lean`, `directional_lean`, `vote`, and `signal` from alignment/evidence measurements. This is protective behavior, but the denylist proves those forbidden keys are still expected to appear at system boundaries.
- **Execution Path**: WorldModel construction calls `_freeze_alignment()` and drops directional alignment keys. `MarketState.add()` calls `scrub_directional()` before evidence reaches the Brain.
- **Why It Violates**: The behavior is a defense-in-depth mitigation rather than direct authority, but the schemas still admit/expect upstream directional contamination. The philosophy says modules should not produce those fields in the first place.
- **Information Lost**: When an upstream source sends a forbidden field, it is silently removed rather than transformed into the underlying observation that produced it.
- **Root Cause**: Sanitizers were added to block legacy directional fields while upstream producers and naming conventions were still being cleaned up.

### V-026: Decision records still expose direction as a central cognitive field
- **Severity**: LOW
- **Philosophy Rule**: 6, 7, 8, 20, 30
- **File(s)**: `cognition/brain.py:57-70`, `cognition/contracts.py:426-438`, `cognition/contracts.py:615-625`, `cognition/brain.py:1020-1030`, `cognition/brain.py:2144-2179`
- **Actual Code Behavior**: `BrainOutput` defaults to `direction=FLAT`; hypotheses carry optional `direction`; demoted portfolio outputs set `output.direction = FLAT`; observe and reasoner-unavailable outputs also return top-level `direction=FLAT`. `DecisionPackage` carries richer decision type and hypotheses, but many call sites/logs still display the top-level direction.
- **Execution Path**: Any Brain decision returns `BrainOutput`. Consumers calling `output.to_dict()`, logs, dashboard surfaces, or gates can see the top-level direction regardless of decision type.
- **Why It Violates**: Direction is still structurally central in the Brain output contract, so non-directional states are represented as `FLAT` by default. This makes it easy to confuse "no opportunity", "waiting", "rejected", "portfolio-demoted", and "provider unavailable".
- **Information Lost**: The reason for non-action is separated into `decision_type`, but the canonical top-level field still collapses all non-LONG/SHORT outcomes into `FLAT`.
- **Root Cause**: Old direction-centric Brain output schema remains for compatibility while richer decision types were added.

### V-027: Live logs and status surfaces still teach operators to read `dir/conf`
- **Severity**: LOW
- **Philosophy Rule**: 6, 8, 12, 20, 34
- **File(s)**: `cognition/loop.py:792-799`, `llm/reasoner.py:917-939`, `llm/reasoning_orchestrator.py:500-526`, `dashboard/state_brain.py:131-148`
- **Actual Code Behavior**: The cognition loop logs decisions as `(dir=... conf=...)`; LLM origination logs `dir`, `conf`, `opp`; council logs `advisor direction(conf)`; dashboard state counts long/short/neutral votes and consensus fields.
- **Execution Path**: Every reasoning cycle with an output executes the cognition log. Every parsed LLM opinion executes the LLM log. Every council consultation with asked advisors can execute panel logging. Dashboard consumers render the legacy vote summary.
- **Why It Violates**: Even when execution behavior is richer, operator-facing observability still frames the system as direction/confidence and majority votes. That makes future regressions toward directional consensus likely.
- **Information Lost**: Operator attention is drawn away from hypotheses, opportunity lifecycle, expected value, execution feasibility, provider capacity, and missing evidence.
- **Root Cause**: Logging/status formats were not upgraded with the cognitive data model.

## Summary by Category

- **PREMATURE_COMPRESSION**: V-001, V-002, V-003, V-004
- **DIRECTIONAL_AUTHORITY**: V-005, V-006, V-007
- **MANAGEMENT**: V-008, V-009, V-010, V-011, V-012
- **PROVIDER_RESILIENCE**: V-013, V-014, V-015, V-016, V-017
- **COUNCIL**: V-018, V-019
- **OPPORTUNITY_HARVESTING**: V-020, V-021, V-022
- **LEGACY**: V-023, V-024
- **SEMANTIC**: V-025, V-026, V-027

## Highest-Risk Execution Chains

1. `CognitionLoop._reason_over_symbol()` -> `CognitiveBrain.reason()` -> native competing hypotheses -> `_originate_independent()` -> activated geometry, verified fixed-horizon return, fresh costs and shared portfolio/risk admission; no global directional-lean shortcut.
2. `LLMReasoner.reason()` -> structured opportunity set -> `CognitiveBrain._from_opinion()` -> activated first-class opportunity, verified return/cost, evidence lineage, advisor quorum and shared risk gates; direction/confidence-only replies remain observational.
3. `CognitionLoop._manage_one()` -> `CognitiveBrain.manage()` -> thesis-based management; missing thesis/action/opportunity fields produce HOLD, and confidence/direction fallback is absent.
4. `PositionWorker.evaluate()` can close positions from scan `direction`/`score` when live cognition management is not enabled.
5. Provider default configuration permits one external provider with no required quota meter, no required local baseline, and no required multi-advisor diversity.


### V-012: Disconnected cross-instrument ranker, sizer and displacer exposed as active controls
- **Severity**: HIGH
- **Status**: RESOLVED in PR #902.
- **Finding**: SystemContext instantiated a cross-instrument ranker, a confidence-weighted opportunity quality sizer and a mechanical position displacer. Repository call-site search found no runtime decision consumers; the dashboard only surfaced their status. Their configuration flags defaulted on, falsely implying active portfolio ranking, confidence-based sizing and displacement.
- **Resolution**: Delete the three helper modules and their isolated tests; remove SystemContext construction, dashboard/frontend status, CrossInstrumentConfig fields, env allowlist entries, .env.example keys and operator-reference rows. Remove the uninitialized global opportunity queue and its config/env/UI controls, and remove the scanner proximity setting because no proximity callback was wired. Keep only the observational proactive watchlist with the three settings actually consumed; it cannot authorize or submit a trade.
- **Regression evidence**: `test_disconnected_cross_instrument_heuristics_are_retired` asserts that modules, config fields and SystemContext wiring remain absent.


### V-013: Execution-quality sizing toggle advertised without a runtime consumer
- **Severity**: MEDIUM
- **Status**: RESOLVED in PR #902.
- **Finding**: `execution_quality_sizing_enabled` existed in RiskConfig and had a test asserting that it defaulted to true, but repository call-site inspection found no execution-quality gate or sizing consumer. The test gave false assurance that a live size throttle existed.
- **Resolution**: Remove the unused config field, its inventory entry and the misleading default-only test. Do not describe execution-quality sizing as live until an actual execution-path consumer and broker-evidence tests exist.
