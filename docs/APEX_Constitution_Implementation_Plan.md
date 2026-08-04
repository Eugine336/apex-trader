# APEX Constitution — Implementation Plan (1:1 Closure)

> **Status:** BINDING execution plan. This document maps the APEX Constitution
> (`docs/APEX_Constitution.md`) against the **current code** and enumerates every
> gap that is not yet 1:1. It is followed **strictly and in order** until the
> Definition of Done (Part XIV) is fully met. Each phase is one PR.
>
> Legend: ✅ = 1:1 in code · 🟡 = partial · ❌ = not started.

---

## 0. How this plan is executed (non-negotiable rules)

1. **One phase = one PR.** No phase starts before the previous phase's
   acceptance criteria pass.
2. **Sync first.** Every working session begins by fetching + merging
   `origin/main` (no fast-forward) so we build on latest master.
3. **Shadow before authoritative.** Every new Brain authority ships first in
   `shadow` (records, changes nothing), is observed, then is promoted to
   `veto`/`authoritative` via config — never flipped blind (Parts XIII, XV).
4. **Fail-safe always.** New code never raises into the trading loop. Softer
   modes fail-open; `authoritative` fails-closed (no reasoning ⇒ no trade).
5. **No legacy deletion until superseded + validated.** Legacy decision code is
   first demoted to evidence/mechanics; it is physically removed only in
   Phase K, after E–G are live-validated on the demo.
6. **Offline-testable.** Every new module is pure/duck-typed where possible so
   it is unit-testable without network; the single provider call stays isolated
   behind an injectable transport.
7. **Deterministic safety is retained** (Part X): execution feasibility, risk
   ceilings, circuit breaker, capital preservation. The Brain owns *market
   judgment*; it never overrides *physics/capital* constraints.

---

## 1. Compliance matrix (Constitution → code → status)

| Constitution | Requirement | Status | Where in code | Closed by |
|---|---|---|---|---|
| I.4 / II.1 | Single cognitive authority | 🟡 entry-only | `cognition/gate.py` (authoritative on entry); management still legacy | E, F, G, K |
| II.7 / II.3 | Structured decision package + required questions | ✅ | `cognition/contracts.py` `DecisionPackage`; `cognition/brain.py` | — |
| III.2 | No module emits buy/sell/hold/close/reverse | 🟡 | legacy `directional_consensus`, `thesis_engine` still emit directional votes/should_act | E, K |
| III.3 | All evidence domains feed the Brain | ✅ | `cognition/evidence_adapters.py` + `cognition/loop.py` consolidator (per-module, domain-classified) | E ✅ |
| III.4 | Evidence format (id/ts/source/conf/uncertainty/horizon) | ✅ | `cognition/contracts.py` `Evidence` | — |
| IV.1–8 | Pre-trade cognitive cycle → campaign spec | 🟡 | Brain produces `DecisionPackage`/`CampaignSpecification`; spec now originates entries (G, shadow-default) | G✅(shadow), K |
| V.1–8 | Opportunity harvesting / campaign lifecycle | 🟡 | `brain/campaign.py` registry + Brain-driven management (F) + Brain origination (G, shadow) | G✅, H |
| VI.1 | Execution never reinterprets market intent | 🟡 | Brain-originated entries reach executor unchanged (G); legacy Gates 1–7 still judge markets on the legacy path | G🟡, K |
| VI.2 | Execution consumes Brain `CampaignSpecification` | 🟡 | `cognition/campaign_translator.py` + loop origination + `event_driven_bootstrap` live sink (shadow-default; live inert until stops emitted) | **G✅ (shadow)** |
| VI.3 | Execution feasibility validation | ✅ | `execution/risk_gate.py`, `execution/action_executor.py`, compliance | — |
| VI.4 | Renewed Brain authorization per execution/management action | 🟡 | entry gated + freshness (C/D); risk-adding management (scale-in/re-entry) gated (F); exit/reverse *execution* still G | F, G |
| VI.5 | Brain-driven management (hold/scale/protect/exit/reverse) | ✅ | `cognition/brain.py` `manage()` + `cognition/management_gate.py` (adds gated live; de-risking never blocked); legacy managers demoted to evidence | F ✅ (exit/reverse execution in G) |
| VII.1–6 | Post-trade reconstruction, decision audit, memory | 🟡 | `brain/campaign.py` post-mortem (in-memory, bounded); `adaptive/counterfactual.py` | H |
| VII (memory) | Persistent institutional memory + retrieval | ❌ | campaigns not persisted to `persistence/event_store.py`; no similarity retrieval | **H** |
| VIII | Adaptive influence over evidence + Brain, validated | 🟡 | `adaptive/*` grades legacy vote emitters, not new `Evidence`/Brain | J |
| IX | Composio: AI objective → governance → execute → observe → memory | 🟡 | `action/*` gateway + `BrainActionBridge` (notify only); no capability registry / self-improvement objectives | I |
| X | Governance & safety (policy, approvals, ceilings, audit) | ✅ | `action/orchestrator.py` `GovernancePolicy`, `governance/division.py`, risk stack | — |
| XI | Modular, testable, no hidden decision logic | 🟡 | `event_driven_bootstrap.py` 10.8k-line god-file; legacy decision code present | K |
| XII | Observability (metrics/logs/traces/lifecycle) | 🟡 | governance `get_status` surfaces cognition; no cognition metrics/traces/dashboards | L |
| XIII | Validation (unit/integration/sim/walk-forward/paper/rollout) | 🟡 | unit tests present; no cognition sim/walk-forward/paper harness | L |
| XIV | Acceptance criteria | ❌ | see Definition of Done | E–L |

