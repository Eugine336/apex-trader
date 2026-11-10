# Operator environment configuration reference

Generated from config_env.py and config.py on branch fix/mt5-automatic-broker-clock.

The root .env.example exposes 101 typed operational settings across 16 sections. Empty assignments preserve typed defaults. The parser rejects malformed values and invalid operational ranges fail startup.

This is an allowlist, not a blanket environment override of all 716 typed fields. Analytical cutoffs, learned/statistical thresholds, broker-derived facts, destructive data-purge switches and immutable safety controls are not operator overrides.

| Environment variable | Config field | Type | Default |
|---|---|---|---|
| APEX_performance_candle_cache_enabled | PerformanceConfig.candle_cache_enabled | bool | True |
| APEX_performance_candle_cache_ttl | PerformanceConfig.candle_cache_ttl | dict[str, float] | field( |
| APEX_performance_candle_cache_default_ttl | PerformanceConfig.candle_cache_default_ttl | float | 5.0  # unknown timeframes — conservative |
| APEX_performance_m1_cache_ttl | PerformanceConfig.m1_cache_ttl | float | 1.0 |
| APEX_performance_scan_interval_active | PerformanceConfig.scan_interval_active | int | 5 |
| APEX_performance_scan_interval_with_positions | PerformanceConfig.scan_interval_with_positions | int | 5 |
| APEX_performance_parallel_scan_enabled | PerformanceConfig.parallel_scan_enabled | bool | True |
| APEX_performance_parallel_scan_max_workers | PerformanceConfig.parallel_scan_max_workers | int | 10 |
| APEX_performance_account_info_cache_enabled | PerformanceConfig.account_info_cache_enabled | bool | True |
| APEX_ops_enabled | OpsConfig.enabled | bool | True |
| APEX_ops_shutdown_timeout_seconds | OpsConfig.shutdown_timeout_seconds | float | 30.0 |
| APEX_ops_crash_marker_path | OpsConfig.crash_marker_path | str | "data/.crash_marker" |
| APEX_ops_heartbeat_interval_seconds | OpsConfig.heartbeat_interval_seconds | float | 10.0 |
| APEX_ops_heartbeat_file | OpsConfig.heartbeat_file | str | "data/.heartbeat" |
| APEX_ops_max_tick_duration_seconds | OpsConfig.max_tick_duration_seconds | float | 60.0 |
| APEX_ops_tick_profiling_enabled | OpsConfig.tick_profiling_enabled | bool | True |
| APEX_ops_slow_tick_threshold_ms | OpsConfig.slow_tick_threshold_ms | float | 100.0 |
| APEX_ops_profiling_window_size | OpsConfig.profiling_window_size | int | 1000 |
| APEX_ops_log_format | OpsConfig.log_format | str | "json"          # "json" or "text" |
| APEX_ops_log_level | OpsConfig.log_level | str | "INFO" |
| APEX_ops_log_max_size_mb | OpsConfig.log_max_size_mb | int | 50 |
| APEX_ops_log_max_files | OpsConfig.log_max_files | int | 10 |
| APEX_ops_main_log | OpsConfig.main_log | str | "logs/apex.log" |
| APEX_ops_trade_audit_log | OpsConfig.trade_audit_log | str | "logs/trade_audit.log" |
| APEX_ops_risk_audit_log | OpsConfig.risk_audit_log | str | "logs/risk_audit.log" |
| APEX_ops_reconnect_max_retries | OpsConfig.reconnect_max_retries | int | 10 |
| APEX_ops_reconnect_base_delay_seconds | OpsConfig.reconnect_base_delay_seconds | float | 5.0 |
| APEX_data_backup_enabled | DataBackupConfig.enabled | bool | True |
| APEX_data_backup_interval_hours | DataBackupConfig.interval_hours | int | 1 |
| APEX_data_backup_max_file_size_mb | DataBackupConfig.max_file_size_mb | float | 95.0 |
| APEX_data_backup_exclude_patterns | DataBackupConfig.exclude_patterns | list[str] | field(default_factory=lambda: ["*.csv"]) |
| APEX_data_backup_auto_sync_data_repo | DataBackupConfig.auto_sync_data_repo | bool | True |
| APEX_data_backup_sync_branch | DataBackupConfig.sync_branch | str | "main" |
| APEX_data_backup_data_repo_url | DataBackupConfig.data_repo_url | str | "https://github.com/Eugine336/apex-trader-data.git" |
| APEX_data_backup_sync_orphan_branch | DataBackupConfig.sync_orphan_branch | bool | True |
| APEX_backtest_enabled | BacktestConfig.enabled | bool | True |
| APEX_backtest_default_spread_pips | BacktestConfig.default_spread_pips | float | 2.0 |
| APEX_backtest_default_slippage_pips | BacktestConfig.default_slippage_pips | float | 0.5 |
| APEX_backtest_use_memory_db | BacktestConfig.use_memory_db | bool | True |
| APEX_backtest_progress_interval | BacktestConfig.progress_interval | int | 100 |
| APEX_backtest_synthetic_seed | BacktestConfig.synthetic_seed | int | 42 |
| APEX_backtest_starting_balance | BacktestConfig.starting_balance | float | 10_000.0 |
| APEX_decision_trace_enabled | DecisionTraceConfig.enabled | bool | True |
| APEX_outcome_feedback_enabled | OutcomeFeedbackConfig.enabled | bool | True |
| APEX_outcome_feedback_journal_path | OutcomeFeedbackConfig.journal_path | str | "data/outcome_feedback.jsonl" |
| APEX_outcome_feedback_accuracy_lookback | OutcomeFeedbackConfig.accuracy_lookback | int | 300 |
| APEX_outcome_feedback_gold_specialist | OutcomeFeedbackConfig.gold_specialist | bool | False |
| APEX_signal_ledger_signal_ledger_enabled | SignalLedgerConfig.signal_ledger_enabled | bool | True |
| APEX_signal_ledger_signal_grading_enabled | SignalLedgerConfig.signal_grading_enabled | bool | True |
| APEX_signal_ledger_signal_grading_delay_minutes | SignalLedgerConfig.signal_grading_delay_minutes | int | 30 |
| APEX_signal_ledger_signal_grading_check_intervals | SignalLedgerConfig.signal_grading_check_intervals | list[int] | field( |
| APEX_signal_ledger_emitter_feedback_enabled | SignalLedgerConfig.emitter_feedback_enabled | bool | True |
| APEX_signal_ledger_signal_ledger_db_path | SignalLedgerConfig.signal_ledger_db_path | str | "data/signal_ledger.db" |
| APEX_signal_ledger_accuracy_lookback | SignalLedgerConfig.accuracy_lookback | int | 100 |
| APEX_post_close_tracker_enabled | PostCloseTrackerConfig.enabled | bool | True |
| APEX_post_close_tracker_db_path | PostCloseTrackerConfig.db_path | str | "data/trade_journal.db" |
| APEX_post_close_tracker_check_intervals_minutes | PostCloseTrackerConfig.check_intervals_minutes | list[int] | field( |
| APEX_post_close_tracker_max_retries | PostCloseTrackerConfig.max_retries | int | 3 |
| APEX_tuner_agent_enabled | TunerAgentConfig.enabled | bool | True |
| APEX_tuner_agent_max_tune_duration_seconds | TunerAgentConfig.max_tune_duration_seconds | float | 30.0 |
| APEX_tuner_agent_max_consecutive_failures | TunerAgentConfig.max_consecutive_failures | int | 3 |
| APEX_tuner_agent_audit_db_path | TunerAgentConfig.audit_db_path | str | "data/tuner_audit.db" |
| APEX_tuner_agent_log_all_skips | TunerAgentConfig.log_all_skips | bool | False |
| APEX_counterfactual_counterfactual_enabled | CounterfactualConfig.counterfactual_enabled | bool | True |
| APEX_counterfactual_attribution_lookback | CounterfactualConfig.attribution_lookback | int | 500 |
| APEX_counterfactual_attribution_interval | CounterfactualConfig.attribution_interval | int | 100 |
| APEX_counterfactual_min_trades_for_attribution | CounterfactualConfig.min_trades_for_attribution | int | 50 |
| APEX_counterfactual_counterfactual_db_path | CounterfactualConfig.counterfactual_db_path | str | "data/counterfactual.db" |
| APEX_interaction_interaction_discovery_enabled | InteractionConfig.interaction_discovery_enabled | bool | True |
| APEX_interaction_interaction_lookback | InteractionConfig.interaction_lookback | int | 500 |
| APEX_interaction_interaction_interval | InteractionConfig.interaction_interval | int | 500 |
| APEX_interaction_exhaustive_search_max_modules | InteractionConfig.exhaustive_search_max_modules | int | 12 |
| APEX_interaction_interaction_db_path | InteractionConfig.interaction_db_path | str | "data/interaction_discovery.db" |
| APEX_calibration_enabled | CalibrationConfig.enabled | bool | True |
| APEX_calibration_state_path | CalibrationConfig.state_path | str | "data/calibration_state.json" |
| APEX_calibration_spread_sample_tf | CalibrationConfig.spread_sample_tf | str | "M5" |
| APEX_calibration_persist | CalibrationConfig.persist | bool | True |
| APEX_developing_analysis_enabled | DevelopingAnalysisConfig.enabled | bool | True |
| APEX_developing_analysis_max_workers | DevelopingAnalysisConfig.max_workers | int | 4 |
| APEX_developing_analysis_refresh_m5 | DevelopingAnalysisConfig.refresh_m5 | float | 10.0 |
| APEX_developing_analysis_refresh_m15 | DevelopingAnalysisConfig.refresh_m15 | float | 15.0 |
| APEX_developing_analysis_refresh_h1 | DevelopingAnalysisConfig.refresh_h1 | float | 30.0 |
| APEX_developing_analysis_refresh_h4 | DevelopingAnalysisConfig.refresh_h4 | float | 60.0 |
| APEX_developing_analysis_refresh_d1 | DevelopingAnalysisConfig.refresh_d1 | float | 300.0 |
| APEX_developing_analysis_management_advisory_enabled | DevelopingAnalysisConfig.management_advisory_enabled | bool | True |
| APEX_campaign_enabled | CampaignConfig.enabled | bool | True |
| APEX_campaign_dormant_after_seconds | CampaignConfig.dormant_after_seconds | float | 900.0 |
| APEX_campaign_invalidate_after_seconds | CampaignConfig.invalidate_after_seconds | float | 3600.0 |
| APEX_campaign_history_limit | CampaignConfig.history_limit | int | 500 |
| APEX_campaign_postmortem_enabled | CampaignConfig.postmortem_enabled | bool | True |
| APEX_campaign_evidence_full_refreshes | CampaignConfig.evidence_full_refreshes | int | 5 |
| APEX_adaptive_tuner_enabled | AdaptiveTunerConfig.enabled | bool | True |
| APEX_adaptive_tuner_periodic_interval_seconds | AdaptiveTunerConfig.periodic_interval_seconds | float | 900.0   # 15 min — drives on_periodic_tick |
| APEX_adaptive_tuner_scan_interval_seconds | AdaptiveTunerConfig.scan_interval_seconds | float | 3600.0      # 1 hr  — drives on_scan_cycle |
| APEX_cross_instrument_queue_enabled | CrossInstrumentConfig.queue_enabled | bool | True |
| APEX_cross_instrument_queue_window_ms | CrossInstrumentConfig.queue_window_ms | int | 1000          # collection window before a drain |
| APEX_cross_instrument_ranking_enabled | CrossInstrumentConfig.ranking_enabled | bool | True |
| APEX_cross_instrument_quality_sizing_enabled | CrossInstrumentConfig.quality_sizing_enabled | bool | True |
| APEX_cross_instrument_displacement_enabled | CrossInstrumentConfig.displacement_enabled | bool | True |
| APEX_cross_instrument_proactive_scan_enabled | CrossInstrumentConfig.proactive_scan_enabled | bool | True |
| APEX_cross_instrument_proactive_scan_interval_seconds | CrossInstrumentConfig.proactive_scan_interval_seconds | float | 60.0 |

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
