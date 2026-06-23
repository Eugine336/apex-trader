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

## 3. Remaining roadmap (deliberately NOT done here — and why)

These were scoped but are intentionally deferred because a careless half-fix on a
live trading system would be worse than none. Each needs a dedicated, tested PR.

### 1B — Symbol-relative conviction
`form_thesis` (`brain/directional_consensus.py:375`) computes
`conviction = 0.5·agreement + 0.5·net_sat` with **no symbol awareness**, even
though `symbol` is in scope at every call site
(`event_driven_bootstrap.py:~4538`, `brain/backtest_engine.py:~1142`). To make
0.82 mean something relative to a symbol's own history we must first **add a
per-symbol conviction distribution store** (rolling mean/percentile) — none
exists today. `adaptive/pair_learner.PairLearner.get_profile` (win_rate) and
`adaptive/zone_edge_tracker.ZoneEdgeTracker.snapshot` (per-`(symbol,dir,zone,regime)`
win_rate) provide per-symbol signals to seed it, but not a conviction range.
Touching the raw conviction also shifts the entry trigger gate and the 0–100
score that flows into DecisionEngine/RL/sizing, so it needs its own validation.

### 1C — Shard global learners by symbol
`VoteCalibrator`, `ModuleGovernor`, `GateTuner` are **module/family-keyed
globals** (single shared instance/store each, built in `core/system_context.py`).
The two hot read hooks where `symbol` is already available are
`brain/decision_core.py:413` (`module_governor.is_suppressed`) and `:425`
(`vote_calibrator.calibrated_weight`). BUT their **record** paths
(`recalibrate`/`evaluate_transitions`/`calibrate`, driven by `TunerAgent`) are fed
by emitter-feedback aggregated **across all symbols**. Adding per-symbol read
buckets without first sharding the feedback source would leave those buckets
permanently empty → always fall back to global → zero behavior change but added
complexity. The correct order is: (1) record emitter feedback per `(symbol, module)`,
(2) shard the stores with old flat payloads as a `__global__` fallback, (3) thread
`symbol` through the read hooks. `ScoreOptimizer` is the cheap win — it already
keys per asset-class and its trades carry `t['pair']`, so a `symbol_weights`
bucket (symbol → class → global lookup) is low-risk and format-compatible.

### Dept verification (multi-tenant awareness)
Per-process isolation makes every department's LiveState per-user by construction
(the dashboard runs inside each engine subprocess, proxied via
`api/routes/engine_proxy.py`). The prior `MULTI_TENANT_LOOP_AUDIT.md` already
confirmed the feedback→learning→evolution stores resolve through
`runtime_paths.data_dir()`. Outstanding pre-existing bug to fix in its own PR:
`api/instance_config.py` sets `APEX_REPO_DIR` from `runtime_paths.repo_root()`
rather than the passed `repo_root` param (the two `test_api_process_manager.py`
failures above pin this).
