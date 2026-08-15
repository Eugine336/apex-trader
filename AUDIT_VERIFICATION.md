# APEX TRADER — CONSTITUTIONAL VERIFICATION AUDIT

**Post-fix 1:1 philosophy-mirror check.** Adversarial re-audit of the live
execution path after the eight category fix-PRs (V-001…V-027) merged.

- **Base commit:** `e11be71` (all `path:line` references below are at this commit).
- **Method:** Static reconstruction of executed logic. Comments, docstrings, and
  naming were explicitly distrusted and cross-checked against the code that runs
  and the *shipped config/.env defaults*. Every claim cites `file:line`.
- **Live-path guard:** `python scripts/constitution_audit.py` → **CONSTITUTIONAL
  (clean live path): 0 violations across 0/8 invariants.** This audit treats that
  verdict as a *hypothesis to disprove*, not proof — see §7 (the guard is a narrow
  static allow-list with structural blind spots).

> ⚠️ **Register-source caveat.** `AUDIT_CONSTITUTIONAL.md` (the original 27-violation
> register) is **not present in this repository** — it was authored in a separate
> session's sandbox and never committed. This verification reconstructs each
> violation from: (a) the merged fix-PR commit messages and in-code fix markers
> (`V-01`, `V-03`, `V-05/06`, `V5`, `Constitution §…`), (b) the merged constitution
> text (Part XL non-negotiables) and [`docs/APEX_PHILOSOPHY_AUDIT.md`](https://github.com/eugine336/apex-trader/blob/e11be718138857609422e8c3f63fe9078e0c21de/docs/APEX_PHILOSOPHY_AUDIT.md),
> and (c) the category→violation mapping. Where only a MEDIUM/LOW category theme was
> recoverable, the concrete code artifact was identified and is named in the Notes.
> Findings are anchored to **live code**, which is the source of truth.

---

## 0. EXECUTIVE VERDICT

**The codebase does NOT yet mirror the constitutional philosophy 1:1.**

The *structural / obvious* violations are genuinely gone: the directional
vote/consensus engine, the retired decider cluster
(`DecisionEngine`/`SituationEngine`/`RiskGovernor`/`DecisionJournal`),
`EntryEngine.calculate_entry`, the `expected_value = confidence` proxy, and the
`provider-failure ⇒ FLAT` collapse are all removed, and the single-Brain authority
plus the non-voting advisory council are solidly in place. That is real progress.

But the fixes are **shallower than the guard implies**:

- **10 / 27 FIXED · 10 / 27 PARTIAL · 7 / 27 STILL PRESENT** (17 of 27 not fully closed).
- The strongest remediations are **opt-in behind permissive shipped defaults**
  (`ev_primary_gate=False`, `min_advisors_for_action=1`, budgets unmetered,
  `scan_directional_exits_enabled=True`) — the fix code exists but is **inert on the
  default live path**.
- Several forbidden mechanisms survive **dormant-but-one-wire-from-live**
  (`planning/trade_planner.py` directional decider, `adaptive/signal_ledger.py` +
  `module_governor.py` directional-correctness learning,
  `adaptive/adaptive_weight_provider.py`).
- A **new, live, pre-reasoning directional-vote collapse** sits in the guard's blind
  spot: `cognition/brain_reasoning.py::_directional_lean` (V-NEW-001).
- Core cognitive gates **fail OPEN** (UNKNOWN opportunity ⇒ actionable; absent
  council metadata ⇒ quorum passes).
- Operator surfaces still present a **votes / consensus / ranker** model.

| Status | Count | Violations |
|---|---|---|
| ✅ FIXED | 10 | V-004, V-006, V-007, V-016, V-017, V-018, V-019, V-020, V-022, V-023 |
| ⚠️ PARTIALLY_FIXED | 10 | V-001, V-002, V-005, V-008, V-009, V-012, V-013, V-014, V-021, V-024 |
| ❌ STILL_PRESENT | 7 | V-003, V-010, V-011, V-015, V-025, V-026, V-027 |

---

## 1. VIOLATION-BY-VIOLATION VERIFICATION TABLE

| V-ID | Category | Orig. Sev. | Status | Notes (evidence @ `e11be71`) |
|---|---|---|---|---|
| **V-001** | PREMATURE_COMPRESSION | CRITICAL | ⚠️ PARTIAL | `cognition/brain_reasoning.py:301-326` `_directional_lean` sums indicator signs → collapses to `LONG/SHORT/FLAT` **inside `analyze_evidence` (`:291`) before hypotheses**, then seeds hypothesis direction (`:390,407,426`). On LLM outage the lean-seeded lead hypothesis originates a live campaign via `cognition/brain.py::_originate_independent` (`:336-341`, `:1971-2088`). The remediation — EV-primary gate — is **default OFF** (`config.py:3752`) and does not govern independent origination. Collapse-before-reasoning persists on the live path. |
| **V-002** | PREMATURE_COMPRESSION | CRITICAL | ⚠️ PARTIAL | A bare legacy `direction+confidence` reply is `is_harvesting=False` (`cognition/brain.py:1568-1570`) → the lifecycle/activation gate is **bypassed** (`:667-671`). "LONG 0.55" clears `directional_and_qualified` (`:693-697`); `ev_ok` uses live `min_expected_value=0.0` with `reward` defaulted 2.0 (`cognition/expected_value.py:52`) so EV is a monotone transform of confidence (no independent structured-opportunity signal) → `act=True` → `OPEN_CAMPAIGN` (`:698-704`). No structured `Opportunity`/EV object is required to open. |
| **V-003** | PREMATURE_COMPRESSION | HIGH | ❌ PRESENT | `cognition/contracts.py:513-526` `Opportunity.is_actionable` returns **True when `state == UNKNOWN`** (docstring: "treated as actionable so pre-harvesting behaviour is unchanged"). `opportunity_state_from` defaults unknown/unrecognised → `UNKNOWN` (`:184-212`); `from_reply` yields `UNKNOWN` when state omitted (`:566`). Missing lifecycle state = actionable ⇒ **fail-open**, directly against Principle 5. |
| **V-004** | PREMATURE_COMPRESSION | MEDIUM | ✅ FIXED | `expected_value = confidence` proxy removed: EV is genuine `p·reward −(1−p)·risk −cost` (`cognition/expected_value.py:56-64`), and confidence is decomposed into a 4-D `min()` profile (`cognition/brain.py:417`). Grep for the proxy in `cognition/` → none. (Residual: for excursion-less opinions EV degenerates to a monotone fn of confidence — see V-002.) |
| **V-005** | DIRECTIONAL_AUTHORITY | HIGH | ⚠️ PARTIAL | `planning/trade_planner.py` **still contains** the directional decider: `_advisor_agreement` computes weighted directional agreement (`:374-413`) and `plan_trade` emits `TradePlan(direction="BUY"/"SELL")` + `ENTER/WAIT/SKIP` (`:234,267`). **Dormant:** `plan_trade(` has **zero live callers** (tests only); the object is instantiated and *calibrated* live (`adaptive/tunable_adapters.py:644-646`) but never invoked. The guard **excludes** this file from its call-scan (`scripts/constitution_audit.py:193`) — one line re-wires a live BUY/SELL decider undetected. |
| **V-006** | DIRECTIONAL_AUTHORITY | HIGH | ✅ FIXED | Retired decider cluster physically gone: `decision/` holds only `__init__.py` + `actions.py` (inert dataclasses). `engine.py/situation.py/governor.py/journal.py/context.py` deleted; no live imports (only a string label `owner="decision.engine"` at `event_driven_bootstrap.py:830`). |
| **V-007** | DIRECTIONAL_AUTHORITY | HIGH | ✅ FIXED | `trigger/entry_engine.py` no longer defines `calculate_entry` (removal noted `:162-170`); only non-deciding geometry/sizing helpers remain (`find_entry_zone`, `calculate_stop_loss`, `calculate_targets`, `calculate_position_size` — all take `direction` as input). No live `.calculate_entry(` caller. `brain/structure_engine.py` has no `get_bias`. |
| **V-008** | MANAGEMENT | HIGH | ⚠️ PARTIAL | `cognition/brain.py::manage` routes to thesis tree when `management_action`/`thesis_state` present, **else `_decide_management_legacy`** (`:1142-1150`), which still returns **REVERSE/EXIT from `opinion.direction` vs held side + confidence** (`:1191-1229`, esp. `:1222-1225`). Genuine gains: fault/no-opinion/unavailable/FLAT ⇒ HOLD (`:1108-1129,1170-1175`); thesis tree has no direction compare. But when a management reply lacks thesis fields yet carries a bare LONG/SHORT, the forbidden direction+confidence liquidation is still live (not strictly fail-closed). |
| **V-009** | MANAGEMENT | HIGH | ⚠️ PARTIAL | Scan-directional/score exits are live: `execution/position_worker.py::_check_invalidation` (`:780-807`), `_check_conviction_collapse` (`:809-830`), `_stall_structure_lost` (`:567-582`), fed a **re-derived** scan direction (`event_driven_bootstrap.py:578-583`). V-03 flag `scan_directional_exits_enabled` is **default True** (`config.py:666`, `position_worker.py:88`). Mitigation: in cognition mode (shipped default) `discretionary_exits_enabled` is forced False at runtime (`event_driven_bootstrap.py:5091-5095`), deferring the suite to the Brain — but in any deterministic/non-cognition run these exits are **ON by default**. |
| **V-010** | MANAGEMENT | HIGH | ❌ PRESENT | `management/re_entry.py:57` `direction = closed_trade.direction` (same side); `:84-92` `bias_valid` requires M5 `StructureEngine` trend == `BULLISH`(LONG)/`BEARISH`(SHORT) — a **directional same-direction M5-trend re-arm**, not thesis/opportunity-based. Wired live (`event_driven_bootstrap.py:6537-6573`; `core/system_context.py:2076-2077`). Scope-limited to breakeven-stopped trades, but the directional logic executes. |
| **V-011** | MANAGEMENT | MEDIUM | ❌ PRESENT (by design) | Cycle direction lock: `scanner/cycle_selection.py:45-54` promotes the top candidate's `direction` to `winning_direction` and drops all opposing-direction candidates — a per-cycle premature directional collapse on the entry side. Documented as interim pending `PortfolioGovernor.allocate`; defensible as an anti-self-hedge guard but still a directional lock. |
| **V-012** | MANAGEMENT | MEDIUM | ⚠️ PARTIAL | The strategic comparator (`DecisionEngine.decide_management`/`SituationEngine.assess_open_trade`) is **retired** (cluster deleted). Residual instances are the opposing-direction closes in `execution/position_worker.py:793-807` and `:576-582` (same gating/verdict as V-009). |
| **V-013** | PROVIDER_RESILIENCE | HIGH | ⚠️ PARTIAL | Real quorum gate exists (`cognition/brain.py:601-604,698-704`; management degrade `:1317-1342`) but **neutered**: production default `min_advisors_for_action=1` (`config.py:3757`, shipped `.env`), quorum **passes when council metadata is absent** (`:601-603`), and "degraded council" is observation-only (`degraded_confidence_multiplier` retained but deliberately unused, `:147-152,454-456`). Enforcement is a per-symbol `CONTINUE_OBSERVING`, **not** a global degraded-mode halt/escalation. |
| **V-014** | PROVIDER_RESILIENCE | HIGH | ⚠️ PARTIAL | Budget machinery is solid and shared-per-account (`llm/provider_budget.py`; client refuses+benches over budget `llm/client.py:459-472`; shared across roster). **But metering is opt-in:** `attach_budget` is a no-op unless a positive limit is set (`llm/client.py:731-733`), no rpm/rpd/tpm/tpd defaults in config, shipped `.env` sets none ⇒ **unmetered in the deployed config.** |
| **V-015** | PROVIDER_RESILIENCE | HIGH | ❌ PRESENT | Diversity/local foundation is only *surfaced*, never enforced: tier taxonomy + failover exist (`llm/provider_tiers.py`, `provider_registry.py:173-186`, `model_manager.py:184-188`) but **no code requires ≥2 tiers or a local model**; whole subsystem opt-in (`LLMConfig.enabled` default False, `consult_multi` default False). The only "requirement" is a `.env` test (`tests/test_env_local_classification.py`) which is **currently FAILING** — shipped `LLM_EXTRA_MODELS` are all Tier-2 (groq/nvidia/modal), no Tier-3 local; the sole `deep` reasoner is hosted `modal`. No offline/keyless failsafe in the deployed roster. |
| **V-016** | PROVIDER_RESILIENCE | HIGH | ✅ FIXED | `DecisionType.REASONER_UNAVAILABLE` is a distinct, non-actionable state (`cognition/contracts.py:136`, excluded from `authorises_action` `:656-663`). Down/degraded ⇒ `REASONER_UNAVAILABLE`; healthy None/throttle ⇒ `CONTINUE_OBSERVING` (`cognition/brain.py:328-373`). Provider states AVAILABLE/CONFIGURED/UNAVAILABLE (`llm/provider_registry.py:45-100`). `tests/test_reasoner_unavailable_distinction.py` (12) PASS. Default-on. |
| **V-017** | PROVIDER_RESILIENCE | MEDIUM | ✅ FIXED | Vendor `Retry-After`/quota-reset honored end-to-end: header parse (`llm/client.py:165-228`), circuit-breaker `bench` opens without a hard fault, extend-only, cleared by success (`llm/health.py:98-121`, §27); council benches throttled advisor without fault (`llm/reasoning_orchestrator.py:189-203`). `tests/test_provider_retry_after.py` (13) PASS. |
| **V-018** | COUNCIL | MEDIUM | ✅ FIXED | Advisory council **never votes/averages/picks a majority**: `llm/reasoning_orchestrator.py::consult` fans out and preserves structured cognition (`:462-514`; "never a vote"). `_council_majority` (`cognition/consultation_ledger.py:109-126`) is used **only** to grade per-advisor agreement scorecards (`:194,211-212`), consumed by dashboard telemetry — **no decision consumer**. |
| **V-019** | COUNCIL | MEDIUM | ✅ FIXED | Advisor "agreement" is **self-calibration of the Brain's own independently-formed hypothesis** (`cognition/brain.py:1823-1879`, boost 1.1 / penalty 0.8 vs the Brain's leading direction formed before advisors), not a council tally; each advisor becomes its own Evidence with `polarity=0.0` (`cognition/evidence_adapters.py:603-655`). |
| **V-020** | OPPORTUNITY_HARVESTING | LOW | ✅ FIXED | `adaptive/vote_calibrator.py` removed (guard `vote_consensus_engine` requires absence and PASSES). Only inert vestiges: an always-`None` `vote_calibrator` param at `scanner/candle_close_handler.py:77` and a stale docstring. |
| **V-021** | OPPORTUNITY_HARVESTING | HIGH | ⚠️ PARTIAL | `adaptive/signal_ledger.py` still grades `direction_correct` ("did price move in the predicted direction?", `:14-16,63-65`) and `adaptive/module_governor.py` governs the 9 directional-vote modules; both `enabled=True` (`config.py:1469,1652`). **Severed from live path:** the only live feed calls `record_signal(...)` with the wrong signature (`event_driven_bootstrap.py:5439`) → `TypeError` swallowed → nothing recorded; `ModuleGovernor.is_suppressed()` has **zero production callers**. Directional-correctness learning target + `=True` flags persist → one wire from re-activation. |
| **V-022** | OPPORTUNITY_HARVESTING | MEDIUM | ✅ FIXED | Opportunity-harvest **sizing** opt-in (`risk/position_sizer.py:58-61`, `config.py:652`, `allow_min_lot_over_risk`) is a capital-participation accommodation (broker-minimum lot), default off, broker floor still the real cap — not a directional vote/consensus mechanism. |
| **V-023** | LEGACY | MEDIUM | ✅ FIXED | All 16 retired modules physically gone (see §3 table) — vote/consensus engine, decider cluster, retired entry pipeline, `single_path.py`, `legacy_audit.py`. Bonus: `brain/opportunity_ranker.py` also gone. No live imports of any retired module. |
| **V-024** | LEGACY | LOW | ⚠️ PARTIAL | Sanitizer is live-enforced: `cognition/contracts.py::scrub_directional` zeroes polarity + strips directional keys (`:289-313`) and `MarketState.add` calls it on every item (`:336-342`), routed by the live consolidator. **Residual config debt:** `VoteCalibratorConfig` (`config.py:1514`, `vote_calibration_enabled=True`) and `OpportunityRankerConfig` (`config.py:341`, `execute=True`) remain **wired** though their consumers are retired; `cognition.single_path` flag orphaned. **Latent gap:** `scrub_directional` matches keys by *exact lowercased top-level name only* (`:308-309`) — a non-canonical/nested directional key would slip through (safe today only because the arbitrary-key `evidence_from_votes` adapter is unwired). |
| **V-025** | SEMANTIC | LOW | ❌ PRESENT | `dashboard/state_brain.py:82-172` `get_module_votes()` still emits per-module `direction/confidence/signed` + `consensus_direction/consensus_net/consensus_agreement/long_count/short_count`, exposed live at `dashboard/api.py:288-291` (`/api/module-votes`) + frontend `ModuleVotes.jsx`. Renders empty (no WorldModel `.votes` post-cutover) but the **directional field-contract/labels persist to operators.** |
| **V-026** | SEMANTIC | LOW | ❌ PRESENT | `dashboard/state_brain.py:175-244` `get_ranker()` emits `direction_distribution{LONG,SHORT}`, `consensus_direction`, `live_direction`, `ranker_override`, `net_score`, exposed live at `dashboard/api.py:293-296` (`/api/ranker`) + `Ranker.jsx`. Presents a "ranker override vs consensus" authority. Renders empty; surface unremediated. |
| **V-027** | SEMANTIC | LOW | ❌ PRESENT | `dashboard/state_departments.py:129-132` defines department #2 **"Consensus" — "Transforms evidence into a market thesis — the active entry trigger"**; frontend nav shows **"② Consensus"** (`frontend/src/components/Layout.jsx:131-134`). Operators are shown a "Consensus" entry-trigger authority instead of the cognitive model (hypotheses/opportunities/thesis). |

