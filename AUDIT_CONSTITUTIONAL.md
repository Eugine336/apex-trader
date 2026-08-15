# APEX Constitutional Audit Violation Register

Audit date: 2026-08-15

Scope audited:
- All Python files under `brain/`, `cognition/`, `llm/`, `management/`, `execution/`, `tick/`, `scanner/`, `adaptive/`, `decision/`, `trigger/`, `entry/`, `governor/`, `risk/`, and `planning/`.
- `dashboard/state_brain.py` was also inspected because the starting context identified legacy vote display there.

Method:
- Searched every scoped Python file for `LONG`, `SHORT`, `FLAT`, `vote`, `consensus`, `direction`, `lean`, and `bias`.
- Traced the reachable runtime paths through the cognition loop, Brain, LLM provider layer, management loop, execution worker, planning layer, scanner/candle-close path, and adaptive learning layer.
- Distinguished live/default behavior from initialized legacy/shadow code and dormant compatibility APIs.
- This file is documentation only. No violations were fixed in this session.

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
- **Actual Code Behavior**: `_classify_regime()` converts structure displacement, order-flow sign, and liquidity sweep side into a single `directional_lean` value of `LONG`, `SHORT`, or `FLAT`. `EvidenceAnalysis.leaning()` exposes that collapsed value. `generate_hypotheses()` uses the lean to assign directional hypotheses, `_agreement()` boosts the analysis when the lean is directional, and `leading()` prefers the highest-confidence directional hypothesis. When the external reasoner is unavailable or returns unusable output, `_originate_independent()` can open a campaign directly from that native leading directional hypothesis.
- **Execution Path**: `CognitionLoop.run_once()` or `CognitionLoop.reason_symbol_now()` -> `_reason_over_symbol()` -> `EvidenceConsolidator.build()` -> `CognitiveBrain.reason()` -> `_native_reasoning()` -> `BrainReasoningEngine._classify_regime()` / `generate_hypotheses()` -> if the reasoner is unavailable or degraded, `CognitiveBrain._originate_independent()` -> `DecisionType.OPEN_CAMPAIGN` and `CampaignSpecification(direction=direction)`.
- **Why It Violates**: The Brain is supposed to reason from observations into hypotheses, opportunities, expected value, risk, and only then direction. Here a scalar directional lean is created before the Brain completes hypothesis formation, and that lean can become execution authority during provider degradation.
- **Information Lost**: The independent shapes of displacement, order flow, liquidity sweep behavior, momentum state, time horizon, contradictory evidence, and conditional alternatives are reduced to one net direction before opportunity discovery. Mixed evidence can be demoted to `FLAT` by score cancellation before a conditional opportunity is preserved.
- **Root Cause**: Legacy bias/vote machinery was moved inside "native Brain" code as a helper instead of being removed as a pre-cognition directional collapse.

