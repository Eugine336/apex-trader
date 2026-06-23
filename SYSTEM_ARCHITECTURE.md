# APEX TRADER — Multi-Tenant Integration & Instrument Intelligence

This document records the line-by-line audit that backed the changes in this PR,
what was implemented, and the precise remaining roadmap. Every claim carries a
`file:line` anchor so the next engineer can verify against the raw code (not docs).

---

## 1. Data-flow recap (single source of truth)

```
tick / candle-close
  → scanner/candle_close_handler.py  (per-TF brain modules → WorldModel)
  → brain/decision_core.build_consensus(symbol, wm, …)  (votes + candidates)
  → brain/directional_consensus.form_thesis(votes, …)   (conviction, trigger)
  → entry/entry_orchestrator + entry/entry_gate          (gates, score floor)
  → decision/engine.decide_entry / decide_management     (enter / size / exit)
  → execution → broker
  → event_driven_bootstrap._on_trade_closed              (feedback fan-out)
  → adaptive/* + planning/* + brain/calibration_engine   (learning)
```

In **multi-tenant** mode each user runs the whole chain as its own OS process
(`api/process_manager.py:_spawn`), so all module-level globals and `ctx.*`
components are inherently per-user. The **only** cross-user surface is the
filesystem; isolation therefore reduces to *where each component reads/writes*,
which is governed by `runtime_paths.data_dir()` (= `$APEX_DATA_DIR`, the
per-user workdir) vs `runtime_paths.shared_data_dir()` / `checkpoints_dir()`
(repo-anchored, read-only shared assets).

---

## 2. What this PR changed

### Fix 1A — Per-symbol CalibrationEngine enabled (instrument intelligence)
The CalibrationEngine (`brain/calibration_engine.py`) is the system's only true
per-symbol intelligence layer (rolling ATR, spread medians, `vol_by_hour[24]`,
structure/zone reliability, per-currency news EMA). It was **unreachable**:
`CalibrationConfig` existed (`config.py`) but was **not a field on `AppConfig`**,
so every consumer's `getattr(self._config, "calibration", None)` returned `None`.

- `config.py`: added `calibration: CalibrationConfig = field(default_factory=CalibrationConfig)`
  to `AppConfig` and flipped `CalibrationConfig.enabled` default `False → True`.
- Consumers already gated on this flag and wire it correctly:
  `event_driven_bootstrap.py` (`set_stats_provider`, load on boot, save on
  shutdown, `record_zone_outcome` on close), `scanner/candle_close_handler.py`
  (`update_candles`/`update_spread`), `brain/backtest_engine.py` (same, so
  backtest stays in parity).
- Cold-start is unchanged: `brain/instrument_profile.get_profile` → `derive_profile`
  only overrides geometry once a symbol `is_calibrated` (≈19 M5 candles), and
  floors every derived value at 0.5× the category prior. Until then it returns
  the hardcoded category constants exactly as before.
- Per-user safety: the stats provider is a per-process global and
  `calibration_state.json` resolves under the per-user cwd → no cross-user bleed.
- Test updated: `tests/test_calibration_engine.py::test_calibration_enabled_by_default`.

### Fix 2 — Multi-tenant per-user data sync (restored auto-backup)
Single-user mode auto-committed/pushed the `data/` junction
(`platforms/maintenance.py` → `scripts/backup_data.sync_data_repo`). Spawned
instances write to isolated, **non-git** workdirs (`api_data/instances/user_<id>/data`)
and `api/instance_config.apply_user_overrides` deliberately disables their sync —
so nothing was being backed up in multi-tenant mode.

- `scripts/backup_data.py`:
  - `sync_data_repo(..., data_dir=None)` — parameterised the work tree.
  - `mirror_instances(instances_dir, dest_root, …)` — copies each
    `instances/user_<id>/data` → `<junction>/instances/user_<id>/data`
    (CSV/oversize filtered). **Per-user namespace ⇒ no user can overwrite another.**
  - `sync_instances_to_data_repo(…)` — mirror, then one commit+push on the junction.
- `api/process_manager.py`: `start_data_sync()` + `_data_sync_loop()` (interval
  thread) + `sync_now()`; stopped cleanly in `shutdown()`. Non-fatal by contract.
- `api/config.py`: `data_sync_enabled` (default True), `data_sync_interval_seconds`
  (3600), `data_sync_branch` (main).
- `api/app.py`: `process_manager.start_data_sync()` in the lifespan.
- The junction (`<repo>/data`, the apex-trader-data clone) remains the single git
  work tree; the admin/root-level files stay the aggregate view, per-user data
  lands under `instances/user_<id>/`. If the junction is not a git repo the sync
  returns a soft warning and trading is never disrupted.

### Fix 4 — Admin instance overview
`api/routes/admin.py` already exposes `/api/admin/instances`, `/users/performance`,
`/stats`, `/trades`, `/aggregate` (full per-instance status/pid/uptime/restarts).
Added `POST /api/admin/data-sync` → `ProcessManager.sync_now()` for manual
trigger/visibility of the new sync.