**Per-mandate note — "exactly what remains" for PARTIAL items** is documented inline
in each Notes cell above and elaborated for the highest-risk cases in §5.

---

## 2. RETIRED-MODULE EXISTENCE CHECK (V-023 / guard `exists`)

| Module | Status |
|---|---|
| `brain/directional_consensus.py` | GONE |
| `brain/vote_evidence.py` | GONE |
| `adaptive/vote_calibrator.py` | GONE |
| `cognition/single_path.py` | GONE |
| `cognition/legacy_audit.py` | GONE |
| `decision/engine.py` · `situation.py` · `governor.py` · `journal.py` · `context.py` | GONE |
| `entry/zone_watcher.py` · `entry_gate.py` · `entry_orchestrator.py` · `flip_confirmer.py` · `tick_delta_analyzer.py` | GONE |
| `trigger/entry_validator.py` | GONE |
| `brain/opportunity_ranker.py` (bonus) | GONE |

---

## 3. NEW VIOLATIONS FOUND (adversarial re-scan)

Same format as the register (ID | severity | live/dormant | evidence).

| V-ID | Sev. | Reach | Violation & evidence |
|---|---|---|---|
| **V-NEW-001** | CRITICAL | **LIVE** | **Pre-reasoning indicator-vote collapse in the Brain's native reasoner.** `cognition/brain_reasoning.py:301-326` `_directional_lean` sums `+1/-1` for structure / order-flow / absorbed-sweep and thresholds to `LONG/SHORT/FLAT` (docstring: "Combine directional signals into a single LONG/SHORT/FLAT lean"). It runs in `analyze_evidence` **before** `generate_hypotheses` and seeds hypothesis direction (`:390,407,426`); on LLM outage it drives `_originate_independent` → `OPEN_CAMPAIGN`. This is verbatim what Part XL forbids ("NO indicator voting / NO collapsing into LONG/SHORT/FLAT before reasoning; direction is a CONSEQUENCE of cognition"). **The guard never scans `cognition/brain_reasoning.py`** — complete blind spot. (Root cause behind V-001's PARTIAL status; called out separately because it is a distinct live mechanism.) |
| **V-NEW-002** | HIGH | **LIVE (default)** | **The strongest fixes are inert on shipped defaults.** `ev_primary_gate=False` (`config.py:3752`), `min_advisors_for_action=1` + `min_evidence_domains=1` (`config.py:3757,3761`, shipped `.env`), budgets unmetered (`llm/client.py:731-733`), `scan_directional_exits_enabled=True` (`config.py:666`). The constitutional guarantees exist in code but are **not active in the deployed configuration** — compliance depends entirely on operator config. |
| **V-NEW-003** | HIGH | **LIVE** | **Fail-open cognitive gates.** UNKNOWN opportunity ⇒ actionable (`cognition/contracts.py:525`); quorum passes when `advisors_responded is None` (`cognition/brain.py:601-604`); evidence-domain gate passes when `domain_count` absent (`:620`); native independent origination can emit `OPEN_CAMPAIGN` under **total provider outage with zero advisors** (`:336-341,1971-2090`, sized 0.5×). Uncertainty/absence becomes action permission — inverts fail-closed. |
| **V-NEW-004** | HIGH | dormant (one wire) | **Forbidden deciders present-but-unreachable.** `planning/trade_planner.py` weighted directional agreement + BUY/SELL (`:234,374-413`, guard-excluded); `adaptive/signal_ledger.py` + `module_governor.py` directional-correctness learning (`enabled=True`, feed broken / `is_suppressed` uncalled); `adaptive/adaptive_weight_provider.py` learns directional predictiveness and is wired live via `brain/decision_core.set_evidence_weight_provider` (`core/system_context.py:1743`) but its consumer `compute_bias` is deleted. Each is a single call-site from reactivating a directional authority without tripping the guard. |
| **V-NEW-005** | HIGH | **LIVE (deployed)** | **No enforced provider diversity / local failsafe.** Shipped `.env` roster carries no Tier-3 local model and no quota limits; `tests/test_env_local_classification.py` FAILS (missing local failsafe / deep reasoner). Runtime never detects the missing local foundation. (Deployment-level manifestation of V-015/V-014.) |
| **V-NEW-006** | MEDIUM | **LIVE** | **Operator surfaces still speak dir/conf/votes/consensus.** Live endpoints `/api/module-votes` and `/api/ranker` (`dashboard/api.py:288-296`) and the "② Consensus" nav department billed as "the active entry trigger" (`dashboard/state_departments.py:129-132`). Even rendering empty, the operator-facing *contract* is the retired model, not the cognitive one. |
| **V-NEW-007** | LOW | debt | **Regressions / rot introduced by the removals.** Broken test imports of deleted modules: `tests/test_developing_analysis.py:18` imports `compute_bias` (deleted → ImportError on collection); `dashboard/tests/test_brain_dashboard.py:16-17` imports `brain.directional_consensus.Vote` + `brain.opportunity_ranker.rank_opportunities` (deleted). Stale docstrings claim live wiring (`adaptive/module_governor.py` "the single hook the scanner calls"; `llm/reasoner.py:10-15` vote-panel comments) — an invitation to re-wire a directional path. Orphaned config `VoteCalibratorConfig`/`OpportunityRankerConfig`/`single_path`. |
| **V-NEW-008** | MEDIUM | tooling | **The guard overstates compliance.** `scripts/constitution_audit.py` is a hardcoded file/pattern/call allow-list: it never scans `cognition/brain_reasoning.py`, `brain_output_collapse` greps only the literal `expected_value = confidence` (`:126-128`), and it excludes `planning/trade_planner.py` from the call-scan without banning `plan_trade`. Its exit-0 "clean" verdict cannot see V-NEW-001, V-005's dormant decider, or the fail-open gates. |