**Completed foundation (Phases A–D, merged):** constitutional contracts
(`cognition/contracts.py`), the single `CognitiveBrain` (`cognition/brain.py`),
evidence-consolidation + background loop + Brain→Composio bridge
(`cognition/loop.py`), the entry gate shadow→veto→authoritative
(`cognition/gate.py`), default live.

---

## 2. Phase E — Full evidence consolidation (Part III.3) — ✅ DONE

**Goal:** the Brain reasons over ALL evidence domains, not just the ThesisEngine.

**Current state:** `EvidenceConsolidator._add_thesis_evidence` only converts
`brain/thesis_engine.py`. All other analytical modules still feed the legacy
consensus.

**Tasks**
- Add `cognition/evidence_adapters.py`: one read-only, fail-safe adapter per
  domain that maps an existing module's current read into `Evidence`
  (no signals — observation + polarity + confidence + uncertainty + horizon):
  - structure → `brain/structure_engine.py`, `brain/wyckoff_engine.py`
  - liquidity → `brain/liquidity_mapper.py`, `brain/order_block.py`, `brain/fvg_detector.py`, `brain/inducement_detector.py`
  - momentum → `brain/momentum_divergence.py`
  - volatility → `brain/volatility_stop.py`, `brain/atr_percentile.py`, `brain/compression_detector.py`
  - volume → `brain/volume_analyzer.py`, `brain/session_vwap.py`
  - multi-timeframe → `brain/world_model.py`, `brain/session_context.py`
  - correlation → `brain/correlation_engine.py`, `brain/currency_strength.py`, `brain/cross_instrument_ranker.py`
  - session/macro → `brain/session_engine.py`, `brain/news_impact_tracker.py`
  - regime → `brain/regime_detector.py`
  - portfolio → account/portfolio state via `ctx`
  - historical analogue → `brain/instrument_stats.py` (until Phase H memory retrieval)
- Extend `EvidenceConsolidator.build` to invoke every enabled adapter; each
  adapter is independently guarded (one failing adapter never breaks the state).
- Config: `CognitionConfig.evidence_domains` (enable/disable per domain).

**Acceptance:** `MarketState.consolidation()` reports ≥8 domains present for a
live symbol; per-adapter unit tests; no adapter emits a directional instruction;
consolidation degrades gracefully when a source is absent.

**Rollout:** additive; the Brain immediately reasons over richer evidence. Gate
stays as configured.

---

## 3. Phase F — Brain-driven campaign management (Parts V, VI.5, VI.4) — ✅ DONE

**Goal:** the Brain, not the legacy management engine, decides HOLD / SCALE_IN /
SCALE_OUT / PROTECT / TIGHTEN / EXIT / REVERSE / TERMINATE for open campaigns.

**Current state:** `management/trade_manager.py`, `reversal_manager.py`,
`re_entry.py`, `partial_close.py`, `position_displacer.py` decide independently;
`brain/thesis_engine.py` drives evidence-exits. Nothing consults the Brain.

**Tasks**
- `cognition/brain.py`: add `manage(campaign_state, market_state)` →
  management `DecisionPackage` (uses the `DecisionType` management actions).
