# Tracked SQLite Database Inventory — 2026-10-26

## Scope and method

This report comes from GitHub Actions on the `redesign/route-audit-and-ci-recovery` branch. The audit opened tracked SQLite files read-only, ran both `PRAGMA quick_check` and `PRAGMA integrity_check`, listed non-internal table schemas, and counted rows. It did not print database row contents, update files, rebuild indexes, or delete anything.

**Result:** 26 tracked `.db` files inspected. Twenty-four passed both integrity checks. Two files have index-integrity findings and must be preserved for investigation.

## Integrity findings requiring attention

| File | Reported content | Integrity result | Recommendation |
|---|---|---|---|
| `data/apex_shadow.db` (69,632 bytes) | `shadow_contracts`: 114 rows | `quick_check` reports an issue; `integrity_check` reports 72 issues, including rows missing from `idx_shadow_status` and `sqlite_autoindex_shadow_contracts_1` | Do not delete. Preserve a byte-for-byte backup before considering `REINDEX` or reconstruction. Validate uniqueness and row recovery first. |
| `data/counterfactual.db` (36,864 bytes) | `trade_attribution`: 6 rows; `attribution_cache`: 0 rows | `integrity_check` reports 2 index issues: wrong entry counts for `idx_attr_closed` and `sqlite_autoindex_trade_attribution_1` | Do not delete. Preserve a byte-for-byte backup and validate rows/uniqueness before any index repair. |

The integrity findings concern indexes. They do not by themselves prove that the underlying records are unrecoverable, and they are not a reason to discard the files.

## Full inventory

| File | Size | Non-internal tables and row counts | Integrity |
|---|---:|---|---|
| `data/apex_campaign_memory.db` | 20,480 B | `campaign_memory`: 1 | Pass |
| `data/apex_deriv_positions.db` | 12,288 B | `deriv_positions`: 0 | Pass |
| `data/apex_events.db` | 26,226,688 B | `events`: 75,918 | Pass |
| `data/apex_positions.db` | 24,576 B | `guard_state`: 0; `in_flight_intents`: 0; `managed_positions`: 0 | Pass |
| `data/apex_shadow.db` | 69,632 B | `shadow_contracts`: 114 | **Issues** |
| `data/authority.db` | 16,384 B | `authority_log`: 0; `authority_state`: 1 | Pass |
| `data/backups/positions_2026-07-30.db` | 24,576 B | `guard_state`: 0; `in_flight_intents`: 0; `managed_positions`: 0 | Pass |
| `data/backups/positions_2026-07-31.db` | 24,576 B | `guard_state`: 0; `in_flight_intents`: 0; `managed_positions`: 0 | Pass |
| `data/backups/positions_2026-08-02.db` | 24,576 B | `guard_state`: 0; `in_flight_intents`: 0; `managed_positions`: 0 | Pass |
| `data/backups/positions_2026-08-05.db` | 24,576 B | `guard_state`: 0; `in_flight_intents`: 0; `managed_positions`: 0 | Pass |
| `data/backups/positions_2026-08-08.db` | 24,576 B | `guard_state`: 0; `in_flight_intents`: 0; `managed_positions`: 0 | Pass |
| `data/behavior_discovery.db` | 28,672 B | `behavior_history`: 0; `behavior_trades`: 6; `behaviors`: 0 | Pass |
| `data/capital_allocation.db` | 36,864 B | `allocation_meta`: 1; `allocation_trades`: 6; `allocations`: 0; `rebalance_log`: 0 | Pass |
| `data/counterfactual.db` | 36,864 B | `attribution_cache`: 0; `trade_attribution`: 6 | **Issues** |
| `data/execution_profiles.db` | 32,768 B | `profile_outcomes`: 6; `profile_selections`: 1; `profiles`: 5 | Pass |
| `data/interaction_discovery.db` | 12,288 B | `interaction_cache`: 0 | Pass |
| `data/management_state.db` | 12,288 B | `management_state`: 1 | Pass |
| `data/module_governor.db` | 24,576 B | `module_governor_state`: 0; `module_governor_transitions`: 0 | Pass |
| `data/param_evolution.db` | 16,384 B | `promotions`: 0; `shadow_candidates`: 0 | Pass |
| `data/regime_detection.db` | 32,768 B | `regime_performance`: 0; `regime_states`: 0; `regime_transitions`: 0 | Pass |
| `data/replay_checkpoints.db` | 12,288 B | `replay_checkpoints`: 1 | Pass |
| `data/signal_discovery.db` | 12,288 B | `discovery_cache`: 0 | Pass |
| `data/signal_ledger.db` | 36,864 B | `signal_ledger`: 0; `signal_outcomes`: 0 | Pass |
| `data/trade_journal.db` | 28,672 B | `decisions`: 0; `post_close_checks`: 9; `post_close_pending`: 0; `trades`: 3 | Pass |
| `data/tuner_audit.db` | 16,384 B | `tuner_audit`: 5 | Pass |
| `data/virtual_modules.db` | 24,576 B | `virtual_module_transitions`: 0; `virtual_modules`: 0 | Pass |

## Deletion/repair decision pending

No database files have been removed or modified.

Potential cleanup candidates are the five dated position backups: their inspected position tables are empty and each file passes integrity checks. Even so, these are tracked historical artifacts; removal requires explicit approval. The two index-damaged databases are **not** deletion candidates.

If approved, the safe sequence is: preserve copies, verify no runtime process is using them, remove only the specifically approved paths from the repository, then add a separate repair/rebuild plan for the two index-damaged files. A tracked database cleanup should not be mixed into an unreviewed live-trading restart.

`.env` was not read or modified by this audit.