### V-002: Legacy top-level `direction` and `confidence` still drive origination
- **Severity**: CRITICAL
- **Philosophy Rule**: 1, 5, 6, 7, 8, 9, 10, 16, 19, 20
- **File(s)**: `llm/reasoner.py:389-454`, `llm/reasoner.py:573-589`, `llm/reasoner.py:694-709`, `cognition/brain.py:390-417`, `cognition/brain.py:515-532`, `cognition/brain.py:647-704`, `cognition/brain.py:779-855`, `cognition/brain.py:857-907`
- **Actual Code Behavior**: `_normalize_opportunity_reply()` backfills legacy `direction` and `confidence` fields from a preferred opportunity. `LLMOpinion` keeps `direction` and `confidence` as primary fields. `_from_opinion()` reads those fields first, constructs questions like "evidence items lean {direction}", and can open a campaign when `directional_and_qualified`, EV, quorum, domain coverage, and activation pass. For legacy replies without an opportunity set, `activation_ok` is automatically true. When the legacy path has no direction, the Brain records `direction=FLAT` with "no exploitable directional opportunity".
- **Execution Path**: `CognitionLoop._reason_over_symbol()` -> `CognitiveBrain.reason()` -> `LLMReasoner.reason()` -> `_parse()` -> `_build_opinion()` / `_normalize_opportunity_reply()` -> `CognitiveBrain._from_opinion()` -> `DecisionType.OPEN_CAMPAIGN` or legacy `FLAT` observe/reject branch.
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
- **File(s)**: `cognition/loop.py:80-91`, `cognition/loop.py:116-132`, `cognition/loop.py:269-285`, `cognition/loop.py:603-623`, `cognition/evidence_adapters.py:136-167`, `cognition/evidence_adapters.py:201-213`, `cognition/evidence_adapters.py:246-286`, `cognition/evidence_adapters.py:299-344`, `event_driven_bootstrap.py:2445-2464`, `event_driven_bootstrap.py:2881-2920`
- **Actual Code Behavior**: `EvidenceConsolidator` still exposes `set_vote_source()` and calls `evidence_from_votes()` when wired. `evidence_from_votes()` strips directional fields but falls back to `vote.confidence` as observation certainty when no observation-quality field is present. `evidence_from_developing_bias()` still accepts `confidence`, `long_probability`, `short_probability`, and `tradeable` from a "developing bias" dict. The current bootstrap wires `_cognition_developing_bias()`, which now returns non-directional `multi_tf_alignment_dict()`, and no current production call to `set_vote_source()` was found in `event_driven_bootstrap.py`.
- **Execution Path**: Any runtime caller can call `CognitionLoop.set_vote_source()` -> `EvidenceConsolidator.build()` -> `evidence_from_votes()` -> `MarketState.add()` -> `CognitiveBrain.reason()`. Current default bootstrap path is `set_developing_source(_cognition_developing_bias)` -> `evidence_from_developing_bias()` with a non-directional source dict.
- **Why It Violates**: The live API surface still accepts a raw vote panel and can leak directional conviction magnitude as evidence confidence. Even though the sanitizer strips direction keys, the confidence/weight lineage remains vote-derived.
- **Information Lost**: If the source supplies only a vote confidence, the adapter cannot distinguish certainty in an observation from confidence in a directional prediction.
- **Root Cause**: Transitional compatibility path retained while moving from vote panels to observation evidence.

## DIRECTIONAL_AUTHORITY

