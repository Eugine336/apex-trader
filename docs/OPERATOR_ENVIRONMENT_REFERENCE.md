# Operator environment configuration reference

Generated from config_env.py and config.py on branch fix/mt5-automatic-broker-clock.

The root .env.example exposes 101 typed operational settings across 16 sections. Empty assignments preserve typed defaults. The parser rejects malformed values and invalid operational ranges fail startup.

This is an allowlist, not a blanket environment override of all 716 typed fields. Analytical cutoffs, learned/statistical thresholds, broker-derived facts, destructive data-purge switches and immutable safety controls are not operator overrides.

| Environment variable | Config field | Type | Default |
|---|---|---|---|
| APEX_PERFORMANCE_CANDLE_CACHE_ENABLED | PerformanceConfig.candle_cache_enabled | bool | True |
| APEX_PERFORMANCE_CANDLE_CACHE_TTL | PerformanceConfig.candle_cache_ttl | dict[str, float] | factory default (defined in config.py) |
| APEX_PERFORMANCE_CANDLE_CACHE_DEFAULT_TTL | PerformanceConfig.candle_cache_default_ttl | float | 5.0 |
| APEX_PERFORMANCE_M1_CACHE_TTL | PerformanceConfig.m1_cache_ttl | float | 1.0 |
| APEX_PERFORMANCE_SCAN_INTERVAL_ACTIVE | PerformanceConfig.scan_interval_active | int | 5 |
| APEX_PERFORMANCE_SCAN_INTERVAL_WITH_POSITIONS | PerformanceConfig.scan_interval_with_positions | int | 5 |
| APEX_PERFORMANCE_PARALLEL_SCAN_ENABLED | PerformanceConfig.parallel_scan_enabled | bool | True |
| APEX_PERFORMANCE_PARALLEL_SCAN_MAX_WORKERS | PerformanceConfig.parallel_scan_max_workers | int | 10 |
| APEX_PERFORMANCE_ACCOUNT_INFO_CACHE_ENABLED | PerformanceConfig.account_info_cache_enabled | bool | True |
| APEX_OPS_ENABLED | OpsConfig.enabled | bool | True |
| APEX_OPS_SHUTDOWN_TIMEOUT_SECONDS | OpsConfig.shutdown_timeout_seconds | float | 30.0 |
| APEX_OPS_CRASH_MARKER_PATH | OpsConfig.crash_marker_path | str | "data/.crash_marker" |
| APEX_OPS_HEARTBEAT_INTERVAL_SECONDS | OpsConfig.heartbeat_interval_seconds | float | 10.0 |
| APEX_OPS_HEARTBEAT_FILE | OpsConfig.heartbeat_file | str | "data/.heartbeat" |
| APEX_OPS_MAX_TICK_DURATION_SECONDS | OpsConfig.max_tick_duration_seconds | float | 60.0 |
| APEX_OPS_TICK_PROFILING_ENABLED | OpsConfig.tick_profiling_enabled | bool | True |
| APEX_OPS_SLOW_TICK_THRESHOLD_MS | OpsConfig.slow_tick_threshold_ms | float | 100.0 |
| APEX_OPS_PROFILING_WINDOW_SIZE | OpsConfig.profiling_window_size | int | 1000 |
| APEX_OPS_LOG_FORMAT | OpsConfig.log_format | str | "json" |
| APEX_OPS_LOG_LEVEL | OpsConfig.log_level | str | "INFO" |
| APEX_OPS_LOG_MAX_SIZE_MB | OpsConfig.log_max_size_mb | int | 50 |
| APEX_OPS_LOG_MAX_FILES | OpsConfig.log_max_files | int | 10 |
| APEX_OPS_MAIN_LOG | OpsConfig.main_log | str | "logs/apex.log" |
| APEX_OPS_TRADE_AUDIT_LOG | OpsConfig.trade_audit_log | str | "logs/trade_audit.log" |
| APEX_OPS_RISK_AUDIT_LOG | OpsConfig.risk_audit_log | str | "logs/risk_audit.log" |
| APEX_OPS_RECONNECT_MAX_RETRIES | OpsConfig.reconnect_max_retries | int | 10 |
| APEX_OPS_RECONNECT_BASE_DELAY_SECONDS | OpsConfig.reconnect_base_delay_seconds | float | 5.0 |
| APEX_DATA_BACKUP_ENABLED | DataBackupConfig.enabled | bool | True |
| APEX_DATA_BACKUP_INTERVAL_HOURS | DataBackupConfig.interval_hours | int | 1 |
| APEX_DATA_BACKUP_MAX_FILE_SIZE_MB | DataBackupConfig.max_file_size_mb | float | 95.0 |
| APEX_DATA_BACKUP_EXCLUDE_PATTERNS | DataBackupConfig.exclude_patterns | list[str] | factory default (defined in config.py) |
| APEX_DATA_BACKUP_AUTO_SYNC_DATA_REPO | DataBackupConfig.auto_sync_data_repo | bool | True |
| APEX_DATA_BACKUP_SYNC_BRANCH | DataBackupConfig.sync_branch | str | "main" |
| APEX_DATA_BACKUP_DATA_REPO_URL | DataBackupConfig.data_repo_url | str | "https://github.com/Eugine336/apex-trader-data.git" |
| APEX_DATA_BACKUP_SYNC_ORPHAN_BRANCH | DataBackupConfig.sync_orphan_branch | bool | True |
| APEX_BACKTEST_ENABLED | BacktestConfig.enabled | bool | True |
| APEX_BACKTEST_DEFAULT_SPREAD_PIPS | BacktestConfig.default_spread_pips | float | 2.0 |
| APEX_BACKTEST_DEFAULT_SLIPPAGE_PIPS | BacktestConfig.default_slippage_pips | float | 0.5 |
| APEX_BACKTEST_USE_MEMORY_DB | BacktestConfig.use_memory_db | bool | True |
| APEX_BACKTEST_PROGRESS_INTERVAL | BacktestConfig.progress_interval | int | 100 |
| APEX_BACKTEST_SYNTHETIC_SEED | BacktestConfig.synthetic_seed | int | 42 |
| APEX_BACKTEST_STARTING_BALANCE | BacktestConfig.starting_balance | float | 10_000.0 |
| APEX_DECISION_TRACE_ENABLED | DecisionTraceConfig.enabled | bool | True |
| APEX_OUTCOME_FEEDBACK_ENABLED | OutcomeFeedbackConfig.enabled | bool | True |
| APEX_OUTCOME_FEEDBACK_JOURNAL_PATH | OutcomeFeedbackConfig.journal_path | str | "data/outcome_feedback.jsonl" |
| APEX_OUTCOME_FEEDBACK_ACCURACY_LOOKBACK | OutcomeFeedbackConfig.accuracy_lookback | int | 300 |
| APEX_OUTCOME_FEEDBACK_GOLD_SPECIALIST | OutcomeFeedbackConfig.gold_specialist | bool | False |
| APEX_SIGNAL_LEDGER_SIGNAL_LEDGER_ENABLED | SignalLedgerConfig.signal_ledger_enabled | bool | True |
| APEX_SIGNAL_LEDGER_SIGNAL_GRADING_ENABLED | SignalLedgerConfig.signal_grading_enabled | bool | True |
| APEX_SIGNAL_LEDGER_SIGNAL_GRADING_DELAY_MINUTES | SignalLedgerConfig.signal_grading_delay_minutes | int | 30 |
| APEX_SIGNAL_LEDGER_SIGNAL_GRADING_CHECK_INTERVALS | SignalLedgerConfig.signal_grading_check_intervals | list[int] | factory default (defined in config.py) |
| APEX_SIGNAL_LEDGER_EMITTER_FEEDBACK_ENABLED | SignalLedgerConfig.emitter_feedback_enabled | bool | True |
| APEX_SIGNAL_LEDGER_SIGNAL_LEDGER_DB_PATH | SignalLedgerConfig.signal_ledger_db_path | str | "data/signal_ledger.db" |
| APEX_SIGNAL_LEDGER_ACCURACY_LOOKBACK | SignalLedgerConfig.accuracy_lookback | int | 100 |
| APEX_POST_CLOSE_TRACKER_ENABLED | PostCloseTrackerConfig.enabled | bool | True |
| APEX_POST_CLOSE_TRACKER_DB_PATH | PostCloseTrackerConfig.db_path | str | "data/trade_journal.db" |
| APEX_POST_CLOSE_TRACKER_CHECK_INTERVALS_MINUTES | PostCloseTrackerConfig.check_intervals_minutes | list[int] | factory default (defined in config.py) |
| APEX_POST_CLOSE_TRACKER_MAX_RETRIES | PostCloseTrackerConfig.max_retries | int | 3 |
| APEX_TUNER_AGENT_ENABLED | TunerAgentConfig.enabled | bool | True |
| APEX_TUNER_AGENT_MAX_TUNE_DURATION_SECONDS | TunerAgentConfig.max_tune_duration_seconds | float | 30.0 |
| APEX_TUNER_AGENT_MAX_CONSECUTIVE_FAILURES | TunerAgentConfig.max_consecutive_failures | int | 3 |
| APEX_TUNER_AGENT_AUDIT_DB_PATH | TunerAgentConfig.audit_db_path | str | "data/tuner_audit.db" |
| APEX_TUNER_AGENT_LOG_ALL_SKIPS | TunerAgentConfig.log_all_skips | bool | False |
| APEX_COUNTERFACTUAL_COUNTERFACTUAL_ENABLED | CounterfactualConfig.counterfactual_enabled | bool | True |
| APEX_COUNTERFACTUAL_ATTRIBUTION_LOOKBACK | CounterfactualConfig.attribution_lookback | int | 500 |
| APEX_COUNTERFACTUAL_ATTRIBUTION_INTERVAL | CounterfactualConfig.attribution_interval | int | 100 |
| APEX_COUNTERFACTUAL_MIN_TRADES_FOR_ATTRIBUTION | CounterfactualConfig.min_trades_for_attribution | int | 50 |
| APEX_COUNTERFACTUAL_COUNTERFACTUAL_DB_PATH | CounterfactualConfig.counterfactual_db_path | str | "data/counterfactual.db" |
| APEX_INTERACTION_INTERACTION_DISCOVERY_ENABLED | InteractionConfig.interaction_discovery_enabled | bool | True |
| APEX_INTERACTION_INTERACTION_LOOKBACK | InteractionConfig.interaction_lookback | int | 500 |
| APEX_INTERACTION_INTERACTION_INTERVAL | InteractionConfig.interaction_interval | int | 500 |
| APEX_INTERACTION_EXHAUSTIVE_SEARCH_MAX_MODULES | InteractionConfig.exhaustive_search_max_modules | int | 12 |
| APEX_INTERACTION_INTERACTION_DB_PATH | InteractionConfig.interaction_db_path | str | "data/interaction_discovery.db" |
| APEX_CALIBRATION_ENABLED | CalibrationConfig.enabled | bool | True |
| APEX_CALIBRATION_STATE_PATH | CalibrationConfig.state_path | str | "data/calibration_state.json" |
| APEX_CALIBRATION_SPREAD_SAMPLE_TF | CalibrationConfig.spread_sample_tf | str | "M5" |
| APEX_CALIBRATION_PERSIST | CalibrationConfig.persist | bool | True |
| APEX_DEVELOPING_ANALYSIS_ENABLED | DevelopingAnalysisConfig.enabled | bool | True |
| APEX_DEVELOPING_ANALYSIS_MAX_WORKERS | DevelopingAnalysisConfig.max_workers | int | 4 |
| APEX_DEVELOPING_ANALYSIS_REFRESH_M5 | DevelopingAnalysisConfig.refresh_m5 | float | 10.0 |
| APEX_DEVELOPING_ANALYSIS_REFRESH_M15 | DevelopingAnalysisConfig.refresh_m15 | float | 15.0 |
| APEX_DEVELOPING_ANALYSIS_REFRESH_H1 | DevelopingAnalysisConfig.refresh_h1 | float | 30.0 |
| APEX_DEVELOPING_ANALYSIS_REFRESH_H4 | DevelopingAnalysisConfig.refresh_h4 | float | 60.0 |
| APEX_DEVELOPING_ANALYSIS_REFRESH_D1 | DevelopingAnalysisConfig.refresh_d1 | float | 300.0 |
| APEX_DEVELOPING_ANALYSIS_MANAGEMENT_ADVISORY_ENABLED | DevelopingAnalysisConfig.management_advisory_enabled | bool | True |
| APEX_CAMPAIGN_ENABLED | CampaignConfig.enabled | bool | True |
| APEX_CAMPAIGN_DORMANT_AFTER_SECONDS | CampaignConfig.dormant_after_seconds | float | 900.0 |
| APEX_CAMPAIGN_INVALIDATE_AFTER_SECONDS | CampaignConfig.invalidate_after_seconds | float | 3600.0 |
| APEX_CAMPAIGN_HISTORY_LIMIT | CampaignConfig.history_limit | int | 500 |
| APEX_CAMPAIGN_POSTMORTEM_ENABLED | CampaignConfig.postmortem_enabled | bool | True |
| APEX_CAMPAIGN_EVIDENCE_FULL_REFRESHES | CampaignConfig.evidence_full_refreshes | int | 5 |
| APEX_ADAPTIVE_TUNER_ENABLED | AdaptiveTunerConfig.enabled | bool | True |
| APEX_ADAPTIVE_TUNER_PERIODIC_INTERVAL_SECONDS | AdaptiveTunerConfig.periodic_interval_seconds | float | 900.0 |
| APEX_ADAPTIVE_TUNER_SCAN_INTERVAL_SECONDS | AdaptiveTunerConfig.scan_interval_seconds | float | 3600.0 |
| APEX_CROSS_INSTRUMENT_PROACTIVE_SCAN_ENABLED | CrossInstrumentConfig.proactive_scan_enabled | bool | True |
| APEX_CROSS_INSTRUMENT_PROACTIVE_SCAN_INTERVAL_SECONDS | CrossInstrumentConfig.proactive_scan_interval_seconds | float | 60.0 |
| APEX_CROSS_INSTRUMENT_PROACTIVE_SCAN_MIN_EV | CrossInstrumentConfig.proactive_scan_min_ev | float | 0.5 |

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

