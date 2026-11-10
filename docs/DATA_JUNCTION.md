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

> **Automatic provisioning:** On normal single-user startup, before the event store,
> databases, or trading subsystems initialize, main.py calls
> scripts/data_repo_bootstrap.py. It clones the public
> `Eugine336/apex-trader-data` repository into the sibling directory when absent,
> verifies its Git identity, and creates the platform-appropriate junction or
> symlink automatically. No manual junction command or data-path environment
> variable is required.
>
> If an existing sibling data directory is present but has no Git metadata,
> bootstrap first creates and SHA-256-verifies a complete timestamped backup.
> It then initializes Git in place, fetches `origin/main`, sets the local
> `main` branch to track `origin/main`, installs and verifies Git LFS, and
> loads the remote tree into the index without overwriting existing files.
> A recovery ref retains the original remote main commit.
>
> When the source checkout also has a plain `data/` directory, files unique to
> that source tree are copied into the sibling data repository. If the same
> relative path contains different bytes, the sibling file is left untouched;
> the complete source directory is retained as a timestamped sibling backup.
> Databases are never merged or silently replaced. The sibling's complete
> pre-adoption copy is also retained separately. Both backups remain available
> for manual recovery.
>
> Existing valid junctions are verified, not recreated. Provisioning fails
> closed if repository identity, branch tracking, Git LFS, or junction
> verification fails. The bootstrap never edits `.env`, resets Git history,
> or force-pushes.
> Multi-tenant processes with `APEX_DATA_DIR` continue using isolated per-user
> storage and do not join the shared data repository.
>
> The data repository is public by design. Every committed file is publicly
> readable. Do not store or sync broker credentials, API keys, passwords,
> personal data, or other secrets in it. Cloning requires no authentication;
> pushing still requires an existing machine-level GitHub write credential
> (for example, a configured Git credential helper or authenticated GitHub CLI
> session). Without one, runtime data remains local and synchronization reports
> failure rather than weakening repository safety.

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

### 2. Shared, read-only reference data → `shared_data_dir()`
Reference data that is identical for every user (for example, swap-rate tables)
remains reachable even from a per-user instance whose isolated `data_dir()` is
empty.

- `runtime_paths.shared_data_dir()` always returns `<repo>/data` (the junction),
  regardless of `APEX_DATA_DIR`.
- It anchors to `runtime_paths.repo_root()`, which is **cwd-independent**.

## Why the subprocess cwd is the per-user workdir (not the repo root)

The process manager launches each instance with its **cwd set to the per-user
working directory** (`api/process_manager.ProcessManager._spawn`). A number of
engine state files are addressed by the bare relative path `data/...` and
resolve against the cwd. Pinning cwd to the workdir guarantees those files land
in `workdir/data` (== `APEX_DATA_DIR`) and **never** leak into the operator's
shared `<repo>/data` junction.

Because the cwd is therefore *not* the repo root, repo-relative reference
resources cannot be found relative to the cwd. The process manager exports
`APEX_REPO_DIR` (the code-repo root) so `runtime_paths.repo_root()` can locate
them regardless of cwd. This keeps the shared junction and code-shipped
reference configuration reachable from an isolated instance.

## Quick reference

| Need | Use | Single-user | Multi-tenant per-user |
|------|-----|-------------|------------------------|
| Writeable state | `data_dir()` | `<repo>/data` (junction) | `<workdir>/data` (isolated) |
| Shared read-only data | `shared_data_dir()` | `<repo>/data` (junction) | `<repo>/data` (junction) |
| Repo root | `repo_root()` | this checkout | `APEX_REPO_DIR` → this checkout |

## Environment variables (set automatically by the API; do not set manually)

- `APEX_DATA_DIR` — per-user writeable data tree
- `APEX_LOG_DIR` — per-user log tree
- `APEX_USER_ID` — owning user id
- `APEX_USER_CONFIG` — per-user trading-preference JSON
- `APEX_TRADE_REPORT_DB` — API database for the trade-reporting bridge
- `APEX_REPO_DIR` — code-repo root, for shared repo-relative resources


## Removing historical runtime files from the code repository

The source repository previously tracked a small set of mutable runtime files under
data/ (calibration state, journals, heartbeat/crash markers, and learned state).
Those files belong only in apex-trader-data; they are not code-shipped reference
assets. The source repository now ignores the entire data/ path and does not need
a tracked placeholder because bootstrap creates the junction automatically.

For an existing checkout whose data/ is already a verified junction, bootstrap
removes any remaining legacy data/** entries from the source Git index only.
It verifies that the path resolves to the sibling data repository first. Active
data files and timestamped backups are not deleted or modified. This prevents a
later source-repository checkout from mistaking junction contents for files
owned by the code repository.

Before pulling the cleanup commit into an already-migrated Windows checkout, run
this once from the apex-trader repository root:

```powershell
git rm --cached --force -r --ignore-unmatch -- data
git pull --ff-only
```

The first command removes legacy paths from the source repository index only; it
does not remove files from the junction target. Confirm the data junction still
points to the sibling apex-trader-data directory afterward. New clones need no
manual step: bootstrap provisions the junction and performs the same index cleanup.

Removing these paths from the current source tree does not erase their historical
versions from the public Git commit history. The data repository remains public by
design; only data intended for public visibility should be committed there.