### V-005: Planning layer recreates weighted directional agreement and emits BUY/SELL plans
- **Severity**: HIGH
- **Philosophy Rule**: 1, 3, 4, 6, 10, 16, 19, 20
- **File(s)**: `planning/models.py:25-32`, `planning/models.py:42-65`, `planning/models.py:76-79`, `planning/models.py:135-137`, `planning/trade_planner.py:223-235`, `planning/trade_planner.py:243-273`, `planning/trade_planner.py:374-438`, `planning/trade_planner.py:481-486`, `planning/trade_planner.py:581-583`, `planning/trade_planner.py:633-650`, `core/system_context.py:2053-2058`
- **Actual Code Behavior**: `TradePlanContext` carries scanner `direction`, signed timeframe alignment, and RL `BUY`/`SELL` actions. `TradePlanner.plan_trade()` reads `ctx.is_long`, computes weighted directional "advisor agreement", blends it into confidence, and creates `TradePlan(direction="BUY" if is_long else "SELL")`. The agreement combines scanner support, decision engine signed read, RL buy/sell support, and adaptive pair win rate. Softened gates can flow a low-conviction setup through as `ENTER` with a multiplier rather than requiring cognitive opportunity activation.
- **Execution Path**: `SystemContext` initializes `ctx.trade_planner`. Any entry/planning caller that invokes `ctx.trade_planner.plan_trade(ctx)` enters the legacy path: `TradePlanContext.direction` -> `ctx.is_long` -> `_advisor_agreement()` -> `_confidence()` -> `TradePlan(direction=BUY/SELL)`.
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
- **Philosophy Rule**: 6, 7, 17, 18, 20
- **File(s)**: `cognition/brain.py:1088-1145`, `cognition/brain.py:1140-1169`, `cognition/brain.py:1177-1230`, `llm/reasoner.py:843-860`, `llm/reasoner.py:987-1003`, `llm/reasoner.py:1065-1080`, `cognition/loop.py:1087-1105`, `cognition/loop.py:1327-1370`
- **Actual Code Behavior**: `CognitiveBrain.manage()` asks the reasoner for a management opinion. If the reply lacks `management_action` and `thesis_state`, it falls back to `_decide_management_legacy()`. That branch reads `opinion.direction`, compares it with the held side, and can `SCALE_IN`, `TIGHTEN_RISK`, `EXIT`, or `REVERSE` based on top-level direction/confidence/opportunity strings.
- **Execution Path**: `CognitionLoop._manage_open_positions()` periodic backstop or `_manage_symbol()` event path -> `_manage_one()` -> `EvidenceConsolidator.build()` -> `CognitiveBrain.manage()` -> `LLMReasoner.reason_management()` -> if management fields are missing, `_decide_management_legacy()` -> management sink.
- **Why It Violates**: Management is supposed to continuously re-evaluate the current opportunity and campaign thesis. The fallback still asks whether the current market opinion's direction aligns or opposes the held side.
- **Information Lost**: Thesis evolution, opportunity replacement, expected value over flat, execution feasibility, and risk-adjusted alternatives can be bypassed when a management reply is parsed as legacy direction/confidence.
- **Root Cause**: Backward-compatible management fallback was retained to handle older/minimal LLM replies.

### V-009: PositionWorker can close positions from compressed scan direction and score
- **Severity**: HIGH
- **Philosophy Rule**: 6, 10, 17, 18, 20, 31
- **File(s)**: `execution/position_worker.py:57-88`, `execution/position_worker.py:212-223`, `execution/position_worker.py:259-340`, `execution/position_worker.py:494-582`, `execution/position_worker.py:780-830`, `execution/position_worker.py:858-874`, `event_driven_bootstrap.py:446-450`, `event_driven_bootstrap.py:5073-5116`, `config.py:659-666`
- **Actual Code Behavior**: `WorkerConfig.discretionary_exits_enabled` defaults true and `scan_directional_exits_enabled` defaults true. When discretionary exits are enabled, `PositionWorker.evaluate()` uses `ScanContext.direction`, `score`, and `opposing_score_boost` to trigger stall, invalidation, and conviction-collapse exits. It also closes on an H1 candle against the held side if PnL is not sufficiently positive. The bootstrap disables discretionary worker exits only when cognition is enabled and `management_mode` is exactly `live`; otherwise these deterministic exits remain available.
- **Execution Path**: `event_driven_bootstrap.PositionEvaluator` builds a `PositionSnapshot` and calls `PositionWorker.evaluate(snap, now, scan=scan_ctx, market=market_ctx)` -> discretionary checks -> `_check_stall()` / `_stall_structure_lost()` / `_check_invalidation()` / `_check_conviction_collapse()` / `_check_htf_candle_close()` -> close intents.
- **Why It Violates**: Outside live cognition management, management can still be reduced to a re-derived scan direction/score and candle color instead of a current opportunity/thesis re-evaluation.
- **Information Lost**: Original thesis, current opportunity set, counter-opportunities, EV of holding versus exiting, and execution/risk context are compressed to "scan still supports direction" or "opposing signal".
- **Root Cause**: Legacy deterministic TradeManager/ExitChecks behavior was preserved in `PositionWorker` as the non-cognition management suite.