---

## 4. PHILOSOPHY ALIGNMENT SCORECARD

| # | Constitutional principle | Assessment | Basis |
|---|---|---|---|
| 1 | No premature directional compression before hypothesis formation | ❌ **NON-COMPLIANT** | `_directional_lean` collapses to LONG/SHORT/FLAT before hypotheses and seeds them (V-NEW-001 / V-001). |
| 2 | Observations → hypotheses → opportunities → execution | ⚠️ **PARTIAL** | LLM harvesting + EV gate implement the flow, but the native path collapses direction first and UNKNOWN opportunities are actionable (V-001, V-003). |
| 3 | Modules report observations, not directions | ⚠️ **PARTIAL** | `world_model` scrubs alignment keys; but `brain_reasoning` emits `directional_lean` and dormant `trade_planner`/`signal_ledger` remain directional (V-NEW-001, V-005, V-021). |
| 4 | The Brain is the single cognitive authority | ✅ **COMPLIANT** | Retired deciders gone (V-006/V-007); single reasoner; LONG/SHORT elsewhere is execution-layer side. |
| 5 | Opportunities have lifecycle states; not actionable when unknown | ❌ **NON-COMPLIANT** | `is_actionable` returns True for `UNKNOWN` (V-003) — explicit fail-open. |
| 6 | Management is thesis-based, not direction-comparison | ⚠️ **PARTIAL** | Thesis tree + fail-closed-on-fault exist, but legacy direction+confidence REVERSE/EXIT remains, scan-directional exits default-on outside cognition, re-entry is directional (V-008/V-009/V-010/V-012). |
| 7 | Provider health/diversity is first-class | ⚠️ **PARTIAL** | Rich machinery (UNAVAILABLE state, budgets, tiers, retry-after) but diversity/local/budgets/quorum not enforced by default; shipped config permissive (V-013/V-014/V-015). |
| 8 | Council advisors are independent, not directional voters | ✅ **COMPLIANT** | `consult` never votes/averages; majority is an observational scorecard only (V-018/V-019). |
| 9 | Adaptive learning evaluates observation quality, not directional correctness | ⚠️ **PARTIAL** | `InfluenceLedger`/`CalibrationTracker` are outcome/quality-based and live; but `SignalLedger`/`ModuleGovernor`/`AdaptiveWeightProvider` still encode directional-correctness learning, `enabled=True`, severed from live path (V-021). |
| 10 | Operator logs/dashboards reflect the cognitive model, not dir/conf/votes | ❌ **NON-COMPLIANT** | Live `/api/module-votes`, `/api/ranker`, "Consensus" entry-trigger dept (V-025/026/027, V-NEW-006). |