## Complete environment-variable inventory

This inventory covers the root server environment and both Vite frontends. Secret values are never reproduced. The root server template has 293 active keys; frontend templates are separate because Vite values are public build-time inputs.

### .env.example

| Variable | Section / purpose | Example value |
|---|---|---|
| APEX_LOG_LEVEL | All literal runtime environment reads are covered by tests/test_env_example_coverage.py. ============================================================================= 1. Runtime mo | INFO |
| APEX_OWNER_LIMITS_JSON | Optional bootstrap overrides for the canonical owner-editable capital limits. Only fields accepted by OwnerLimits.apply_updates are allowed; internal hard stops and immutable safet | (blank; typed default or unset) |
| USE_EVENT_DRIVEN | ============================================================================= | true |
| TRADING_MODE | Trading universe: gold (XAUUSD only), full (all configured categories), selected (TRADING_SYMBOLS list), or single (exactly one TRADING_SYMBOLS entry). | gold |
| TRADING_MODE_LOCKED | true makes the process-level universe override persisted per-user universe preferences. | true |
| TRADING_ENABLED | 1. Runtime mode and paths | false |
| TRADING_SYMBOLS | 1. Runtime mode and paths | (blank; typed default or unset) |
| TRADING_ENABLED_CATEGORIES | Comma-separated categories used only with TRADING_MODE=full. | forex,commodity,index,synthetic,crypto |
| MAX_TICK_AGE_SECONDS | 1. Runtime mode and paths | 120 |
| APEX_MARKET_CLOSED_TICK_AGE_SECONDS | 1. Runtime mode and paths | 900 |
| APEX_DATA_DIR | Mutable trading state must live OUTSIDE the source-code Git work tree or in the correctly provisioned data-repository junction. This relative path is resolved from the process work | ../apex-trader-data |
| APEX_LOG_DIR | 1. Runtime mode and paths | (blank; typed default or unset) |
| APEX_REPO_DIR | 1. Runtime mode and paths | (blank; typed default or unset) |
| APEX_USER_ID | 1. Runtime mode and paths | (blank; typed default or unset) |
| APEX_USER_CONFIG | 1. Runtime mode and paths | (blank; typed default or unset) |
| APEX_TRADE_REPORT_DB | 1. Runtime mode and paths | (blank; typed default or unset) |
| APEX_ED_POSEVAL_PARALLEL | Event-driven execution performance and management scheduling. Set either to 0 only as an operational rollback switch. | 1 |
| APEX_ED_EVENT_REACTIVE_MGMT | 1. Runtime mode and paths | 1 |
| APEX_GOLD_DESK_SHADOW_ENABLED | Read-only Gold Desk observer. It publishes evidence only; it has no independent order-submission path. AUTO resolves the venue per symbol. | 1 |
| APEX_GOLD_DESK_VENUE | 1. Runtime mode and paths | AUTO |
| MT5_BROKERS | ============================================================================= Multi-broker MT5 JSON takes priority over the legacy single-account fields. Example (replace every val | (blank; typed default or unset) |
| MT5_LOGIN | 2. Broker credentials and account selection | (blank; typed default or unset) |
| MT5_PASSWORD | 2. Broker credentials and account selection | (blank secret) |
| MT5_SERVER | 2. Broker credentials and account selection | JustMarkets-Demo |
| MT5_TERMINAL_PATH | Optional exact executable path to pin Python to an installed MT5 terminal. Leave blank for the package automatic terminal discovery. | (blank; typed default or unset) |
| DERIV_CLIENT_ID | 2. Broker credentials and account selection | (blank; typed default or unset) |
| DERIV_ACCESS_TOKEN | 2. Broker credentials and account selection | (blank secret) |
| DERIV_APP_ID | 2. Broker credentials and account selection | (blank; typed default or unset) |
| DERIV_ACCOUNT_TYPE | Set to "real" for the requested live account; the OAuth token must be authorized for a real account. Use "demo" only when intentionally testing. | real |
| DERIV_ACCESS_TOKEN_FILE | Optional mounted token file for rotation. If set and readable, refresh uses it. | (blank secret) |
| DERIV_TOKEN_EXPIRES_IN | Access-token lifetime in seconds. Blank or invalid values also fall back safely to 3600. | (redacted secret placeholder) |
| COGNITION_ENABLED | ============================================================================= 3. Identity, cognition, and live execution mode ====================================================== | true |
| COGNITION_ORIGINATION_MODE | 3. Identity, cognition, and live execution mode | live |
| COGNITION_MANAGEMENT_MODE | 3. Identity, cognition, and live execution mode | live |
| COGNITION_MEMORY_ENABLED | 3. Identity, cognition, and live execution mode | true |
| COGNITION_OPERATIONS_ENABLED | 3. Identity, cognition, and live execution mode | false |
| COGNITION_INFLUENCE_ENABLED | 3. Identity, cognition, and live execution mode | false |
| COGNITION_INFLUENCE_WEIGHTING_ENABLED | 3. Identity, cognition, and live execution mode | false |
| COGNITION_EV_PRIMARY_GATE | Identity invariants: Brain is the sole market decision authority and positive net-EV qualification is mandatory. There is no universal evidence- domain count floor; candidate-speci | true |
| COGNITION_EV_ACTION_THRESHOLD_R | 3. Identity, cognition, and live execution mode | 0.0 |
| COGNITION_OPPORTUNITY_QUALIFICATION_ENABLED | 3. Identity, cognition, and live execution mode | true |
| COGNITION_MIN_ADVISORS_FOR_ACTION | 3. Identity, cognition, and live execution mode | 1 |
| COGNITION_PER_MODULE_EVIDENCE | 3. Identity, cognition, and live execution mode | true |
| COGNITION_EMIT_OPERATOR_NOTIFICATIONS | 3. Identity, cognition, and live execution mode | true |
| COGNITION_LOOP_INTERVAL_SECONDS | Loop cadence and live opportunity/management settings. | 30 |
| COGNITION_MAX_SYMBOLS_PER_CYCLE | 3. Identity, cognition, and live execution mode | 12 |
| COGNITION_EVENT_DRIVEN | 3. Identity, cognition, and live execution mode | true |
| COGNITION_EVENT_MIN_INTERVAL_SECONDS | 3. Identity, cognition, and live execution mode | 1 |
| COGNITION_MANAGE_MIN_INTERVAL_SECONDS | 3. Identity, cognition, and live execution mode | 8 |
| COGNITION_ALLOW_SCALE_IN | 3. Identity, cognition, and live execution mode | false |
| COGNITION_ORIGINATION_RISK_FRACTION | Risk fraction is fractional account risk (0.005 = 0.5%); keep it within the independently enforced Layer-2 per-trade and whole-book risk budget. | 0.005 |
| COGNITION_ORIGINATION_MAX_EXPOSURE | 3. Identity, cognition, and live execution mode | 1.0 |
| COGNITION_ORIGINATION_MAX_LOTS | 3. Identity, cognition, and live execution mode | 0.0 |
| COGNITION_OPPORTUNITY_COST_MULTIPLE | 3. Identity, cognition, and live execution mode | 2.0 |
| COGNITION_OPPORTUNITY_MIN_NET_EV | 3. Identity, cognition, and live execution mode | 0.0 |
| COGNITION_COMMISSION_PER_LOT_ROUND_TRIP | Set the broker's actual round-trip commission per lot in account currency. Zero commission is treated as unknown unless the broker explicitly confirms it. | 0.0 |
| COGNITION_COMMISSION_MODEL_VERIFIED | 3. Identity, cognition, and live execution mode | false |
| COGNITION_FINANCING_PER_LOT_ROUND_TRIP_JSON | Per-symbol round-trip financing estimate per 1 lot in account currency, for the expected holding horizon. Example: {"XAUUSD": 4.5, "EURUSD": 1.2}. Empty/missing symbols fail net-EV | {} |
| COGNITION_FINANCING_MODEL_VERIFIED | 3. Identity, cognition, and live execution mode | false |
| RISK_ALLOW_MIN_LOT_OVER_RISK | Explicitly retain the conservative risk configuration: do not inflate a minimum lot beyond the risk budget and do not silently restore legacy exits. | false |
| LLM_PROVIDER | ============================================================================= 4. Primary LLM adviser and provider resilience ======================================================= | gemini |
| LLM_MODEL | 4. Primary LLM adviser and provider resilience | gemini-3.8-flash |
| LLM_API_KEY | 4. Primary LLM adviser and provider resilience | (blank secret) |
| LLM_BASE_URL | 4. Primary LLM adviser and provider resilience | (blank; typed default or unset) |
| LLM_ENABLED | 4. Primary LLM adviser and provider resilience | true |
| LLM_DRIVE_DECISIONS | 4. Primary LLM adviser and provider resilience | true |
| LLM_TIMEOUT_SECONDS | 4. Primary LLM adviser and provider resilience | 30 |
| LLM_TEMPERATURE | LLM_MAX_TOKENS=1024  # source default; uncomment only if overriding it | 0.2 |
| LLM_MIN_INTERVAL_SECONDS | 4. Primary LLM adviser and provider resilience | 15 |
| LLM_WORKER_INTERVAL_SECONDS | 4. Primary LLM adviser and provider resilience | 60 |
| LLM_MAX_SYMBOLS_PER_CYCLE | 4. Primary LLM adviser and provider resilience | 12 |
| LLM_EXTRA_MODELS | 4. Primary LLM adviser and provider resilience | [{"name":"ollama-deep","provider":"ollama","model":"qwen3.5:14b","tier":3,"classes":["deep"],"context_window":32768,"base_url":"http://localhost:11434/v1"},{"name":"vllm-deep","pro |
| LLM_MODEL_POLICY | 4. Primary LLM adviser and provider resilience | performance |
| LLM_CONSULT_MULTI | 4. Primary LLM adviser and provider resilience | true |
| LLM_CONSULT_MAX_ENGINES | 4. Primary LLM adviser and provider resilience | 2 |
| LLM_CONSULT_MODE | 4. Primary LLM adviser and provider resilience | panel |
| LLM_CIRCUIT_FAILURE_THRESHOLD | 4. Primary LLM adviser and provider resilience | 3 |
| LLM_CIRCUIT_COOLDOWN_SECONDS | 4. Primary LLM adviser and provider resilience | 30 |
| LLM_CIRCUIT_COOLDOWN_MAX_SECONDS | 4. Primary LLM adviser and provider resilience | 300 |
| LLM_LOCAL_MAX_CONCURRENCY | 4. Primary LLM adviser and provider resilience | 2 |
| LLM_REASONING_COMPUTE_CLASS | 4. Primary LLM adviser and provider resilience | deep |
| LLM_RECOVERY_PROBE_SECONDS | 4. Primary LLM adviser and provider resilience | 60 |
| LLM_ACKNOWLEDGE_SINGLE_PROVIDER | 4. Primary LLM adviser and provider resilience | false |
| LIGHTNING_OLLAMA_URL | 4. Primary LLM adviser and provider resilience | (blank; typed default or unset) |
| OPENAI_API_KEY | Optional provider credentials. Supply only the keys for the providers used by LLM_PROVIDER / LLM_EXTRA_MODELS; key aliases are listed for provider compatibility. | (blank secret) |
| AZURE_OPENAI_API_KEY | 4. Primary LLM adviser and provider resilience | (blank secret) |
| ANTHROPIC_API_KEY | 4. Primary LLM adviser and provider resilience | (blank secret) |
| GEMINI_API_KEY | 4. Primary LLM adviser and provider resilience | (blank secret) |
| GOOGLE_API_KEY | 4. Primary LLM adviser and provider resilience | (blank secret) |
| XAI_API_KEY | 4. Primary LLM adviser and provider resilience | (blank secret) |
| GROK_API_KEY | 4. Primary LLM adviser and provider resilience | (blank secret) |
| COHERE_API_KEY | 4. Primary LLM adviser and provider resilience | (blank secret) |
| PERPLEXITY_API_KEY | 4. Primary LLM adviser and provider resilience | (blank secret) |
| PPLX_API_KEY | 4. Primary LLM adviser and provider resilience | (blank secret) |
| GROQ_API_KEY | 4. Primary LLM adviser and provider resilience | (blank secret) |
| MISTRAL_API_KEY | 4. Primary LLM adviser and provider resilience | (blank secret) |
| DEEPSEEK_API_KEY | 4. Primary LLM adviser and provider resilience | (blank secret) |
| OPENROUTER_API_KEY | 4. Primary LLM adviser and provider resilience | (blank secret) |
| TOGETHER_API_KEY | 4. Primary LLM adviser and provider resilience | (blank secret) |
| TOGETHERAI_API_KEY | 4. Primary LLM adviser and provider resilience | (blank secret) |
| FIREWORKS_API_KEY | 4. Primary LLM adviser and provider resilience | (blank secret) |
| DEEPINFRA_API_KEY | 4. Primary LLM adviser and provider resilience | (blank secret) |
| CEREBRAS_API_KEY | 4. Primary LLM adviser and provider resilience | (blank secret) |
| HUGGINGFACE_API_KEY | 4. Primary LLM adviser and provider resilience | (blank secret) |
| HF_TOKEN | 4. Primary LLM adviser and provider resilience | (blank secret) |
| NVIDIA_API_KEY | 4. Primary LLM adviser and provider resilience | (blank secret) |
| NIM_API_KEY | 4. Primary LLM adviser and provider resilience | (blank secret) |
| CLOUDFLARE_API_KEY | 4. Primary LLM adviser and provider resilience | (blank secret) |
| CLOUDFLARE_API_TOKEN | 4. Primary LLM adviser and provider resilience | (blank secret) |
| CLOUDFLARE_ACCOUNT_ID | 4. Primary LLM adviser and provider resilience | (blank; typed default or unset) |
| GMI_API_KEY | 4. Primary LLM adviser and provider resilience | (blank secret) |
| GMI_CLOUD_API_KEY | 4. Primary LLM adviser and provider resilience | (blank secret) |
| MODAL_API_KEY | 4. Primary LLM adviser and provider resilience | (blank secret) |
| MODAL_INFERENCE_KEY | 4. Primary LLM adviser and provider resilience | (blank; typed default or unset) |
| ANYSCALE_API_KEY | 4. Primary LLM adviser and provider resilience | (blank secret) |
| REPLICATE_API_KEY | 4. Primary LLM adviser and provider resilience | (blank secret) |
| REPLICATE_API_TOKEN | 4. Primary LLM adviser and provider resilience | (blank secret) |
| BLUESMINDS_API_KEY | 4. Primary LLM adviser and provider resilience | (blank secret) |
| COMPOSIO_ENABLED | ============================================================================= Set COMPOSIO_API_KEY in the private .env before using live external actions. With COMPOSIO_ENABLED=tru | true |
| COMPOSIO_DRY_RUN | 5. Composio external action / knowledge integrations | false |
| COMPOSIO_API_KEY | 5. Composio external action / knowledge integrations | (blank secret) |
| COMPOSIO_BASE_URL | 5. Composio external action / knowledge integrations | https://backend.composio.dev |
| COMPOSIO_ENTITY_ID | 5. Composio external action / knowledge integrations | default |
| COMPOSIO_API_VERSION | 5. Composio external action / knowledge integrations | v3 |
| COMPOSIO_TRANSPORT | 5. Composio external action / knowledge integrations | mcp |
| COMPOSIO_MCP_URL | 5. Composio external action / knowledge integrations | https://connect.composio.dev/mcp |
| COMPOSIO_MCP_AUTH_HEADER | 5. Composio external action / knowledge integrations | (redacted secret placeholder) |
| COMPOSIO_MCP_ROUTER_TOOL | 5. Composio external action / knowledge integrations | COMPOSIO_MULTI_EXECUTE_TOOL |
| COMPOSIO_AUTO_MAX_RISK | They are optional schema overrides; uncomment only when your Composio endpoint differs. COMPOSIO_MCP_ROUTER_TOOLS_KEY=tools COMPOSIO_MCP_ROUTER_SLUG_KEY=tool_slug COMPOSIO_MCP_ROUT | medium |
| COMPOSIO_MIN_CONFIDENCE | 5. Composio external action / knowledge integrations | 0.2 |
| COMPOSIO_MEDIUM_CONFIDENCE_THRESHOLD | 5. Composio external action / knowledge integrations | 0.7 |
| COMPOSIO_TIMEOUT_SECONDS | 5. Composio external action / knowledge integrations | 30 |
| COMPOSIO_REQUIRE_SOURCE | 5. Composio external action / knowledge integrations | true |
| COMPOSIO_KNOWLEDGE_ENABLED | 5. Composio external action / knowledge integrations | true |
| COMPOSIO_ADVISOR_ENABLED | 5. Composio external action / knowledge integrations | true |
| COMPOSIO_KNOWLEDGE_INTERVAL_SECONDS | 5. Composio external action / knowledge integrations | 300 |
| COMPOSIO_KNOWLEDGE_MAX_ITEMS | 5. Composio external action / knowledge integrations | 5 |
| COMPOSIO_AVAILABLE_PROVIDERS | 5. Composio external action / knowledge integrations | composio_search,alphavantage,coinmarketcal,openrouter,gmail,notion,googledrive,github |
| COMPOSIO_PROVIDER_PREFERENCES | 5. Composio external action / knowledge integrations | knowledge.retrieve=alphavantage,advisor.consult=openrouter,operator.notify=gmail |
| COMPOSIO_KNOWLEDGE_ARG_OVERRIDES | 5. Composio external action / knowledge integrations | (blank; typed default or unset) |
| APEX_API_DATA_DIR | 6. API control plane and instance management ============================================================================= APEX_API_DATA_DIR is the API's own database/secret direct | (blank; typed default or unset) |
| APEX_API_HOST | 6. API control plane and instance management | 127.0.0.1 |
| APEX_API_PORT | 6. API control plane and instance management | 8080 |
| APEX_API_JWT_SECRET | 6. API control plane and instance management | (blank secret) |
| APEX_API_VAULT_KEY | 6. API control plane and instance management | (blank; typed default or unset) |
| APEX_API_ACCESS_TTL_MIN | 6. API control plane and instance management | 30 |
| APEX_API_REFRESH_TTL_DAYS | 6. API control plane and instance management | 7 |
| APEX_API_RESET_TTL_MIN | 6. API control plane and instance management | 30 |
| APEX_API_CORS_ORIGINS | 6. API control plane and instance management | http://localhost:3000,http://127.0.0.1:3000,https://apex-trader.live,https://www.apex-trader.live |
| APEX_API_AUTH_RATE_MAX | 6. API control plane and instance management | (redacted secret placeholder) |
| APEX_API_AUTH_RATE_WINDOW | 6. API control plane and instance management | (redacted secret placeholder) |
| APEX_API_MAX_INSTANCES | 6. API control plane and instance management | 50 |
| APEX_API_STOP_GRACE_SEC | 6. API control plane and instance management | 15 |
| APEX_API_PYTHON | 6. API control plane and instance management | (blank; typed default or unset) |
| APEX_API_DATA_SYNC | 6. API control plane and instance management | true |
| APEX_API_DATA_SYNC_BRANCH | 6. API control plane and instance management | main |
| APEX_API_DATA_SYNC_INTERVAL | 6. API control plane and instance management | 3600 |
| DD_DASHBOARD_BIND_HOST | ============================================================================= 7. Dashboard and Datadog observability =============================================================== | 127.0.0.1 |
| DD_DASHBOARD_PORT | 7. Dashboard and Datadog observability | 8000 |
| DD_DASHBOARD_API_KEY | 7. Dashboard and Datadog observability | (blank secret) |
| DD_DASHBOARD_ALLOWED_ORIGINS | 7. Dashboard and Datadog observability | http://localhost:3000,http://127.0.0.1:3000,http://localhost:5173,http://127.0.0.1:5173 |
| DD_DASHBOARD_TRUST_LOOPBACK | 7. Dashboard and Datadog observability | true |
| DD_API_KEY | 7. Dashboard and Datadog observability | (blank secret) |
| DD_AGENT_HOST | 7. Dashboard and Datadog observability | localhost |
| DD_TRACE_AGENT_PORT | 7. Dashboard and Datadog observability | 8126 |
| DD_ENV | 7. Dashboard and Datadog observability | development |
| DD_SERVICE | 7. Dashboard and Datadog observability | apex-trader |
| DD_LOGS_INJECTION | 7. Dashboard and Datadog observability | true |
| CHATGPT_RELAY_TOKEN | 8. ChatGPT signal relay and optional Binance adapter ============================================================================= This is a separate signal adapter, not the Cognit | (blank secret) |
| CHATGPT_RELAY_HOST | 8. ChatGPT signal relay and optional Binance adapter | 127.0.0.1 |
| CHATGPT_RELAY_PORT | 8. ChatGPT signal relay and optional Binance adapter | 8790 |
| CHATGPT_SIGNAL_TIMEOUT_SECONDS | 8. ChatGPT signal relay and optional Binance adapter | 90 |
| APEX_REPO | 8. ChatGPT signal relay and optional Binance adapter | Eugine336/apex-trader |
| CHATGPT_SIGNAL_BRANCH | 8. ChatGPT signal relay and optional Binance adapter | chatgpt-signal-inbox |
| CHATGPT_SIGNAL_POLL_SECONDS | 8. ChatGPT signal relay and optional Binance adapter | 3 |
| CHATGPT_SIGNAL_WORKTREE | 8. ChatGPT signal relay and optional Binance adapter | .runtime/chatgpt-signal-inbox |
| CHATGPT_ALLOW_DUPLICATES | 8. ChatGPT signal relay and optional Binance adapter | false |
| BINANCE_EXECUTION_MODE | 8. ChatGPT signal relay and optional Binance adapter | PAPER |
| BINANCE_LIVE_EXECUTION_ENABLED | 8. ChatGPT signal relay and optional Binance adapter | false |
| BINANCE_API_KEY | 8. ChatGPT signal relay and optional Binance adapter | (blank secret) |
| BINANCE_API_SECRET | 8. ChatGPT signal relay and optional Binance adapter | (blank secret) |
| BINANCE_MAX_NOTIONAL_USDT | 8. ChatGPT signal relay and optional Binance adapter | 100 |
| BINANCE_FUTURES_BASE_URL | 8. ChatGPT signal relay and optional Binance adapter | https://fapi.binance.com |
| MAX_SIMULTANEOUS_POSITIONS | 8. ChatGPT signal relay and optional Binance adapter | 3 |
| MAX_ACCOUNT_RISK_PERCENT | 8. ChatGPT signal relay and optional Binance adapter | 1.0 |
| APEX_PERFORMANCE_CANDLE_CACHE_ENABLED | ============================================================================= Empty assignments retain the validated dataclass defaults. These are operational/deployment settings o | (blank; typed default or unset) |
| APEX_PERFORMANCE_CANDLE_CACHE_TTL | 10. Typed operator runtime controls | (blank; typed default or unset) |
| APEX_PERFORMANCE_CANDLE_CACHE_DEFAULT_TTL | 10. Typed operator runtime controls | (blank; typed default or unset) |
| APEX_PERFORMANCE_SCAN_INTERVAL_ACTIVE | 10. Typed operator runtime controls | (blank; typed default or unset) |
| APEX_PERFORMANCE_SCAN_INTERVAL_WITH_POSITIONS | 10. Typed operator runtime controls | (blank; typed default or unset) |
| APEX_PERFORMANCE_PARALLEL_SCAN_ENABLED | 10. Typed operator runtime controls | (blank; typed default or unset) |
| APEX_PERFORMANCE_PARALLEL_SCAN_MAX_WORKERS | 10. Typed operator runtime controls | (blank; typed default or unset) |
| APEX_PERFORMANCE_ACCOUNT_INFO_CACHE_ENABLED | 10. Typed operator runtime controls | (blank; typed default or unset) |
| APEX_OPS_ENABLED | 10. Typed operator runtime controls | (blank; typed default or unset) |
| APEX_OPS_SHUTDOWN_TIMEOUT_SECONDS | 10. Typed operator runtime controls | (blank; typed default or unset) |
| APEX_OPS_CRASH_MARKER_PATH | 10. Typed operator runtime controls | (blank; typed default or unset) |
| APEX_OPS_HEARTBEAT_INTERVAL_SECONDS | 10. Typed operator runtime controls | (blank; typed default or unset) |
| APEX_OPS_HEARTBEAT_FILE | 10. Typed operator runtime controls | (blank; typed default or unset) |
| APEX_OPS_MAX_TICK_DURATION_SECONDS | 10. Typed operator runtime controls | (blank; typed default or unset) |
| APEX_OPS_TICK_PROFILING_ENABLED | 10. Typed operator runtime controls | (blank; typed default or unset) |
| APEX_OPS_SLOW_TICK_THRESHOLD_MS | 10. Typed operator runtime controls | (blank; typed default or unset) |
| APEX_OPS_PROFILING_WINDOW_SIZE | 10. Typed operator runtime controls | (blank; typed default or unset) |
| APEX_OPS_LOG_FORMAT | 10. Typed operator runtime controls | (blank; typed default or unset) |
| APEX_OPS_LOG_LEVEL | 10. Typed operator runtime controls | (blank; typed default or unset) |
| APEX_OPS_LOG_MAX_SIZE_MB | 10. Typed operator runtime controls | (blank; typed default or unset) |
| APEX_OPS_LOG_MAX_FILES | 10. Typed operator runtime controls | (blank; typed default or unset) |
| APEX_OPS_MAIN_LOG | 10. Typed operator runtime controls | (blank; typed default or unset) |
| APEX_OPS_TRADE_AUDIT_LOG | 10. Typed operator runtime controls | (blank; typed default or unset) |
| APEX_OPS_RISK_AUDIT_LOG | 10. Typed operator runtime controls | (blank; typed default or unset) |
| APEX_OPS_RECONNECT_MAX_RETRIES | 10. Typed operator runtime controls | (blank; typed default or unset) |
| APEX_OPS_RECONNECT_BASE_DELAY_SECONDS | 10. Typed operator runtime controls | (blank; typed default or unset) |
| APEX_DATA_BACKUP_ENABLED | 10. Typed operator runtime controls | (blank; typed default or unset) |
| APEX_DATA_BACKUP_INTERVAL_HOURS | 10. Typed operator runtime controls | (blank; typed default or unset) |
| APEX_DATA_BACKUP_MAX_FILE_SIZE_MB | 10. Typed operator runtime controls | (blank; typed default or unset) |
| APEX_DATA_BACKUP_EXCLUDE_PATTERNS | 10. Typed operator runtime controls | (blank; typed default or unset) |
| APEX_DATA_BACKUP_AUTO_SYNC_DATA_REPO | 10. Typed operator runtime controls | (blank; typed default or unset) |
| APEX_DATA_BACKUP_SYNC_BRANCH | 10. Typed operator runtime controls | (blank; typed default or unset) |
| APEX_DATA_BACKUP_DATA_REPO_URL | 10. Typed operator runtime controls | (blank; typed default or unset) |
| APEX_DATA_BACKUP_SYNC_ORPHAN_BRANCH | 10. Typed operator runtime controls | (blank; typed default or unset) |
| APEX_BACKTEST_ENABLED | 10. Typed operator runtime controls | (blank; typed default or unset) |
| APEX_BACKTEST_DEFAULT_SPREAD_PIPS | 10. Typed operator runtime controls | (blank; typed default or unset) |
| APEX_BACKTEST_DEFAULT_SLIPPAGE_PIPS | 10. Typed operator runtime controls | (blank; typed default or unset) |
| APEX_BACKTEST_USE_MEMORY_DB | 10. Typed operator runtime controls | (blank; typed default or unset) |
| APEX_BACKTEST_PROGRESS_INTERVAL | 10. Typed operator runtime controls | (blank; typed default or unset) |
| APEX_BACKTEST_SYNTHETIC_SEED | 10. Typed operator runtime controls | (blank; typed default or unset) |
| APEX_BACKTEST_STARTING_BALANCE | 10. Typed operator runtime controls | (blank; typed default or unset) |
| APEX_DECISION_TRACE_ENABLED | 10. Typed operator runtime controls | (blank; typed default or unset) |
| APEX_OUTCOME_FEEDBACK_ENABLED | 10. Typed operator runtime controls | (blank; typed default or unset) |
| APEX_OUTCOME_FEEDBACK_JOURNAL_PATH | 10. Typed operator runtime controls | (blank; typed default or unset) |
| APEX_OUTCOME_FEEDBACK_ACCURACY_LOOKBACK | 10. Typed operator runtime controls | (blank; typed default or unset) |
| APEX_OUTCOME_FEEDBACK_GOLD_SPECIALIST | 10. Typed operator runtime controls | (blank; typed default or unset) |
| APEX_SIGNAL_LEDGER_SIGNAL_LEDGER_ENABLED | 10. Typed operator runtime controls | (blank; typed default or unset) |
| APEX_SIGNAL_LEDGER_SIGNAL_GRADING_ENABLED | 10. Typed operator runtime controls | (blank; typed default or unset) |
| APEX_SIGNAL_LEDGER_SIGNAL_GRADING_DELAY_MINUTES | 10. Typed operator runtime controls | (blank; typed default or unset) |
| APEX_SIGNAL_LEDGER_SIGNAL_GRADING_CHECK_INTERVALS | 10. Typed operator runtime controls | (blank; typed default or unset) |
| APEX_SIGNAL_LEDGER_EMITTER_FEEDBACK_ENABLED | 10. Typed operator runtime controls | (blank; typed default or unset) |
| APEX_SIGNAL_LEDGER_SIGNAL_LEDGER_DB_PATH | 10. Typed operator runtime controls | (blank; typed default or unset) |
| APEX_SIGNAL_LEDGER_ACCURACY_LOOKBACK | 10. Typed operator runtime controls | (blank; typed default or unset) |
| APEX_POST_CLOSE_TRACKER_ENABLED | 10. Typed operator runtime controls | (blank; typed default or unset) |
| APEX_POST_CLOSE_TRACKER_DB_PATH | 10. Typed operator runtime controls | (blank; typed default or unset) |
| APEX_POST_CLOSE_TRACKER_CHECK_INTERVALS_MINUTES | 10. Typed operator runtime controls | (blank; typed default or unset) |
| APEX_POST_CLOSE_TRACKER_MAX_RETRIES | 10. Typed operator runtime controls | (blank; typed default or unset) |
| APEX_TUNER_AGENT_ENABLED | 10. Typed operator runtime controls | (blank; typed default or unset) |
| APEX_TUNER_AGENT_MAX_TUNE_DURATION_SECONDS | 10. Typed operator runtime controls | (blank; typed default or unset) |
| APEX_TUNER_AGENT_MAX_CONSECUTIVE_FAILURES | 10. Typed operator runtime controls | (blank; typed default or unset) |
| APEX_TUNER_AGENT_AUDIT_DB_PATH | 10. Typed operator runtime controls | (blank; typed default or unset) |
| APEX_TUNER_AGENT_LOG_ALL_SKIPS | 10. Typed operator runtime controls | (blank; typed default or unset) |
| APEX_COUNTERFACTUAL_COUNTERFACTUAL_ENABLED | 10. Typed operator runtime controls | (blank; typed default or unset) |
| APEX_COUNTERFACTUAL_ATTRIBUTION_LOOKBACK | 10. Typed operator runtime controls | (blank; typed default or unset) |
| APEX_COUNTERFACTUAL_ATTRIBUTION_INTERVAL | 10. Typed operator runtime controls | (blank; typed default or unset) |
| APEX_COUNTERFACTUAL_MIN_TRADES_FOR_ATTRIBUTION | 10. Typed operator runtime controls | (blank; typed default or unset) |
| APEX_COUNTERFACTUAL_COUNTERFACTUAL_DB_PATH | 10. Typed operator runtime controls | (blank; typed default or unset) |
| APEX_INTERACTION_INTERACTION_DISCOVERY_ENABLED | 10. Typed operator runtime controls | (blank; typed default or unset) |
| APEX_INTERACTION_INTERACTION_LOOKBACK | 10. Typed operator runtime controls | (blank; typed default or unset) |
| APEX_INTERACTION_INTERACTION_INTERVAL | 10. Typed operator runtime controls | (blank; typed default or unset) |
| APEX_INTERACTION_EXHAUSTIVE_SEARCH_MAX_MODULES | 10. Typed operator runtime controls | (blank; typed default or unset) |
| APEX_INTERACTION_INTERACTION_DB_PATH | 10. Typed operator runtime controls | (blank; typed default or unset) |
| APEX_CALIBRATION_ENABLED | 10. Typed operator runtime controls | (blank; typed default or unset) |
| APEX_CALIBRATION_STATE_PATH | 10. Typed operator runtime controls | (blank; typed default or unset) |
| APEX_CALIBRATION_SPREAD_SAMPLE_TF | 10. Typed operator runtime controls | (blank; typed default or unset) |
| APEX_CALIBRATION_PERSIST | 10. Typed operator runtime controls | (blank; typed default or unset) |
| APEX_DEVELOPING_ANALYSIS_ENABLED | 10. Typed operator runtime controls | (blank; typed default or unset) |
| APEX_DEVELOPING_ANALYSIS_MAX_WORKERS | 10. Typed operator runtime controls | (blank; typed default or unset) |
| APEX_DEVELOPING_ANALYSIS_MANAGEMENT_ADVISORY_ENABLED | 10. Typed operator runtime controls | (blank; typed default or unset) |
| APEX_CAMPAIGN_ENABLED | 10. Typed operator runtime controls | (blank; typed default or unset) |
| APEX_CAMPAIGN_DORMANT_AFTER_SECONDS | 10. Typed operator runtime controls | (blank; typed default or unset) |
| APEX_CAMPAIGN_INVALIDATE_AFTER_SECONDS | 10. Typed operator runtime controls | (blank; typed default or unset) |
| APEX_CAMPAIGN_HISTORY_LIMIT | 10. Typed operator runtime controls | (blank; typed default or unset) |
| APEX_CAMPAIGN_POSTMORTEM_ENABLED | 10. Typed operator runtime controls | (blank; typed default or unset) |
| APEX_CAMPAIGN_EVIDENCE_FULL_REFRESHES | 10. Typed operator runtime controls | (blank; typed default or unset) |
| APEX_ADAPTIVE_TUNER_ENABLED | 10. Typed operator runtime controls | (blank; typed default or unset) |
| APEX_ADAPTIVE_TUNER_PERIODIC_INTERVAL_SECONDS | 10. Typed operator runtime controls | (blank; typed default or unset) |
| APEX_ADAPTIVE_TUNER_SCAN_INTERVAL_SECONDS | 10. Typed operator runtime controls | (blank; typed default or unset) |
| APEX_CROSS_INSTRUMENT_PROACTIVE_SCAN_ENABLED | 10. Typed operator runtime controls | (blank; typed default or unset) |
| APEX_CROSS_INSTRUMENT_PROACTIVE_SCAN_INTERVAL_SECONDS | 10. Typed operator runtime controls | (blank; typed default or unset) |
| APEX_CROSS_INSTRUMENT_PROACTIVE_SCAN_MIN_EV | 10. Typed operator runtime controls | (blank; typed default or unset) |
| APEX_PERFORMANCE_M1_CACHE_TTL | Additional allowlisted controls containing numeric field names. | (blank; typed default or unset) |
| APEX_DEVELOPING_ANALYSIS_REFRESH_M5 | 10. Typed operator runtime controls | (blank; typed default or unset) |
| APEX_DEVELOPING_ANALYSIS_REFRESH_M15 | 10. Typed operator runtime controls | (blank; typed default or unset) |
| APEX_DEVELOPING_ANALYSIS_REFRESH_H1 | 10. Typed operator runtime controls | (blank; typed default or unset) |
| APEX_DEVELOPING_ANALYSIS_REFRESH_H4 | 10. Typed operator runtime controls | (blank; typed default or unset) |
| APEX_DEVELOPING_ANALYSIS_REFRESH_D1 | 10. Typed operator runtime controls | (blank; typed default or unset) |

### frontend/.env.example

| Variable | Section / purpose | Example value |
|---|---|---|
| VITE_API_URL | Base URL of the APEX multi-tenant API server. | http://localhost:8080 |

### dashboard/frontend/.env.example

| Variable | Section / purpose | Example value |
|---|---|---|
| VITE_API_URL | Optional. Base URL for the API (leave blank to use the same origin the dashboard is served from). | (blank; typed default or unset) |
| VITE_DASHBOARD_API_KEY | need this — paste the key at runtime on the Controls page instead (stored in the browser, no rebuild). If set, it must MATCH the backend DD_DASHBOARD_API_KEY. NOTE: a value set her | (blank secret) |

Do not move private credentials into VITE_* variables; Vite embeds them in browser bundles. The root .env.example is the server-side configuration source.
