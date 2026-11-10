# Configuration field inventory

Source: `config.py` on branch `fix/brain-probability-and-confidence-governance` at commit `861cd7487fbaa360adfe27407534bc418d2ac39e` plus the three retired cognition controls removed in this change.

This is a complete field-name inventory of the typed configuration dataclasses
declared in `config.py`. It is **not** a declaration that every field should
become an environment variable. Each field must be classified before exposing
it: operator/deployment control, account/owner limit, broker-derived fact,
analytical concept definition, data-quality/statistical safeguard, internal
implementation detail, or non-overridable constitutional safety invariant.

A field becomes an environment control only when its owner, valid range,
default, precedence, restart behavior, audit requirements and safety impact
are defined. Do not expose a setting that can weaken Brain authority, data
integrity, unknown-state handling, owner limits, or execution safety.

The typed classes below contain **713 fields across 43 classes**.
Literal runtime environment reads are separately checked against `.env.example`
by `tests/test_env_example_coverage.py`; frontend Vite reads are checked against
their own frontend templates by `tests/test_frontend_env_example_coverage.py`.

## `ScoringConfig` (19 fields)

- `min_entry_score`
- `watchlist_score`
- `structure_points`
- `order_block_points`
- `fvg_points`
- `mtf_confluence_points`
- `session_points`
- `news_points`
- `currency_strength_points`
- `ranging_score_cap`
- `volatile_score_cap`
- `use_adaptive_scoring_weights`
- `pattern_confluence_bonus`
- `pattern_confluence_max_bonus`
- `per_class_optimizer`
- `min_trades_per_class`
- `class_shrinkage_strength`
- `per_symbol_optimizer`
- `per_symbol_min_trades`

## `ConfirmationPenaltyConfig` (16 fields)

- `enabled`
- `shadow_mode`
- `vwap_wrong_side_penalty`
- `divergence_both_tf_penalty`
- `divergence_single_tf_penalty`
- `atr_dead_regime_penalty`
- `vp_poc_trap_penalty`
- `atr_dead_percentile`
- `atr_percentile_window`
- `rsi_period`
- `macd_fast`
- `macd_slow`
- `macd_signal`
- `vwap_min_session_minutes`
- `vp_poc_lookback`
- `vp_poc_proximity_pct`

## `OpportunityRankerConfig` (26 fields)

- `enabled`
- `execute`
- `scalp_modules`
- `swing_modules`
- `reward_risk`
- `scalp_reward_risk`
- `swing_reward_risk`
- `base_win_rate`
- `confidence_win_rate_gain`
- `min_expected_value`
- `min_cluster_confidence`
- `min_cluster_contributors`
- `max_concurrent`
- `dispatch_top_n`
- `slot_aware_dispatch`
- `dispatch_max_n`
- `rescue_neutral_consensus`
- `scalp_htf_penalty_scale`
- `swing_htf_penalty_scale`
- `mixed_htf_penalty_scale`
- `adaptive_win_rate_provider_enabled`
- `adaptive_win_rate_prior`
- `adaptive_win_rate_prior_strength`
- `adaptive_win_rate_min_trades`
- `adaptive_win_rate_clamp_low`
- `adaptive_win_rate_clamp_high`

## `LayeredDecisionConfig` (8 fields)

- `enabled`
- `opportunity_quality_min`
- `entry_quality_min`
- `ready_min_score`
- `revalidate_opportunity_quality_min`
- `revalidate_entry_quality_min`
- `oq_weights`
- `eq_weights`

## `RiskConfig` (129 fields)

