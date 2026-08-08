# Feedback → Learning → Evolution Loop — Multi-Tenant Data Isolation Audit

**Scope:** Verify the full trading lifecycle data loop (scan → rank → decide →
open → manage → close → feedback → learning → evolution) reads/writes from
per-user isolated storage in multi-tenant mode, where each user's engine runs as
a subprocess with `cwd = instances/user_<id>/` and `APEX_DATA_DIR` pointing to
per-user writeable storage.

**Method:** Raw-code trace. Every claim carries a `file:line` reference.

---

## 0. How isolation actually works (the mechanism)

The multi-tenant process manager launches each user's engine via
`subprocess.Popen([python, <repo>/main.py, "--dashboard"], cwd=<workdir>, env=…)`
([api/process_manager.py:207-224](api/process_manager.py)), where `workdir =
instances/user_<id>/`. The injected env sets `APEX_DATA_DIR = workdir/data`,
`APEX_LOG_DIR`, `APEX_USER_ID`, `APEX_USER_CONFIG`, `APEX_TRADE_REPORT_DB`
([api/instance_config.py:141-148](api/instance_config.py)).

Because each user is a **separate OS process**, all module-level globals and
`ctx.*` component instances are inherently per-user — there is no shared mutable
in-process state across users. The only cross-user surface is the **filesystem**,
so isolation reduces entirely to *where each component reads/writes*.

Two path-resolution mechanisms were in play before this change:

| Mechanism | Resolves to | Used by |
|---|---|---|
| `runtime_paths.data_dir()` → reads `APEX_DATA_DIR` | absolute `workdir/data` | all `adaptive/*` SQLite stores, `persistence/*`, `execution/management_state`, `ShadowStore` |
| bare relative `"data/…"` literal default | `cwd/data` = `workdir/data` (cwd-dependent) | gate_tuner, zone_edge, score_optimizer, the 3 ML learners, post_close_tracker, tuner_agent, outcome_logger, RL dbs |

Both *happened* to resolve to the same per-user location only because
`APEX_DATA_DIR == workdir/data` **and** `cwd == workdir`. The relative-path
components were therefore isolated **by luck of cwd**, not by design.

---

## 1. The loop, component by component (close path)

Trade close fans out through `_on_trade_closed`
([event_driven_bootstrap.py:6150](event_driven_bootstrap.py)). Every consumer is
a per-process `ctx.*` instance:

| Stage | Component | Writes to | Resolution (after fix) |
|---|---|---|---|
| edge attribution | `ZoneEdgeTracker.record_trade` | `zone_edge.json` | `data_dir()` ✅ |
| zone calibration | `CalibrationEngine.record_zone_outcome` | in-engine store | per-process ✅ |
| feedback | `OutcomeFeedback.record_outcome` | `outcome_feedback.jsonl` | `data_dir()` ✅ (already) |
| counterfactual | `CounterfactualEngine.complete` | `counterfactual.db` | `_DB_DIR=data_dir()` ✅ |
| signal ledger | `SignalLedger.attach_trade_outcome` | `signal_ledger.db` + `archive/` | `_DB_DIR=data_dir()` ✅ |
| learning | `MLAdapter.register_new_trade` → PairLearner | `ml_pair_profiles.json` | `data_dir()` ✅ (fixed) |
| tuning | `TunerAgent.on_trade_close` | `tuner_audit.db` | `data_dir()` ✅ (fixed) |
| post-close | `PostCloseTracker.record_close` | `trade_journal.db` | `data_dir()` ✅ (fixed) |
| journal | `TradeJournal.log_trade` | trade DB | `data_dir()` ✅ (already) |
| evolution | `CapitalAllocator.record_outcome` | `capital_allocation.db` | `_DB_DIR=data_dir()` ✅ |
| evolution | `ExecutionProfileManager.record_outcome` | `execution_profiles.db` | `_DB_DIR=data_dir()` ✅ |
| evolution | `BehaviorDiscovery.record_trade` | `behavior_discovery.db` | `_DB_DIR=data_dir()` ✅ |
| evolution | `ParameterEvolver.run_cycle` | `param_evolution.db` | `_DB_DIR=data_dir()` ✅ |
| plan→outcome | `OutcomeLogger.log_outcome` | `plan_journal.jsonl` | `data_dir()` ✅ (fixed) |