### Tests
- `tests/test_multitenant_data_sync.py` — 9 new tests (mirror namespacing, CSV
  exclusion, no overwrite, idempotency, non-git/missing junction, `data_dir`
  override). All pass; `ruff` clean.
- Pre-existing failures in `tests/test_data_autosync.py::TestCleanStart` and
  `tests/test_api_process_manager.py` (`APEX_REPO_DIR`/FK) reproduce on the clean
  base commit and are **unrelated** to this PR.

---

## 3. Instrument-awareness layer — IMPLEMENTED (Governor + 1B + 1C)

The items below were previously deferred; this PR implements them end-to-end,
each behaviour-neutral at cold start and backward-compatible.

### Fix 1 — ModuleGovernor (and VoteCalibrator) actually enabled
Both learners read their master switch + thresholds off the config object handed
to them, but `core/system_context.py` passed the **top-level `AppConfig`** while
those fields live on the nested `ModuleGovernorConfig` / `VoteCalibratorConfig`.
`getattr(config, "module_governor_enabled", False)` therefore resolved to the
`False` default and the Governor was **permanently inert** (no module ever
shadowed) despite `module_governor_enabled=True`. The VoteCalibrator had the same
latent bug. Fix: pass `config.module_governor` / `config.vote_calibrator`. Both
are cold-start neutral (the Governor leaves every module ACTIVE until it has
enough graded signals; the calibrator returns neutral 1.0 until ≥2 modules
qualify), so enabling them changes nothing until real graded data accrues.

### 1B — Symbol-relative conviction (Learning ⑦ → Consensus ②)
New leaf `adaptive/symbol_conviction.SymbolConvictionStore` keeps a bounded,
per-symbol history of raw `form_thesis` convictions and exposes
`record_and_normalize(symbol, raw)` → the raw value re-expressed as its percentile
rank within that symbol's own distribution, blended back toward raw by
`ConvictionNormalizationConfig.blend` (default 0.5). Wired into
`brain/directional_consensus.form_thesis` via optional `symbol` + `conviction_store`
params, applied identically on the **live** path
(`event_driven_bootstrap._evaluate_consensus_entry`, store from
`ctx.symbol_conviction`) and the **backtest** path (`brain/backtest_engine`, a
non-persistent store that warms within the run) — so the planes stay parity-
preserving. Cold-start neutral: until a symbol reaches `min_samples` (30) the raw
conviction passes through unchanged, so the trigger gate is identical to before.
`ConsensusThesis.raw_conviction` preserves the pre-normalization value for audit.
Per-user isolated persistence under `runtime_paths.data_dir()`.

### 1C — Shard global learners by symbol (instrument isolation)
The accuracy-driven learners are sharded by reusing the per-pair query the
`SignalLedger`/`EmitterFeedbackService` already support
(`EmitterFeedbackRequest.pair`):

* **VoteCalibrator** — `multiplier_for(module, symbol=None)` /
  `calibrated_weight(module, base_weight, symbol=None)` consult a per-symbol
  multiplier overlay computed (throttled, accuracy-only) from that symbol's own
  graded accuracy. A symbol with <2 qualifying modules falls back to the global
  (neutral) multiplier.
* **ModuleGovernor** — `is_suppressed(module, symbol=None)` consults a per-symbol
  suppression overlay: a module poor on THIS symbol is shadowed here even if
  globally ACTIVE, and a module reliable here is allowed even if globally
  SHADOWED. Symbols without enough per-symbol data defer to the global state
  machine (whose SHADOW→DISABLED hysteresis + audit are untouched).

`symbol` is threaded through `brain/decision_core._add_vote` (already in scope),
with a `TypeError` fallback so older/stub learners stay compatible. Result:
GBPJPY's losses no longer recalibrate or shadow a module on EURUSD. `ScoreOptimizer`
already keys per asset-class, so it is left as-is. The global record paths
(`recalibrate`/`evaluate_transitions`, TunerAgent-driven) are unchanged.

**Instrument-intelligence grade:** moves from **B (partially instrument aware)**
toward **A** — conviction is now symbol-relative and the two accuracy-driven
learners isolate per symbol, on top of the already-enabled per-symbol
CalibrationEngine.


### Dept verification (multi-tenant awareness)
Per-process isolation makes every department's LiveState per-user by construction
(the dashboard runs inside each engine subprocess, proxied via
`api/routes/engine_proxy.py`). The prior `MULTI_TENANT_LOOP_AUDIT.md` already
confirmed the feedback→learning→evolution stores resolve through
`runtime_paths.data_dir()`. Outstanding pre-existing bug to fix in its own PR:
`api/instance_config.py` sets `APEX_REPO_DIR` from `runtime_paths.repo_root()`
rather than the passed `repo_root` param (the two `test_api_process_manager.py`
failures above pin this).

## 4. Nested-config-passthrough audit (system-wide sweep)

After the Governor/VoteCalibrator/CalibrationEngine fixes, every component that
`core/system_context.py` builds with a config object was swept for the same
bug class: a subsystem reads `self.config.<field>`, but it was handed the
top-level `AppConfig` (or read a field/key that does not exist on what it was
handed), so the value silently fell through to a constructor/`getattr` default
and the operator's configured value never took effect.