- `risk_per_trade_pct`
- `max_daily_drawdown_pct`
- `max_weekly_drawdown_pct`
- `max_open_trades`
- `max_correlated_trades`
- `min_risk_reward`
- `max_spread_multiplier`
- `ev_threshold`
- `losing_pattern_block_enabled`
- `ev_gate_mode`
- `ev_gate_below_size_mult`
- `losing_pattern_mode`
- `losing_pattern_size_mult`
- `ml_should_trade_mode`
- `ml_should_trade_size_mult`
- `tp_adjust_enabled`
- `pending_max_wait_minutes`
- `max_cluster_same_direction`
- `max_pair_concentration`
- `max_broker_positions`
- `allow_intentional_hedge`
- `margin_guardian_enabled`
- `margin_warn_pct`
- `margin_block_entry_pct`
- `margin_flatten_pct`
- `daily_loss_flatten_enabled`
- `daily_loss_flatten_pct`
- `weekend_protection_enabled`
- `weekend_protection_mode`
- `weekend_close_buffer_minutes`
- `friday_close_hour_utc`
- `sunday_open_buffer_minutes`
- `micro_account_threshold_usd`
- `deriv_min_stake_usd`
- `max_risk_pct_per_trade`
- `allow_min_lot_over_risk`
- `scan_directional_exits_enabled`
- `legacy_mechanical_exit_fallback_enabled`
- `backtest_starting_balance_usd`
- `tp3_ladder_enabled`
- `tp3_r_multiple`
- `tp3_close_ratio`
- `scale_in_enabled`
- `scale_in_max_adds`
- `scale_in_min_profit_r`
- `scale_in_add_ratio`
- `reject_on_minlot_inflation`
- `continuous_analysis_enabled`
- `invalidation_score_threshold`
- `opposing_signal_threshold`
- `invalidation_min_hold_minutes`
- `conviction_monitoring_enabled`
- `conviction_decline_cycles`
- `conviction_decline_min_drop`
- `news_exit_enabled`
- `news_exit_minutes_before`
- `news_exit_mode`
- `session_close_enabled`
- `index_close_buffer_minutes`
- `dead_zone_management`
- `dynamic_sl_tightening_enabled`
- `dynamic_sl_tighten_at_r`
- `dynamic_sl_tighten_ratio`
- `portfolio_heat_enabled`
- `max_portfolio_heat_pct`
- `portfolio_heat_block_pct`
- `portfolio_risk_engine_enabled`
- `reference_balance`
- `heat_defensive_pct`
- `heat_recovery_pct`
- `recovery_dwell_seconds`
- `be_eligible_r_multiple`
- `defensive_action_cooldown_seconds`
- `portfolio_reduction_enabled`
- `heat_reduction_pct`
- `reduction_persist_seconds`
- `reduction_partial_ratio`
- `reduction_action_cooldown_seconds`
- `max_reductions_per_hour`
- `portfolio_emergency_enabled`
- `heat_emergency_pct`
- `emergency_reconcile_failure_seconds`
- `emergency_broker_exposure_tolerance`
- `emergency_max_closes_per_cycle`
- `emergency_action_cooldown_seconds`
- `emergency_max_closes_per_hour`
- `reconcile_max_unconfirmed_cycles`
- `drawdown_rolling_window_days`
- `model_swap_costs`
- `swap_rates_path`
- `swap_rollover_hour_utc`
- `swap_triple_weekday`
- `volatility_stop_mode`
- `atr_stop_period`
- `atr_stop_mult`
- `atr_stop_ratio_min`
- `atr_stop_ratio_max`
- `atr_stop_max_risk_mult`
- `opportunity_cost_exit_mode`
- `opportunity_cost_score_margin`
- `opportunity_cost_max_pnl_pips`
- `opportunity_cost_min_hold_minutes`
- `max_tick_age_seconds`
- `spread_monitor_enabled`
- `spread_deterioration_multiplier`
- `htf_reassessment_enabled`
- `htf_reassess_on_h1_close`
- `h4_bias_gate_enabled`
- `h4_bias_gate_mode`
- `h4_counter_trend_penalty`
- `regime_score_threshold_mode`
- `regime_below_threshold_size_mult`
- `execution_quality_sizing_enabled`
- `be_stop_cooldown_minutes`
- `max_deviation_points`
- `max_slippage_pips`
- `breakeven_min_profit_r`
- `trailing_swing_lookback`
- `structure_exit_tf_alignment_enabled`
- `structure_exit_tf_alignment_defer`
- `absolute_be_protection_enabled`
- `absolute_be_floor_usd`
- `absolute_be_floor_pips`
- `absolute_be_buffer_pips`
- `heat_trail_tighten_enabled`
- `heat_trail_factor_defensive`
- `heat_trail_factor_reducing`
- `heat_trail_factor_emergency`
- `management_status_log_interval_cycles`

## `DataBackupConfig` (10 fields)

- `enabled`
- `interval_hours`
- `max_file_size_mb`
- `exclude_patterns`
- `auto_sync_data_repo`
- `sync_branch`
- `data_repo_url`
- `sync_orphan_branch`
- `startup_purge_enabled`
- `clean_start_on_first_boot`

## `FeatureFlags` (1 fields)

