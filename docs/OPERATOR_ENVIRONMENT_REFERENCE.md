# Operator environment configuration reference

Generated from config_env.py and config.py on branch fix/mt5-automatic-broker-clock.

The root .env.example exposes 101 typed operational settings across 16 sections. Empty assignments preserve typed defaults. The parser rejects malformed values and invalid operational ranges fail startup.

This is an allowlist, not a blanket environment override of all 716 typed fields. Analytical cutoffs, learned/statistical thresholds, broker-derived facts, destructive data-purge switches and immutable safety controls are not operator overrides.

| Environment variable | Config field | Type | Default |
|---|---|---|---|
| APEX_CANDLE_CACHE_ENABLED | PerformanceConfig.candle_cache_enabled | bool | True |
| APEX_CANDLE_CACHE_TTL | PerformanceConfig.candle_cache_ttl | dict[str, float] | factory default (defined in config.py) |
| APEX_CANDLE_CACHE_DEFAULT_TTL | PerformanceConfig.candle_cache_default_ttl | float | 5.0 |
| APEX_M1_CACHE_TTL | PerformanceConfig.m1_cache_ttl | float | 1.0 |
| APEX_SCAN_INTERVAL_ACTIVE | PerformanceConfig.scan_interval_active | int | 5 |
| APEX_SCAN_INTERVAL_WITH_POSITIONS | PerformanceConfig.scan_interval_with_positions | int | 5 |
| APEX_PARALLEL_SCAN_ENABLED | PerformanceConfig.parallel_scan_enabled | bool | True |
| APEX_PARALLEL_SCAN_MAX_WORKERS | PerformanceConfig.parallel_scan_max_workers | int | 10 |
| APEX_ACCOUNT_INFO_CACHE_ENABLED | PerformanceConfig.account_info_cache_enabled | bool | True |
| APEX_ENABLED | OpsConfig.enabled | bool | True |
| APEX_SHUTDOWN_TIMEOUT_SECONDS | OpsConfig.shutdown_timeout_seconds | float | 30.0 |
| APEX_CRASH_MARKER_PATH | OpsConfig.crash_marker_path | str | "data/.crash_marker" |
| APEX_HEARTBEAT_INTERVAL_SECONDS | OpsConfig.heartbeat_interval_seconds | float | 10.0 |
| APEX_HEARTBEAT_FILE | OpsConfig.heartbeat_file | str | "data/.heartbeat" |
| APEX_MAX_TICK_DURATION_SECONDS | OpsConfig.max_tick_duration_seconds | float | 60.0 |
| APEX_TICK_PROFILING_ENABLED | OpsConfig.tick_profiling_enabled | bool | True |
| APEX_SLOW_TICK_THRESHOLD_MS | OpsConfig.slow_tick_threshold_ms | float | 100.0 |
| APEX_PROFILING_WINDOW_SIZE | OpsConfig.profiling_window_size | int | 1000 |
| APEX_LOG_FORMAT | OpsConfig.log_format | str | "json" |
| APEX_LOG_LEVEL | OpsConfig.log_level | str | "INFO" |
| APEX_LOG_MAX_SIZE_MB | OpsConfig.log_max_size_mb | int | 50 |
| APEX_LOG_MAX_FILES | OpsConfig.log_max_files | int | 10 |
| APEX_MAIN_LOG | OpsConfig.main_log | str | "logs/apex.log" |
| APEX_TRADE_AUDIT_LOG | OpsConfig.trade_audit_log | str | "logs/trade_audit.log" |
| APEX_RISK_AUDIT_LOG | OpsConfig.risk_audit_log | str | "logs/risk_audit.log" |
| APEX_RECONNECT_MAX_RETRIES | OpsConfig.reconnect_max_retries | int | 10 |
| APEX_RECONNECT_BASE_DELAY_SECONDS | OpsConfig.reconnect_base_delay_seconds | float | 5.0 |
| APEX_BACKUP_ENABLED | DataBackupConfig.enabled | bool | True |
| APEX_BACKUP_INTERVAL_HOURS | DataBackupConfig.interval_hours | int | 1 |
| APEX_BACKUP_MAX_FILE_SIZE_MB | DataBackupConfig.max_file_size_mb | float | 95.0 |
| APEX_BACKUP_EXCLUDE_PATTERNS | DataBackupConfig.exclude_patterns | list[str] | factory default (defined in config.py) |
| APEX_BACKUP_AUTO_SYNC_DATA_REPO | DataBackupConfig.auto_sync_data_repo | bool | True |
| APEX_BACKUP_SYNC_BRANCH | DataBackupConfig.sync_branch | str | "main" |
| APEX_BACKUP_DATA_REPO_URL | DataBackupConfig.data_repo_url | str | "https://github.com/Eugine336/apex-trader-data.git" |
| APEX_BACKUP_SYNC_ORPHAN_BRANCH | DataBackupConfig.sync_orphan_branch | bool | True |
| APEX_ENABLED | BacktestConfig.enabled | bool | True |
| APEX_DEFAULT_SPREAD_PIPS | BacktestConfig.default_spread_pips | float | 2.0 |
| APEX_DEFAULT_SLIPPAGE_PIPS | BacktestConfig.default_slippage_pips | float | 0.5 |
| APEX_USE_MEMORY_DB | BacktestConfig.use_memory_db | bool | True |
| APEX_PROGRESS_INTERVAL | BacktestConfig.progress_interval | int | 100 |
| APEX_SYNTHETIC_SEED | BacktestConfig.synthetic_seed | int | 42 |
| APEX_STARTING_BALANCE | BacktestConfig.starting_balance | float | 10_000.0 |
| APEX_TRACE_ENABLED | DecisionTraceConfig.enabled | bool | True |
| APEX_FEEDBACK_ENABLED | OutcomeFeedbackConfig.enabled | bool | True |
| APEX_FEEDBACK_JOURNAL_PATH | OutcomeFeedbackConfig.journal_path | str | "data/outcome_feedback.jsonl" |
| APEX_FEEDBACK_ACCURACY_LOOKBACK | OutcomeFeedbackConfig.accuracy_lookback | int | 300 |
| APEX_FEEDBACK_GOLD_SPECIALIST | OutcomeFeedbackConfig.gold_specialist | bool | False |
| APEX_LEDGER_SIGNAL_LEDGER_ENABLED | SignalLedgerConfig.signal_ledger_enabled | bool | True |
| APEX_LEDGER_SIGNAL_GRADING_ENABLED | SignalLedgerConfig.signal_grading_enabled | bool | True |
| APEX_LEDGER_SIGNAL_GRADING_DELAY_MINUTES | SignalLedgerConfig.signal_grading_delay_minutes | int | 30 |
| APEX_LEDGER_SIGNAL_GRADING_CHECK_INTERVALS | SignalLedgerConfig.signal_grading_check_intervals | list[int] | factory default (defined in config.py) |
| APEX_LEDGER_EMITTER_FEEDBACK_ENABLED | SignalLedgerConfig.emitter_feedback_enabled | bool | True |
| APEX_LEDGER_SIGNAL_LEDGER_DB_PATH | SignalLedgerConfig.signal_ledger_db_path | str | "data/signal_ledger.db" |
| APEX_LEDGER_ACCURACY_LOOKBACK | SignalLedgerConfig.accuracy_lookback | int | 100 |
| APEX_CLOSE_TRACKER_ENABLED | PostCloseTrackerConfig.enabled | bool | True |
| APEX_CLOSE_TRACKER_DB_PATH | PostCloseTrackerConfig.db_path | str | "data/trade_journal.db" |
| APEX_CLOSE_TRACKER_CHECK_INTERVALS_MINUTES | PostCloseTrackerConfig.check_intervals_minutes | list[int] | factory default (defined in config.py) |
| APEX_CLOSE_TRACKER_MAX_RETRIES | PostCloseTrackerConfig.max_retries | int | 3 |
| APEX_AGENT_ENABLED | TunerAgentConfig.enabled | bool | True |
| APEX_AGENT_MAX_TUNE_DURATION_SECONDS | TunerAgentConfig.max_tune_duration_seconds | float | 30.0 |
| APEX_AGENT_MAX_CONSECUTIVE_FAILURES | TunerAgentConfig.max_consecutive_failures | int | 3 |
| APEX_AGENT_AUDIT_DB_PATH | TunerAgentConfig.audit_db_path | str | "data/tuner_audit.db" |
| APEX_AGENT_LOG_ALL_SKIPS | TunerAgentConfig.log_all_skips | bool | False |
| APEX_COUNTERFACTUAL_ENABLED | CounterfactualConfig.counterfactual_enabled | bool | True |
| APEX_ATTRIBUTION_LOOKBACK | CounterfactualConfig.attribution_lookback | int | 500 |
| APEX_ATTRIBUTION_INTERVAL | CounterfactualConfig.attribution_interval | int | 100 |
| APEX_MIN_TRADES_FOR_ATTRIBUTION | CounterfactualConfig.min_trades_for_attribution | int | 50 |
| APEX_COUNTERFACTUAL_DB_PATH | CounterfactualConfig.counterfactual_db_path | str | "data/counterfactual.db" |
| APEX_INTERACTION_DISCOVERY_ENABLED | InteractionConfig.interaction_discovery_enabled | bool | True |
| APEX_INTERACTION_LOOKBACK | InteractionConfig.interaction_lookback | int | 500 |
| APEX_INTERACTION_INTERVAL | InteractionConfig.interaction_interval | int | 500 |
| APEX_EXHAUSTIVE_SEARCH_MAX_MODULES | InteractionConfig.exhaustive_search_max_modules | int | 12 |
| APEX_INTERACTION_DB_PATH | InteractionConfig.interaction_db_path | str | "data/interaction_discovery.db" |
| APEX_ENABLED | CalibrationConfig.enabled | bool | True |
| APEX_STATE_PATH | CalibrationConfig.state_path | str | "data/calibration_state.json" |
| APEX_SPREAD_SAMPLE_TF | CalibrationConfig.spread_sample_tf | str | "M5" |
| APEX_PERSIST | CalibrationConfig.persist | bool | True |
| APEX_ANALYSIS_ENABLED | DevelopingAnalysisConfig.enabled | bool | True |
| APEX_ANALYSIS_MAX_WORKERS | DevelopingAnalysisConfig.max_workers | int | 4 |
| APEX_ANALYSIS_REFRESH_M5 | DevelopingAnalysisConfig.refresh_m5 | float | 10.0 |
| APEX_ANALYSIS_REFRESH_M15 | DevelopingAnalysisConfig.refresh_m15 | float | 15.0 |
| APEX_ANALYSIS_REFRESH_H1 | DevelopingAnalysisConfig.refresh_h1 | float | 30.0 |
| APEX_ANALYSIS_REFRESH_H4 | DevelopingAnalysisConfig.refresh_h4 | float | 60.0 |
| APEX_ANALYSIS_REFRESH_D1 | DevelopingAnalysisConfig.refresh_d1 | float | 300.0 |
| APEX_ANALYSIS_MANAGEMENT_ADVISORY_ENABLED | DevelopingAnalysisConfig.management_advisory_enabled | bool | True |
| APEX_ENABLED | CampaignConfig.enabled | bool | True |
| APEX_DORMANT_AFTER_SECONDS | CampaignConfig.dormant_after_seconds | float | 900.0 |
| APEX_INVALIDATE_AFTER_SECONDS | CampaignConfig.invalidate_after_seconds | float | 3600.0 |
| APEX_HISTORY_LIMIT | CampaignConfig.history_limit | int | 500 |
| APEX_POSTMORTEM_ENABLED | CampaignConfig.postmortem_enabled | bool | True |
| APEX_EVIDENCE_FULL_REFRESHES | CampaignConfig.evidence_full_refreshes | int | 5 |
| APEX_TUNER_ENABLED | AdaptiveTunerConfig.enabled | bool | True |
| APEX_TUNER_PERIODIC_INTERVAL_SECONDS | AdaptiveTunerConfig.periodic_interval_seconds | float | 900.0 |
| APEX_TUNER_SCAN_INTERVAL_SECONDS | AdaptiveTunerConfig.scan_interval_seconds | float | 3600.0 |
| APEX_INSTRUMENT_QUEUE_ENABLED | CrossInstrumentConfig.queue_enabled | bool | True |
| APEX_INSTRUMENT_QUEUE_WINDOW_MS | CrossInstrumentConfig.queue_window_ms | int | 1000 |
| APEX_INSTRUMENT_RANKING_ENABLED | CrossInstrumentConfig.ranking_enabled | bool | True |
| APEX_INSTRUMENT_QUALITY_SIZING_ENABLED | CrossInstrumentConfig.quality_sizing_enabled | bool | True |
| APEX_INSTRUMENT_DISPLACEMENT_ENABLED | CrossInstrumentConfig.displacement_enabled | bool | True |
| APEX_INSTRUMENT_PROACTIVE_SCAN_ENABLED | CrossInstrumentConfig.proactive_scan_enabled | bool | True |
| APEX_INSTRUMENT_PROACTIVE_SCAN_INTERVAL_SECONDS | CrossInstrumentConfig.proactive_scan_interval_seconds | float | 60.0 |