**Score: 2 compliant · 5 partial · 3 non-compliant.**

---

## 5. FIVE HIGHEST-RISK REMAINING EXECUTION CHAINS

1. **LLM outage → native `_directional_lean` collapse → lean-seeded hypothesis →
   `_originate_independent` → `OPEN_CAMPAIGN`.**
   `cognition/brain_reasoning.py:291-326` → `cognition/brain.py:336-341` →
   `:1971-2088` (`OPEN_CAMPAIGN` `:2058`). A pre-reasoning indicator vote becomes a
   live directional campaign exactly when providers are down — the scenario V-001
   named. The EV-primary gate that would blunt it is **default OFF** (`config.py:3752`)
   and does not govern this path.

2. **Bare legacy "LONG 0.55" reply → `is_harvesting=False` bypasses the
   lifecycle/activation gate → EV ≈ monotone(confidence) ≥ 0 → `act=True` →
   `OPEN_CAMPAIGN`.**
   `cognition/brain.py:1568-1570` → `:667-671` → `:693-704`. Direction+confidence
   still opens a campaign with no structured opportunity/EV object required.

3. **Reply omits opportunity `state` → `state=UNKNOWN` → `is_actionable=True` →
   origination.**
   `cognition/contracts.py:566` → `:513-526`. Any reply lacking a lifecycle state is
   treated as actionable — fail-open at the core of the opportunity model.