- `cognition/management_gate.py`: mirror the entry gate — a management action
  requires a fresh, aligned Brain authorization (shadow → authoritative);
  fail-safe. Renewed each cycle (Part VI.4).
- Wire the management scheduler / `PositionEvaluator` path to consult the gate
  before executing a management action; legacy managers become *evidence*
  (their reads flow into the Brain via Phase E adapters) + *mechanics*.
- Route `brain/campaign.py` registry as the campaign-state source for `manage`.

**Acceptance:** in authoritative mode no management action executes without Brain
authorization; shadow mode records would-manage; legacy managers no longer close
/scale on their own judgment; tests for each action + fail-safe.

---

## 4. Phase G — Campaign origination + execution consumes the spec (Parts IV.8, VI.1–2)

## 4. Phase G — Campaign origination + execution consumes the spec (Parts IV.8, VI.1–2) — 🟡 SHIPPED SHADOW

**Goal:** the Brain *originates* entries from its `CampaignSpecification`, and
execution consumes the spec without reinterpreting it.

**Shipped (shadow-by-default):**
- `cognition/campaign_translator.py`: pure, fail-safe `translate(spec, *, balance,
  risk_fraction, max_exposure) -> OriginationIntent` — sizes `stake_usd = balance
  × risk_fraction × exposure`, carries direction + optional stop/target + full
  provenance (campaign_id/decision_id/confidence). Carries no market judgment.
- `cognition/loop.py`: on a fresh `OPEN_CAMPAIGN` decision carrying a directional
  campaign the book does not already hold, `_maybe_originate()` translates the
  spec and either records the intended order (`shadow`, default) or hands the
  `OriginationIntent` to the wired executor sink (`live`). Deduplicates within a
  cycle and against currently-open `(symbol, direction)` keys. `get_status()`
  surfaces `origination_mode` / `orig_intended` / `orig_submitted`.
- `config.py` `CognitionConfig`: `origination_mode` (off|shadow|**live**; default
  **shadow**) + `origination_risk_fraction` / `origination_max_exposure`, with env
  overrides (`COGNITION_ORIGINATION_MODE`, …) and validation.
- `event_driven_bootstrap.py`: `_make_origination_sink()` submits a Brain-
  originated `Intent.open` onto the SAME execution plane (aggregator → RiskGate →
  broker) as every other entry; wired only when `origination_mode == "live"`.
  Fail-safe: refuses to submit without a protective stop/target (never bypasses
  the Part X deterministic-safety floor).

**Why shadow-first:** the live order/sizing/stop path cannot be verified offline
(no broker, no deps). `shadow` records intended orders so origination is fully
observable before a single real submission; `live` stays inert until the Brain
emits stops AND an operator flips the flag. This honours Part XIII (shadow →
controlled rollout) exactly as the entry gate (C/D) and management gate (F) did.

**Remaining for full VI.1 (tracked into K):**
- Reduce the legacy candidate generator (`brain/opportunity_ranker.py`,
  `directional_consensus.py`) to an *opportunity/evidence feed* (surfaces
  candidate symbols; the Brain decides). Gates 1–7 become feasibility-only or
  evidence — no market judgment. Deferred to Phase K (legacy removal) so the
  authoritative path stays validated first.

**Acceptance:** ✅ an entry can originate purely from a Brain campaign spec and
reach the feasibility-validated executor unchanged (live sink); ⏳ retiring the
legacy market-judging candidate path is Phase K.

---

## 5. Phase H — Persistent institutional memory (Part VII)

**Goal:** every campaign is reconstructed, audited, and persisted for retrieval
and future reasoning.

**Current state:** `brain/campaign.py` keeps a bounded in-memory history +
post-mortem; `adaptive/counterfactual.py` exists; nothing persisted to
`persistence/event_store.py`; no similarity retrieval.

**Tasks**
- `cognition/memory.py`: persist each terminated campaign (spec, evidence
  snapshot, decision packages, executions, outcome, post-mortem, counterfactual)
  via `persistence/event_store.py` / a `campaign_memory` store.
- Similarity retrieval (`find_analogues(market_state)`) feeding the Phase E
  historical-analogue adapter, so the Brain consults memory when reasoning.
- Wire campaign close (`_on_trade_closed` / registry finalize) to write memory.