## Dedicated controls outside the generic allowlist

| Variable | Purpose | Default / behaviour |
|---|---|---|
| APEX_LOG_LEVEL | Root application console level | INFO; TRACE/DEBUG/INFO/SUCCESS/WARNING/ERROR/CRITICAL |
| APEX_OWNER_LIMITS_JSON | Bootstrap canonical owner-editable capital limits | Empty means typed OwnerLimits defaults; JSON passes OwnerLimits.apply_updates |
| TRADING_MODE | Instrument universe | gold, full, selected or single; example uses gold |
| TRADING_MODE_LOCKED | Prevent per-user settings widening process universe | Defaults to locked when TRADING_MODE is explicitly set |
| TRADING_ENABLED | Master switch for new exposure | Example defaults to false; ActionExecutor rejects queued OPEN intents when false |
| TRADING_SYMBOLS | Explicit symbol list for selected/single mode | Comma-separated |
| TRADING_ENABLED_CATEGORIES | Categories for full mode | forex, commodity, index, synthetic and crypto |
| COGNITION_ORIGINATION_MODE | Brain new campaign origination route | off, shadow, live |
| COGNITION_MANAGEMENT_MODE | Brain position management route | off, shadow, live |

## Parsing rules

- Boolean values: true/false, 1/0, yes/no, on/off.
- Arrays and objects: JSON syntax.
- Strings: literal values; use a JSON-quoted empty string when explicitly clearing a string.
- Invalid types, non-finite numbers, unsupported timeframes, invalid log levels and invalid operational ranges fail startup.
- .env is read at process startup. Restart the service after changing it.
- The application does not rewrite the owner's .env.

## Non-overridable configuration

OwnerLimits internal hard stops, Brain decision authority, execution gates, broker feasibility, completed-bar semantics, source timestamp integrity and fail-closed unknown states remain code-enforced. They must not become generic .env switches.