- `data_path_fixes_enabled`

## `PerformanceConfig` (9 fields)

- `candle_cache_enabled`
- `candle_cache_ttl`
- `candle_cache_default_ttl`
- `m1_cache_ttl`
- `scan_interval_active`
- `scan_interval_with_positions`
- `parallel_scan_enabled`
- `parallel_scan_max_workers`
- `account_info_cache_enabled`

## `DecisionTraceConfig` (1 fields)

- `enabled`

## `OrchestratorConfig` (39 fields)

- `enabled`
- `apply_sizing`
- `size_floor`
- `dimension_floor`
- `ranker_ev_full`
- `use_ranker_ev`
- `de_margin_full`
- `scan_score_full`
- `soften_de_gate`
- `de_safety_margin`
- `de_gate_quality_floor`
- `scalp_htf_opposition_scale`
- `soften_scanner_gates`
- `soften_planner_gates`
- `soften_entry_gates`
- `accumulate_risk`
- `gate_quality_floor`
- `scanner_safety_oq`
- `scanner_safety_eq`
- `scanner_safety_score`
- `entry_safety_score`
- `manage_open_positions`
- `allow_scale_up`
- `health_dimension_floor`
- `health_thresholds`
- `risk_heat_full_pct`
- `risk_drawdown_full_r`
- `profit_loss_full_r`
- `time_decay_span_mult`
- `situation_shift_full`
- `thesis_change_flag`
- `expected_hold_minutes_scalp`
- `expected_hold_minutes_swing`
- `scale_down_close_pct`
- `exit_partial_close_pct`
- `scale_up_min_health`
- `scale_up_min_delta`
- `min_cycles_before_management`
- `management_cooldown_cycles`

## `OutcomeFeedbackConfig` (4 fields)

- `enabled`
- `journal_path`
- `accuracy_lookback`
- `gold_specialist`

## `SignalLedgerConfig` (8 fields)

- `signal_ledger_enabled`
- `signal_grading_enabled`
- `signal_grading_delay_minutes`
- `signal_grading_check_intervals`
- `signal_min_move_pct`
- `emitter_feedback_enabled`
- `signal_ledger_db_path`
- `accuracy_lookback`

## `VoteCalibratorConfig` (15 fields)

- `Safety`
- `vote_calibration_enabled`
- `vote_weight_method`
- `vote_weight_temperature`
- `vote_weight_floor`
- `vote_weight_ceiling`
- `vote_calibration_min_signals`
- `vote_calibration_shrinkage`
- `vote_calibration_lookback`
- `vote_calibration_min_interval_seconds`
- `use_counterfactual_weight`
- `counterfactual_weight_blend`
- `counterfactual_weight_min_trades`
- `per_symbol_enabled`
- `per_symbol_recompute_seconds`

## `ModuleGovernorConfig` (16 fields)

- `module_governor_enabled`
- `shadow_threshold`
- `shadow_lookback`
- `reactivation_threshold`
- `reactivation_min_signals`
- `disable_threshold`
- `disable_min_signals`
- `auto_retry_days`
- `feedback_lookback`
- `db_path`
- `use_counterfactual_signal`
- `marginal_r_shadow_threshold`
- `marginal_r_reactivation_threshold`
- `marginal_r_min_trades`
- `per_symbol_enabled`
- `per_symbol_recompute_seconds`

## `PostCloseTrackerConfig` (4 fields)

- `enabled`
- `db_path`
- `check_intervals_minutes`
- `max_retries`

## `PairLearnerConfig` (18 fields)

- `continuous_pair_multiplier`
- `continuous_midpoint`
- `continuous_steepness`
- `continuous_floor`
- `continuous_ceiling`
- `continuous_prior`
- `shrinkage_full_weight`
- `continuous_absolute_floor`
- `cold_start_multiplier`
- `cold_start_min_trades`
- `cold_start_score_boost`
- `cold_start_graduated`
- `early_avoid_min_trades`
- `early_avoid_max_win_rate`
- `early_avoid_max_consecutive_losses`
- `entry_management_split_enabled`
- `entry_accuracy_blend_weight`
- `management_quality_log_enabled`

## `TunerAgentConfig` (5 fields)

- `enabled`
- `max_tune_duration_seconds`
- `max_consecutive_failures`
- `audit_db_path`
- `log_all_skips`

## `CounterfactualConfig` (5 fields)