**Acceptance:** campaigns persisted across restarts and retrievable; analogue
evidence appears in `MarketState`; post-mortem verdicts feed Phase J.

---

## 6. Phase I — Composio operational intelligence (Part IX)

**Goal:** the Brain proposes operational objectives; governance authorises;
Composio executes; observation verifies; memory records — end to end.

**Current state:** `action/*` gateway + policy tiers done; `BrainActionBridge`
emits only `operator.notify`; no capability registry; no self-improvement
objectives.

**Tasks**
- `action/capabilities.py`: typed capability registry (operator.notify,
  github.create_issue, docs.update, report.publish, research.record) with
  per-capability risk tier + required params.
- `cognition/operations.py`: Brain-authored operational objectives from
  institutional memory (recurring failure → issue; significant discovery →
  notify; weekly report). Ecosystem-only — never broker orders.
- Observation/verification: confirm the external effect and record to memory.
- `.env`: real Composio key + `COMPOSIO_DRY_RUN=false` (already staged).

**Acceptance:** end-to-end objective→governance→Composio→observation→memory for
≥2 capabilities; high/destructive still require approval.

---

## 7. Phase J — Adaptive intelligence over evidence + Brain (Part VIII)

**Goal:** evidence-source influence and the Brain's own influence are graded by
demonstrated decision quality, via governance-validated updates.

**Tasks**
- Feed campaign post-mortems (Phase H) into the adaptive layer keyed by
  `Evidence.source_module`, so each evidence source earns/loses influence.
- Weight `Evidence` in `MarketState.consolidation()` by learned influence.
- Measure Brain reasoning quality (calibration: predicted confidence vs realised)
  and surface it; adaptation via the existing governance gateway (no bypass).

**Acceptance:** evidence weights move only on statistically-significant samples
through governance; Brain calibration tracked; robustness prioritised over
recent profit.

---

## 8. Phase K — Legacy decider removal + god-file decomposition (Part XI)

**Goal:** physically remove the now-dead legacy *decision* code; decompose the
orchestrator.

**Preconditions:** Phases E–G live-validated on the demo (Brain covers entry +
management + origination over full evidence).

**Tasks**
- Delete/retire the legacy *decision* authority: consensus argmax direction,
  `thesis_engine` entry/exit authority, `orchestrator` sizing verdict, Gates
  1–7 market judgment. Keep only feasibility/mechanics + evidence adapters.
- Decompose `event_driven_bootstrap.py` (10.8k lines): extract `PositionEvaluator`,
  the entry pipeline, and lifecycle loops into named, unit-tested modules with
  characterization tests written first.

**Acceptance:** no competing decision code remains (grep audit); god-file split;
characterization tests green; behaviour unchanged for the Brain-driven path.

---

## 9. Phase L — Observability, validation & controlled rollout (Parts XII, XIII)

**Tasks**
- Cognition metrics/traces: decisions/sec, campaigns opened, veto/authorise
  counts, evidence-domain coverage, Brain latency, calibration — dashboards.
- Validation harness: replay/simulation over historical events, walk-forward,
  and a paper-trading mode driving the Brain end-to-end.
- Controlled rollout runbook: shadow → veto → authoritative per symbol group,
  with rollback (`COGNITION_GATE_MODE`).

**Acceptance:** reasoning quality is measurable; validation harness green before
`authoritative` is trusted on the demo; documented rollback.

---

## 10. Definition of Done (Constitution Part XIV)

The redesign is complete only when ALL hold, verified by tests + a grep/audit:
- [ ] Single Reasoner Principle holds for **entry, management, and origination**
      (no competing decider in code).
- [ ] Evidence modules never emit buy/sell/hold/close/reverse; all feed the Brain.
- [ ] The AI performs the reasoning; execution never reinterprets market intent.
- [ ] Campaigns are opportunity-based, Brain-driven, and persisted.
- [ ] Learning is auditable; evidence + Brain influence adapt through governance.
- [ ] Explainability: every trade/no-trade traces to recorded evidence + decision.
- [ ] Observability + validation harness in place; controlled-rollout documented.

Any violation ⇒ redesign (Part XIV).

---

## 11. Ordering summary

E (evidence) → F (management authority) → G (origination + spec execution) →
H (institutional memory) → I (Composio ops) → J (adaptive) → K (legacy removal +
god-file decomposition) → L (observability + validation).

E is the highest-leverage next step and unblocks the quality of everything after.