4. **Deterministic / non-cognition runtime → PositionWorker scan-direction & low-score
   exits fire by default.**
   `config.py:666` (`scan_directional_exits_enabled=True`) →
   `execution/position_worker.py:321-326,780-830`. Direction/score liquidation
   authority is active by default whenever the Brain is not the live manager (the
   cognition-mode runtime override at `event_driven_bootstrap.py:5091-5095` is the
   *only* thing suppressing it).

5. **Single advisor / absent council metadata → `quorum_ok=True` → campaign
   authorized with no diversity, unmetered quota, no local failsafe.**
   `config.py:3757` (`min_advisors_for_action=1`) + `cognition/brain.py:601-604`
   (None ⇒ pass) + shipped `.env` (no Tier-3 local, no budget limits). A degraded,
   single-provider council still trades.

---

## 6. FINAL VERDICT — DOES THE CODEBASE MIRROR THE PHILOSOPHY 1:1?

**No — not yet.** It is materially closer, but it is not a 1:1 mirror.

- **What genuinely mirrors the constitution:** the death of the vote/consensus
  engine, the retired directional-decider cluster, `calculate_entry`, and the
  `EV=confidence` proxy; the single-Brain authority; the non-voting advisory council;
  the distinct `REASONER_UNAVAILABLE` state; and vendor quota-reset honoring. Ten
  violations are cleanly fixed and two philosophy principles (single authority,
  independent council) are fully met.

