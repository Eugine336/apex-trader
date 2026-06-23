# Data Junction & Multi-Tenant Data Isolation

APEX uses two repositories that live side by side on the operator's machine:

| Repo | Holds |
|------|-------|
| `apex-trader` (code) | the engine, API, dashboard |
| `apex-trader-data` (data) | runtime state DBs, journals, learned/adaptive artifacts, reference data |

They are connected with a **directory junction** (Windows) / symlink (POSIX):

```
<repo>/data  →  ../apex-trader-data
```

Everything the engine addresses as `data/...` therefore physically lives in the
data repo, which has its own git remote, daily auto-backup, and clean-start
sync (see `platforms/maintenance.py`, `platforms/clean_start.py`).

> The junction is created on the host, outside this repo. It is **not** checked
> in — a fresh clone has no `data/` directory until the operator creates the
> junction (e.g. `mklink /J data ..\apex-trader-data` on Windows, or
> `ln -s ../apex-trader-data data` on POSIX). The engine still boots without it:
> stores create their schema on first write, so a plain `data/` directory is a
> valid fallback.

## Two kinds of data — do not conflate them

`runtime_paths.py` is the single source of truth for path resolution and draws a
hard line between two concerns:

### 1. Writeable, per-instance state → `data_dir()`
Positions, journals, calibration, learned weights, governor state, etc. Resolved
via `runtime_paths.data_dir()`, which honours the `APEX_DATA_DIR` environment
variable.

- **Single-user** (`python main.py` from the repo root): `APEX_DATA_DIR` is
  unset, so this resolves to `<repo>/data` — the junction. State flows to the
  data repo and is git-synced/backed up. **The junction is respected.**
- **Multi-tenant** (instance spawned by the API control plane): the process
  manager sets `APEX_DATA_DIR=<api_data>/instances/user_<id>/data`, giving each
  user a fully **isolated** state tree. The junction and its git auto-sync /
  clean-start are deliberately **disabled** for per-user instances
  (`api/instance_config.apply_user_overrides`) — you cannot have N users
  sharing or pushing to one git data repo.

### 2. Shared, read-only reference data → `shared_data_dir()` / `checkpoints_dir()`
Artifacts that are identical for every user (a trained RL checkpoint, a swap
rate table, broker symbol configs). These must stay reachable even from a
per-user instance whose isolated `data_dir()` is empty.

- `runtime_paths.shared_data_dir()` always returns `<repo>/data` (the junction),
  regardless of `APEX_DATA_DIR`.
- `runtime_paths.checkpoints_dir()` always returns `<repo>/checkpoints`.
- Both anchor to `runtime_paths.repo_root()`, which is **cwd-independent**.

## Why the subprocess cwd is the per-user workdir (not the repo root)

The process manager launches each instance with its **cwd set to the per-user
working directory** (`api/process_manager.ProcessManager._spawn`). A number of
engine state files are addressed by the bare relative path `data/...` and
resolve against the cwd. Pinning cwd to the workdir guarantees those files land
in `workdir/data` (== `APEX_DATA_DIR`) and **never** leak into the operator's
shared `<repo>/data` junction.

Because the cwd is therefore *not* the repo root, repo-relative shared resources
cannot be found relative to the cwd. The process manager exports `APEX_REPO_DIR`
(the code-repo root) so `runtime_paths.repo_root()` can locate them regardless
of cwd. This is what keeps `checkpoints/` and the shared junction reachable from
an isolated instance.

## Quick reference

| Need | Use | Single-user | Multi-tenant per-user |
|------|-----|-------------|------------------------|
| Writeable state | `data_dir()` | `<repo>/data` (junction) | `<workdir>/data` (isolated) |
| Shared read-only data | `shared_data_dir()` | `<repo>/data` (junction) | `<repo>/data` (junction) |
| Model checkpoints | `checkpoints_dir()` | `<repo>/checkpoints` | `<repo>/checkpoints` |
| Repo root | `repo_root()` | this checkout | `APEX_REPO_DIR` → this checkout |

## Environment variables (set automatically by the API; do not set manually)

- `APEX_DATA_DIR` — per-user writeable data tree
- `APEX_LOG_DIR` — per-user log tree
- `APEX_USER_ID` — owning user id
- `APEX_USER_CONFIG` — per-user trading-preference JSON
- `APEX_TRADE_REPORT_DB` — API database for the trade-reporting bridge
- `APEX_REPO_DIR` — code-repo root, for shared repo-relative resources