- `counterfactual_enabled`
- `attribution_lookback`
- `attribution_interval`
- `min_trades_for_attribution`
- `counterfactual_db_path`

## `InteractionConfig` (7 fields)

- `interaction_discovery_enabled`
- `interaction_lookback`
- `interaction_interval`
- `toxic_threshold`
- `synergy_threshold`
- `exhaustive_search_max_modules`
- `interaction_db_path`

## `SignalDiscoveryConfig` (27 fields)

- `signal_discovery_enabled`
- `discovery_lookback`
- `discovery_interval`
- `min_trades_for_discovery`
- `min_rule_support`
- `max_rule_conditions`
- `min_edge_r`
- `discovery_walk_forward_split`
- `signal_discovery_db_path`
- `bonferroni_alpha`
- `walk_forward_ratio_threshold`
- `score_decay_rate`
- `max_active_signals`
- `virtual_promotion_enabled`
- `shadow_trades_required`
- `min_shadow_accuracy`
- `min_shadow_marginal_r`
- `promotion_min_signals`
- `max_promotions_per_cycle`
- `promotion_initial_weight`
- `restart_shadow_trades`
- `retirement_accuracy_threshold`
- `retirement_min_signals`
- `retirement_check_interval`
- `retirement_marginal_r_min_trades`
- `retirement_marginal_r_threshold`
- `feedback_lookback`

## `CapitalAllocationConfig` (14 fields)

- `enabled`
- `short_horizon_trades`
- `medium_horizon_trades`
- `long_horizon_trades`
- `short_weight`
- `medium_weight`
- `long_weight`
- `rebalance_interval_trades`
- `max_allocation_shift`
- `min_allocation`
- `min_trades_for_scoring`
- `bayesian_prior_trades`
- `allocation_temperature`
- `capital_allocation_db_path`

## `ExecutionProfileConfig` (8 fields)

- `enabled`
- `default_profile`
- `allow_profile_creation`
- `max_active_profiles`
- `min_trades_for_scoring`
- `strong_consensus_threshold`
- `weak_consensus_threshold`
- `execution_profiles_db_path`

## `RegimeDetectionConfig` (11 fields)

- `enabled`
- `lookback_bars`
- `hysteresis_bars`
- `volatility_short_window`
- `volatility_long_window`
- `adx_period`
- `autocorrelation_lag`
- `trending_threshold`
- `volatile_threshold`
- `quiet_threshold`
- `regime_detection_db_path`

## `RiskManagementConfig` (15 fields)

- `enabled`
- `daily_drawdown_limit_pct`
- `rolling_drawdown_limit_pct`
- `hard_stop_drawdown_pct`
- `cooldown_hours`
- `cooldown_sizing_factor`
- `max_simultaneous_positions`
- `max_per_pair_positions`
- `max_directional_exposure_pct`
- `max_per_regime_pct`
- `correlation_threshold`
- `max_correlated_exposure_factor`
- `correlation_lookback_bars`
- `correlation_update_interval`
- `risk_management_db_path`

## `BehaviorDiscoveryConfig` (13 fields)

- `behavior_discovery_enabled`
- `behavior_lookback`
- `min_trades_to_cluster`
- `min_cluster_size`
- `max_clusters`
- `recluster_every_n_trades`
- `cluster_eps`
- `bayesian_prior_trades`
- `promote_threshold`
- `retire_threshold`
- `cooldown_trades`
- `centroid_match_eps`
- `behavior_discovery_db_path`

## `BacktestConfig` (7 fields)

- `enabled`
- `default_spread_pips`
- `default_slippage_pips`
- `use_memory_db`
- `progress_interval`
- `synthetic_seed`
- `starting_balance`

## `OpsConfig` (18 fields)

- `enabled`
- `shutdown_timeout_seconds`
- `crash_marker_path`
- `heartbeat_interval_seconds`
- `heartbeat_file`
- `max_tick_duration_seconds`
- `tick_profiling_enabled`
- `slow_tick_threshold_ms`
- `profiling_window_size`
- `log_format`
- `log_level`
- `log_max_size_mb`
- `log_max_files`
- `main_log`
- `trade_audit_log`
- `risk_audit_log`
- `reconnect_max_retries`
- `reconnect_base_delay_seconds`

## `HealthAssessorConfig` (8 fields)

- `enabled`
- `window_size`
- `max_history`
- `critical_entry_rate_threshold`
- `critical_learner_loss_rate`
- `degraded_learner_loss_rate`
- `auto_freeze_on_critical`
- `auto_release_on_healthy`