### V-010: Re-entry manager re-arms same-direction trades from M5 trend and zone kind
- **Severity**: HIGH
- **Philosophy Rule**: 4, 6, 9, 17, 18, 20
- **File(s)**: `management/re_entry.py:46-109`, `management/re_entry.py:113-138`, `event_driven_bootstrap.py:6534-6574`, `core/system_context.py:2074-2080`
- **Actual Code Behavior**: After a breakeven exit, `ReEntryManager.check_re_entry()` reads the closed trade's `direction`, checks whether M5 trend still matches that direction, then looks for a same-side FVG or order block. If found, it returns `ReEntryOpportunity(direction=direction, eligible=True)`. The bootstrap calls this on breakeven exits and arms a re-entry zone with the same direction.
- **Execution Path**: Trade close handling detects `is_breakeven_exit` -> builds a `closed` namespace with `direction` -> `ctx.re_entry_manager.check_re_entry(closed, m5_df, m1_df)` -> M5 trend/zone filters -> `_arm_re_entry_zone(symbol, direction, opp)`.
- **Why It Violates**: Re-entry should ask whether a fresh opportunity exists after the campaign closed. This path assumes the original direction remains the candidate and uses M5 trend/zone agreement as the command filter.
- **Information Lost**: Whether the breakeven exit invalidated the original thesis, whether the best new opportunity is opposite-side or no-trade, whether EV still exists, and whether the original horizon has changed.
- **Root Cause**: Legacy re-entry automation preserved as setup-validation logic rather than converted into Brain-managed opportunity re-evaluation.

### V-011: Portfolio reallocation fails open to mechanical trim when Brain is unavailable
- **Severity**: MEDIUM
- **Philosophy Rule**: 17, 18, 30, 31
- **File(s)**: `cognition/loop.py:1129-1217`, `cognition/loop.py:1218-1250`
- **Actual Code Behavior**: `_maybe_reallocate()` receives mechanical trim candidates from the campaign registry. `_reason_reallocation()` is intended to ask the Brain for veto/approval, but if the Brain is missing or unavailable it returns `(True, "unavailable", 0.0)`, causing the deterministic concentration trim to proceed.
- **Execution Path**: `CognitionLoop._manage_open_positions()` -> `_maybe_reallocate(positions)` -> registry `reallocation_targets()` -> `_reason_reallocation()` -> if no Brain or unavailable, approve -> `_management_sink(action, pos)` emits trim.
- **Why It Violates**: The fallback turns lack of cognition into permission for a mechanical capital action. Risk protections must remain alive when cognition is down, but opportunity/capital reallocation should not pretend a provider outage is a reasoned portfolio decision.
- **Information Lost**: The Brain's comparison of current campaign EV against alternative opportunities, timing, thesis state, and whether the "weakest" campaign is actually about to improve.
- **Root Cause**: Fail-open safety fallback for de-risking was reused for portfolio opportunity reallocation.

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
- **File(s)**: `cognition/brain.py:452-471`, `cognition/brain.py:598-604`, `cognition/brain.py:1511-1543`, `config.py:3337-3349`, `config.py:3754-3757`, `llm/reasoning_orchestrator.py:667-702`, `cognition/loop.py:336-345`
- **Actual Code Behavior**: `_extract_advisor_counts()` returns `(None, None, None)` when no reasoning evidence carries council metadata, and the quorum gate treats `advisors_responded is None` as pass. Degraded cognition is logged as "observed but not penalised". `min_advisors_for_action` defaults to 1. `consult_multi` defaults false, and the orchestrator returns a council even with one engine.
- **Execution Path**: `EvidenceConsolidator.build()` optionally records reasoning consultation evidence -> `CognitiveBrain._from_opinion()` extracts advisor counts -> if metadata is missing, `quorum_ok` passes; if fewer than half of available advisors respond, only a warning/metadata record is produced; action gate can still open if other scalar gates pass.
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