The **read side** of the loop is symmetric: each learner/evolution engine reads
its own per-user file/DB at construction (e.g. `ScoreOptimizer.load_weights`,
`PairLearner._load`, the calibrator reads `OutcomeLogger`'s journal). With every
path anchored to `data_dir()` the read and write halves resolve to the same
per-user location independent of cwd.

---

## 2. Dashboard / LiveState

The read-only dashboard API runs **inside each engine subprocess**
(`--dashboard`, bound to a private loopback `DD_DASHBOARD_PORT`); the control
plane proxies JWT-authenticated requests to the owning user's port
([api/process_manager.py:171-191](api/process_manager.py)). LiveState is therefore
per-process = per-user by construction; panels (`/api/scanner`, `/api/feedback`,
`/api/learning`, …) serve only that user's engine state. The API server process
itself never imports `adaptive`/`rl`/engine modules, so there is no shared
module-level `_DB_DIR` contamination in the control plane.

---

## 3. Findings

### 🔴 BUG 1 — Shared RL checkpoint unreachable in multi-tenant (FIXED)

`resolve_rl_checkpoint()` resolved the trained model via `Path("checkpoints")`
([rl/bridge.py](rl/bridge.py)) — **relative to cwd**. In multi-tenant mode
cwd = `instances/user_<id>/`, so every instance looked for
`instances/user_<id>/checkpoints/apex_rl_best.pt`, which never exists. The
trained model is a **shared, read-only** asset shipped at `<repo>/checkpoints/`.
Result: the RL subsystem was **permanently `INACTIVE_NO_CHECKPOINT` for every
user**, even with a trained model present.

**Fix:** Added `repo_root()` / `shared_data_dir()` / `checkpoints_dir()` to
`runtime_paths.py` (honour `APEX_REPO_DIR`, fall back to the repo). The process
manager now injects `APEX_REPO_DIR` ([api/instance_config.py](api/instance_config.py)),
and `resolve_rl_checkpoint()` resolves through `checkpoints_dir()` — shared and
read-only, never the per-user cwd.

### 🟡 RISK 2 — cwd-dependent relative `data/…` defaults (FIXED)

Ten loop components used bare relative `"data/…"` defaults instead of
`runtime_paths.data_dir()`. They were isolated only because cwd happened to equal
the per-user workdir — a latent foot-gun: any future `chdir`, maintenance task,
or refactor that changed cwd would silently write a user's learned state to the
wrong directory (or, in single-user mode, miss the data-junction).

**Fix (path-equivalent in both modes, immune to cwd drift):**

| Component | File |
|---|---|
| `GateTuner` (gate offsets) | [adaptive/gate_tuner.py](adaptive/gate_tuner.py) |
| `ZoneEdgeTracker` (conviction edge) | [adaptive/zone_edge_tracker.py](adaptive/zone_edge_tracker.py) |
| `ScoreOptimizer` + `load_saved_weights` | [adaptive/score_optimizer.py](adaptive/score_optimizer.py) |
| `PairLearner` | [adaptive/pair_learner.py](adaptive/pair_learner.py) |
| `SessionLearner` | [adaptive/session_learner.py](adaptive/session_learner.py) |
| `RegimeLearner` | [adaptive/regime_learner.py](adaptive/regime_learner.py) |
| `PostCloseTracker` | [adaptive/post_close_tracker.py](adaptive/post_close_tracker.py) |
| `TunerAgent` (audit db) | [adaptive/tuner_agent.py](adaptive/tuner_agent.py) |
| `OutcomeLogger` (plan→outcome journal) | [planning/outcome_logger.py](planning/outcome_logger.py) |
| `AuthorityManager` / `ShadowEngine` / `RLBridge` (RL writeable state) | [rl/authority.py](rl/authority.py), [rl/shadow.py](rl/shadow.py), [rl/bridge.py](rl/bridge.py) |

In single-user mode `data_dir()` = `<repo>/data` (byte-for-byte the same as the
old relative default resolved from the repo cwd); in multi-tenant mode it follows
`APEX_DATA_DIR`. Explicit caller-supplied paths (used by tests with `tmp_path`)
are still honoured.

### ✅ Already-correct (no change needed)

* All `adaptive/*` SQLite stores, `persistence/*`, `execution/management_state`,
  and `ShadowStore` already used the `_DB_DIR = data_dir()` idiom.
* `OutcomeFeedback`, `CounterfactualEngine`, `SignalLedger` (and its
  `archive/` subdir), `CapitalAllocator`, `ExecutionProfileManager`,
  `BehaviorDiscovery`, `ParameterEvolver` — all anchored via `data_dir()`.
* The only `Path(__file__)`-anchored reads in the loop region are
  `brain/broker_autodiscovery.py` / `brain/symbol_mapper.py`, which read
  `config/brokers/*` — **shared, read-only reference config**, correctly
  repo-anchored.
* `PlannerConfig.journal_path` ([planning/trade_planner.py:85](planning/trade_planner.py))
  is a **dead config field** — never consumed for I/O (the OutcomeLogger owns the
  journal path). Left as-is; documented here.

---

## 4. Tests

* `tests/test_runtime_paths_isolation.py` — per-user vs shared resolution under
  single-user and multi-tenant env; regression guard that the shared checkpoint
  never resolves under the per-user writeable tree.
* `tests/test_api_instance_config.py` — new assertion that the spawned env
  isolates `APEX_DATA_DIR`/`APEX_LOG_DIR` per user **and** sets `APEX_REPO_DIR`
  to the shared code repo.

All changed modules compile and pass `ruff`. (Full `pytest` runs in CI — the
sandbox has no `numpy`/`loguru`/`pytest` installed; the dependency-free
`runtime_paths` tests were executed directly and pass.)

---

## 5. Verdict

The feedback → learning → evolution loop is now isolated per-user **by design**,
not by cwd coincidence: every mutable artifact resolves through
`runtime_paths.data_dir()` (per-user, `APEX_DATA_DIR`), and the one genuinely
shared read-only asset (the trained RL checkpoint) resolves through
`runtime_paths.checkpoints_dir()` (`APEX_REPO_DIR`). No user can read or corrupt
another user's learned state, and the trained RL model is now reachable by all
instances.