All instantiation is centralized in `core/system_context.py` — there are no
component constructions in `main.py` or `event_driven_bootstrap.py` for these
(bootstrap only builds `CalibrationEngine`, `ZoneEdgeTracker`, `NewsImpactTracker`).

### Fixed (config now authoritative)

| Component | Bug | Fix |
|---|---|---|
| `OutcomeFeedback` | passed top-level `AppConfig`; `accuracy_lookback`/`journal_path`/`enabled` fell to defaults | pass `config.outcome_feedback` |
| `PostCloseTracker` | passed `AppConfig`; `enabled`/`check_intervals_minutes`/`max_retries` fell to defaults (ran with in-code default, ignored config) | pass `config.post_close_tracker` |
| `CounterfactualEngine` | correct object, wrong field names (`enabled`/`lookback`/`interval`) | read `counterfactual_enabled`/`attribution_lookback`/`attribution_interval` |
| `InteractionAnalyzer` | correct object, wrong field names (`enabled`/`lookback`/`interval`) | read `interaction_discovery_enabled`/`interaction_lookback`/`interaction_interval` |
| `TunerAgent` | wrong key `getattr(config, "tuner")` (field is `tuner_agent`) → `None` → all params default | read `config.tuner_agent`; wire all fields |
| `RegimeDetector` (adaptive, L7) | wrong key `getattr(config, "regime_detector")` (field is `regime_detection`) → `None` → lookback/hysteresis default | read `config.regime_detection` |
| `AdaptiveWinRateProvider` | read `adaptive_win_rate_provider_enabled` off `AppConfig` (field is on `OpportunityRankerConfig`) | read `config.opportunity_ranker.adaptive_win_rate_provider_enabled` |
| `SignalLedger` | constructed with no config; `SignalLedgerConfig` grading delay/intervals/min-move/lookback ignored | wire `config.signal_ledger` params |
| `VirtualSignalManager` | passed `AppConfig`; `virtual_promotion_enabled`/`signal_discovery_enabled`/`feedback_lookback` fell to `False`/default → cluster permanently inert (the Governor symptom) | pass `config.signal_discovery` |
| `SignalDiscoveryEngine` | constructed with no config; `enabled` + all mining params used constructor defaults | wire `config.signal_discovery` |
| `CapitalAllocator` | constructed with no config; `CapitalAllocationConfig` ignored | wire `config.capital_allocation` (defaults coincide → behaviour-neutral) |
| `ExecutionProfileManager` | constructed with no config; `ExecutionProfileConfig` ignored | wire `config.execution_profiles` (behaviour-neutral) |

### Config-default alignments (preserve current runtime; make config authoritative)

- `PostCloseTrackerConfig.enabled`: `False` → `True`. The tracker had been running
  (silently `True`) and is purely observational (MFE/MAE; never changes a live
  decision). Setting the default `True` preserves runtime and keeps feeding the
  learning layer; the docstring was updated to match.
- `SignalDiscoveryConfig.signal_discovery_enabled` and `virtual_promotion_enabled`:
  `True` → `False`. The dataclass values contradicted the class docstrings
  ("Defaults OFF"). Because the live consumer read them off the wrong object, the
  cluster has always run dormant. Fixing the passthrough alone would have flipped
  on live promotion of auto-discovered vote modules to live trading weight — a
  risky behaviour change the docstrings explicitly warn against. The defaults are
  aligned to the documented OFF intent so the fix is behaviour-neutral (cluster
  stays dormant) while becoming config-authoritative. Operators flip
  `signal_discovery.virtual_promotion_enabled = True` to activate.

### Confirmed correct (no change)

`VoteCalibrator`, `ModuleGovernor` (prior fixes), `CalibrationEngine`,
`SymbolConvictionStore`, `ScoreOptimizer` (`config.scoring`), `PairLearner`
(`config.pair_learner`), `EntryEngine` (reads `config.risk`/`config.scoring`…),
`DecisionEngine`, `OpportunityExecutor`, `Orchestrator`, `ParameterEvolver`,
`RecommendationGateway`, `GovernanceDivision`, `ComplianceDivision`,
`DailyMaintenance`, `ProcessWatchdog`. `GateTuner`/`ZoneEdgeTracker` take no
config object and have no `enabled` gate (passive per-symbol stores) — not
applicable.

### Noted, benign (out of the bug class)

- `event_driven_bootstrap.py` reads `getattr(_calib_cfg, "persist", True)` but
  `CalibrationConfig` has no `persist` field → always loads (the intended
  behaviour); harmless.
- `event_driven_bootstrap.py` reads `getattr(self._config, "ed_warmup_on_start", True)`
  — a vestigial flag with no config field anywhere; defaults `True` (warmup runs).

### Tests

`tests/test_config_passthrough_wiring.py` builds a real `AppConfig` with
distinctive non-default nested values and asserts each fixed component reflects
its nested config (proving the nested config reaches the component), plus the two
default-alignment guards.