### V-017: Cognition and management gates are configurable fail-open/off surfaces
- **Severity**: MEDIUM
- **Philosophy Rule**: 16, 30, 31
- **File(s)**: `config.py:3717-3739`, `config.py:3873-3878`, `cognition/gate.py:18-20`, `cognition/gate.py:86-106`, `cognition/gate.py:120-151`, `core/system_context.py:1582-1601`, `event_driven_bootstrap.py:1076-1090`
- **Actual Code Behavior**: `CognitionConfig.gate_mode` defaults to `off`. In `shadow` or `veto` modes, missing Brain, cold start, and gate faults are fail-open. The management gate helper returns true if no gate is wired; only `authoritative` mode fail-closes on absent cognition.
- **Execution Path**: Legacy entry or exposure-adding management path calls `CognitionGate.evaluate()` or `_management_gate_allows()` -> if gate is off/missing/soft and Brain has no read, allow can be true. In the current single-path Brain-origination mode this may be less central, but the fail-open gate remains a configurable execution surface.
- **Why It Violates**: If legacy execution surfaces are present, absent cognitive availability should be a distinct operational state, not permission to proceed without cognition.
- **Information Lost**: The reason an entry/scale was allowed can be "no Brain/gate off" rather than a reasoned opportunity decision.
- **Root Cause**: Shadow migration controls were kept available after the constitution moved to a single cognitive authority.

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

### V-022: Adaptive timeframe weights learn from winning LONG/SHORT agreement
- **Severity**: MEDIUM
- **Philosophy Rule**: 4, 6, 10, 20
- **File(s)**: `adaptive/adaptive_weight_provider.py:1-10`, `adaptive/adaptive_weight_provider.py:77-83`, `adaptive/adaptive_weight_provider.py:160-199`, `brain/decision_core.py:77-92`, `brain/decision_core.py:108-140`, `core/system_context.py:1720-1745`
- **Actual Code Behavior**: `AdaptiveWeightProvider` tracks how predictive each timeframe's confirmed structure direction was versus realized trade outcome. It maps `BULLISH` to `LONG` and `BEARISH` to `SHORT`, credits/debits only timeframes whose trend backed the taken direction, and can override `decision_core` evidence weights after enough trades. `decision_core` comments still describe a probabilistic bias model with timeframe weights contributing directional structure.
- **Execution Path**: `SystemContext` initializes `AdaptiveWeightProvider` and registers it through `brain.decision_core.set_evidence_weight_provider()`. Any decision-core bias path using `_active_weights()` receives learned weights that were trained on whether timeframe trends backed winning `LONG`/`SHORT` trades.
- **Why It Violates**: Higher timeframe and timeframe-specific structure should be context, not learned command authority. This system increases or decreases timeframe influence based on directional agreement with winning trades.
- **Information Lost**: A timeframe's contextual usefulness for different horizons, reversals, liquidity events, and conditional opportunities is reduced to whether its trend aligned with a taken side.
- **Root Cause**: Static directional bias model was made adaptive without redesigning learning targets around opportunity/thesis quality.

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
- **File(s)**: `management/__init__.py:8`, `management/trade_manager.py:135-145`, `planning/__init__.py:17-22`, `planning/trade_planner.py:205-223`, `scanner/cycle_selection.py:1-10`
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

1. `CognitionLoop._reason_over_symbol()` -> `CognitiveBrain.reason()` -> native `directional_lean` -> `_originate_independent()` can originate from pre-reasoning directional collapse when providers are unavailable.
2. `LLMReasoner.reason()` -> legacy `LLMOpinion.direction/confidence` -> `CognitiveBrain._from_opinion()` can still open a campaign without a first-class activated opportunity object.
3. `CognitionLoop._manage_one()` -> `CognitiveBrain.manage()` -> `_decide_management_legacy()` can scale, exit, or reverse from a direction/confidence management reply when thesis fields are absent.
4. `PositionWorker.evaluate()` can close positions from scan `direction`/`score` when live cognition management is not enabled.
5. Provider default configuration permits one external provider with no required quota meter, no required local baseline, and no required multi-advisor diversity.