- **Why it is not 1:1:** seventeen of twenty-seven violations remain PARTIAL or
  PRESENT, and the pattern is systemic rather than incidental:
  1. **Fixes are opt-in behind permissive shipped defaults** (V-NEW-002) — the
     EV-primary gate is off, quorum is 1, budgets are unmetered, scan-directional
     exits are on. The constitution is implemented but not *enabled*.
  2. **A live, pre-reasoning directional-vote collapse survives in the guard's blind
     spot** (V-NEW-001 / V-001) — the single most direct contradiction of Part XL.
  3. **Core cognitive gates fail OPEN** (V-003 / V-NEW-003) — UNKNOWN ⇒ actionable,
     absent metadata ⇒ quorum passes, total outage ⇒ can still originate.
  4. **Forbidden deciders persist dormant** (V-005, V-021, V-NEW-004) — one wire from
     live, and the guard cannot see them.
  5. **Operator surfaces still present votes/consensus/ranker** (V-025/026/027) — the
     cognitive model is not yet the face of the system.

- **Trust in the guard:** the static guard's "clean live path" is accurate *only for
  the exact files/patterns/calls it encodes*. It is a narrow allow-list with real
  blind spots (V-NEW-008); it should not be read as proof of constitutional
  compliance.

**Bottom line for the mandate:** the obvious violations were patched, but the
codebase does **not** genuinely reflect the constitutional philosophy 1:1. Closing
the gap requires, at minimum: (a) making the constitutional defaults the *shipped*
defaults (EV-primary gate on, quorum ≥ 2, budgets metered, scan-directional exits
off, local failsafe required); (b) removing or reasoning `_directional_lean` out of
the pre-hypothesis path; (c) flipping the fail-open gates to fail-closed
(UNKNOWN ⇒ not actionable, absent quorum metadata ⇒ block); (d) deleting the dormant
directional deciders and the votes/consensus operator surfaces; and (e) widening the
guard to scan `cognition/brain_reasoning.py`, ban `plan_trade`, and detect fail-open
gates.