## `LearningGovernanceConfig` (14 fields)

- `governance_required`
- `recommendation_history_limit`
- `max_size_multiplier`
- `min_size_multiplier`
- `max_weight_multiplier`
- `enforce_toxic_pairs`
- `promotion_validation_min_signals`
- `promotion_validation_min_accuracy`
- `promotion_limited_min_signals`
- `promotion_limited_min_accuracy`
- `promotion_full_min_signals`
- `promotion_full_min_accuracy`
- `promotion_full_min_marginal_r`
- `health_assessor`

## `ConvictionNormalizationConfig` (6 fields)

- `enabled`
- `min_samples`
- `max_history`
- `blend`
- `persist`
- `state_path`

## `CalibrationConfig` (4 fields)

- `enabled`
- `state_path`
- `spread_sample_tf`
- `persist`

## `DevelopingAnalysisConfig` (10 fields)

- `enabled`
- `max_workers`
- `confidence_discount`
- `refresh_m5`
- `refresh_m15`
- `refresh_h1`
- `refresh_h4`
- `refresh_d1`
- `management_advisory_enabled`
- `management_advisory_min_confidence`

## `TickStateChangeConfig` (10 fields)

- `enabled`
- `warmup_ticks`
- `vol_alpha`
- `spread_alpha`
- `velocity_alpha`
- `price_move_atr_fraction`
- `spread_spike_mult`
- `velocity_spike_mult`
- `reversal_atr_fraction`
- `min_interval_seconds`

## `ThesisConfig` (19 fields)

- `gate_enabled`
- `flat_ev`
- `opportunity_cost_threshold`
- `decay_rate`
- `continuous_reeval_enabled`
- `continuous_reeval_min_interval_seconds`
- `thesis_decay_enabled`
- `thesis_decay_half_life`
- `thesis_decay_floor`
- `evidence_exit_enabled`
- `evidence_exit_check_interval`
- `thesis_flip_exit_enabled`
- `evidence_exit_min_hold_time`
- `atomic_reversal_enabled`
- `reversal_min_thesis_ev`
- `reversal_cooldown`
- `max_reversals_per_session`
- `reversal_threshold_escalation`
- `reversal_max_age_seconds`

## `CampaignConfig` (8 fields)

- `the`
- `enabled`
- `dormant_after_seconds`
- `invalidate_after_seconds`
- `history_limit`
- `postmortem_enabled`
- `sound_evidence_threshold`
- `evidence_full_refreshes`

## `LLMConfig` (28 fields)

- `enabled`
- `provider`
- `model`
- `api_key`
- `base_url`
- `drive_decisions`
- `timeout_seconds`
- `max_tokens`
- `temperature`
- `min_interval_seconds`
- `worker_interval_seconds`
- `max_symbols_per_cycle`
- `extra_models`
- `model_policy`
- `consult_multi`
- `consult_max_engines`
- `consult_mode`
- `circuit_failure_threshold`
- `circuit_cooldown_seconds`
- `circuit_cooldown_max_seconds`
- `local_max_concurrency`
- `reasoning_compute_class`
- `recovery_probe_seconds`
- `rpm_limit`
- `rpd_limit`
- `tpm_limit`
- `tpd_limit`
- `acknowledge_single_provider`

## `ComposioConfig` (25 fields)

- `enabled`
- `api_key`
- `base_url`
- `entity_id`
- `api_version`
- `transport`
- `mcp_url`
- `mcp_auth_header`
- `mcp_router_tool`
- `mcp_router_tools_key`
- `mcp_router_slug_key`
- `mcp_router_args_key`
- `dry_run`
- `require_source`
- `auto_max_risk`
- `min_confidence`
- `medium_confidence_threshold`
- `timeout_seconds`
- `available_providers`
- `provider_preferences`
- `knowledge_arg_overrides`
- `knowledge_enabled`
- `knowledge_interval_seconds`
- `knowledge_max_items`
- `advisor_enabled`

## `CognitionConfig` (45 fields)