---

## 7. METHOD & GUARD-LIMITATION NOTES

- **Guard run:** `python scripts/constitution_audit.py` → 0/8 invariants violated
  (exit 0). Reproduced with `--json` and `--quiet`.
- **Guard blind spots (do not rely on exit 0 alone):** never scans
  `cognition/brain_reasoning.py`; `brain_output_collapse` greps a single literal
  string; `retired_directional_deciders_removed` excludes
  `planning/trade_planner.py` from the call scan and does not ban `plan_trade`; no
  check inspects fail-open behavior or shipped config/`.env` defaults.
- **Tests observed during verification:** `test_reasoner_unavailable_distinction.py`
  and `test_provider_retry_after.py` pass (corroborate V-016/V-017);
  `test_env_local_classification.py` has failing cases for the missing local
  failsafe (corroborates V-015 / V-NEW-005); `tests/test_developing_analysis.py` and
  `dashboard/tests/test_brain_dashboard.py` import deleted modules and error on
  collection (V-NEW-007).
- **Scope covered:** `brain/`, `cognition/`, `llm/`, `management/`, `execution/`,
  `tick/`, `scanner/`, `adaptive/`, `decision/`, `trigger/`, `entry/`, `governor/`,
  `risk/`, `planning/`, and `dashboard/` (incl. `dashboard/state_brain.py`).

*End of verification audit. No trading logic was modified — only this document was added.*