- `enabled`
- `per_module_evidence`
- `ev_primary_gate`
- `ev_action_threshold_r`
- `min_advisors_for_action`
- `degraded_confidence_multiplier`
- `degrade_on_unknown_council`
- `loop_interval_seconds`
- `max_symbols_per_cycle`
- `concept_timeframes`
- `concept_history_bars`
- `emit_operator_notifications`
- `event_driven`
- `event_min_interval_seconds`
- `manage_min_interval_seconds`
- `allow_scale_in`
- `reallocation_enabled`
- `reallocation_max_per_component`
- `reallocation_concentration_limit`
- `opportunity_qualification_enabled`
- `opportunity_cost_multiple`
- `opportunity_min_net_ev`
- `commission_per_lot_round_trip`
- `commission_model_verified`
- `financing_per_lot_round_trip`
- `financing_model_verified`
- `origination_mode`
- `origination_risk_fraction`
- `origination_max_exposure`
- `management_mode`
- `origination_max_lots`
- `memory_enabled`
- `memory_max_analogues`
- `operations_enabled`
- `operations_report_period_seconds`
- `operations_loss_streak`
- `operations_cooldown_seconds`
- `influence_enabled`
- `influence_min_samples`
- `influence_min_weight`
- `influence_max_weight`
- `influence_weighting_enabled`

## `AdaptiveTunerConfig` (6 fields)

- `enabled`
- `periodic_interval_seconds`
- `scan_interval_seconds`
- `recommendation_apply_enabled`
- `recommendation_max_change_pct`
- `recommendation_min_trades`

## `CrossInstrumentConfig` (18 fields)

- `queue_enabled`
- `queue_window_ms`
- `ranking_enabled`
- `spread_ev_penalty_per_pip`
- `winrate_ev_weight`
- `quality_sizing_enabled`
- `quality_sizing_max_boost`
- `quality_sizing_min_cut`
- `quality_sizing_ev_ref`
- `displacement_enabled`
- `displacement_ev_margin`
- `displacement_max_per_cycle`
- `displacement_min_profit_protect`
- `displacement_cooldown_seconds`
- `proactive_scan_enabled`
- `proactive_scan_interval_seconds`
- `proactive_scan_min_ev`
- `proactive_proximity_pct`

## `RegimeLearnerConfig` (2 fields)

- `per_symbol_enabled`
- `per_symbol_min_trades`

## `SessionLearnerConfig` (2 fields)

- `per_symbol_enabled`
- `per_symbol_min_trades`

## `AppConfig` (54 fields)

- `universe_mode`
- `enabled_categories`
- `enabled_symbols_override`
- `broker_discovery_complete`
- `broker_enabled_symbols`
- `scoring`
- `risk`
- `owner_limits`
- `confirmation_penalties`
- `opportunity_ranker`
- `decision_trace`
- `orchestrator`
- `outcome_feedback`
- `signal_ledger`
- `post_close_tracker`
- `pair_learner`
- `regime_learner`
- `session_learner`
- `vote_calibrator`
- `module_governor`
- `calibration`
- `developing_analysis`
- `tick_state_change`
- `thesis`
- `campaign`
- `llm`
- `composio`
- `cognition`
- `conviction_normalization`
- `tuner_agent`
- `adaptive_tuner`
- `cross_instrument`
- `counterfactual`
- `interaction`
- `learning_governance`
- `signal_discovery`
- `capital_allocation`
- `execution_profiles`
- `regime_detection`
- `risk_management`
- `behavior_discovery`
- `layered_decision`
- `backtest`
- `data_backup`
- `planner`
- `governor`
- `performance`
- `ops`
- `features`
- `max_consecutive_cycle_failures`
- `log_level`
- `try`
- `try`
- `try`

## Current operator-facing trading controls

- `TRADING_MODE`, `TRADING_SYMBOLS`, `TRADING_ENABLED_CATEGORIES`
- `TRADING_ENABLED`
- `COGNITION_ENABLED`
  `COGNITION_ORIGINATION_MODE`, `COGNITION_MANAGEMENT_MODE`
- Risk and portfolio limits are represented by the canonical typed
  `OwnerLimits` contract and authenticated API/dashboard controls; do not
  create duplicate unsynchronized risk knobs.

## Remaining migration work

1. Add an owner and category to every field above.
2. Mark internal/derived and invariant fields as non-overridable.
3. Move legitimate operator controls to a typed, validated environment schema.
4. Add explicit precedence tests between environment defaults and per-user
   durable configuration.
5. Generate the operator template from that schema and test that every
   supported setting is documented.
6. Verify no dashboard endpoint writes `.env`; runtime edits must remain
   authenticated, audited and durable.
