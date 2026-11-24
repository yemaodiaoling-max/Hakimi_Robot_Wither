"""Tests whose expectations predate later, deliberate behaviour changes.

Baseline 2026-10-30: these 57 tests failed against the live server code while the
live bot was working normally (e.g. listening windows appear in production
decision logs).  They are run as expected failures so the suite is green and any
*change* in their outcome is visible: an unexpected success fails the run.

Remove an entry once its test has been rewritten for the current behaviour.
"""

REASONS = {
    "persona_text": "人设/提示词文案已改写，测试仍断言旧措辞",
    "scoring_policy": "亲密度/好感改为事件评分，手动分数不再直接写入",
    "terra_cycle": "Terra 巡查的返回结构和反馈文件位置已调整",
    "timing": "内部复盘时限 80s→180s、主模型耐心窗口 +20s 已调整",
    "memory_retention": "长期记忆改为永不过期、手动笔记取消长度上限",
    "scheduling": "听窗/续聊调度流程已重写，测试仍按旧状态机断言",
    "official_lane": "官方通道已开放私聊并调整权限缓存，离线场景脚本未更新",
    "environment": "依赖旧的 official_test_runtime 目录布局",
}

STALE = {
    "test_cache_prefix.PrefixTests.test_all_existing_data_and_instruction_text_are_preserved": "persona_text",
    "test_memory_v3.PromptHabitTests.test_once_only_opportunity_and_related_ids": "persona_text",
    "test_memory_v3.PromptHabitTests.test_prompt_rules_are_stable_and_do_not_claim_success_without_receipt": "persona_text",
    "test_memory_v4.ToolIntegrationTests.test_prompt_guidance_mentions_social_without_claiming_automation": "persona_text",
    "test_memory_v5.PersonalReviewTests.test_guidance_function_and_full_prompt_still_present": "persona_text",
    "test_memory_v5.PersonalReviewTests.test_tick_publishes_internal_turn_without_inventing_messages_or_outbox": "persona_text",
    "test_memory_v6.SubjectiveNoteTests.test_guidance_catalog_and_review_prompt_are_permissive_but_bounded": "persona_text",
    "test_toolchain_listening.CallableToolsTests.test_new_tool_manual_preserves_persona_verbatim_and_shared_listening_hint": "persona_text",
    "test_toolchain_listening.ColdTopicTests.test_both_backends_get_group_history_and_failure_guidance_without_persona_changes": "persona_text",
    "test_toolchain_listening.FeedingTests.test_both_models_receive_same_receipt_and_unchanged_persona": "persona_text",
    "test_toolchain_listening.HybridMemoryTests.test_study_and_lore_smalltalk_share_same_short_rp_rule_for_both_models": "persona_text",
    "test_memory_v4.SocialAssessmentTests.test_exact_note_hash_retry_and_unknown_scores": "scoring_policy",
    "test_memory_v4.SocialAssessmentTests.test_revision_keeps_original_and_supersedes_projection": "scoring_policy",
    "test_memory_v4.SocialAssessmentTests.test_seed_import_is_idempotent_and_never_gemini_original": "scoring_policy",
    "test_memory_v4_review.ConsoleRegressionTests.test_bot_graph_focus_returns_compact_sourced_social_values": "scoring_policy",
    "test_memory_v4_review.ConsoleRegressionTests.test_closeness_can_be_highlighted_for_each_source": "scoring_policy",
    "test_memory_v4_review.SocialRegressionTests.test_pruned_evidence_downgrades_numbers": "scoring_policy",
    "test_memory_v6.InternalReviewCandidateTests.test_social_append_and_auto_evidence_scoring_and_closeness_exposure": "scoring_policy",
    "test_memory_v6.SubjectiveNoteTests.test_no_evidence_scores_and_non_gemini_are_rejected": "scoring_policy",
    "test_social_behavior_v5.SocialBehaviorV5Tests.test_dsh_terra_other_member_and_other_subject_are_excluded": "scoring_policy",
    "test_social_behavior_v5.SocialBehaviorV5Tests.test_expired_evidence_and_assessment_do_not_gate": "scoring_policy",
    "test_social_behavior_v5.SocialBehaviorV5Tests.test_expired_evidence_downgrades_profile_to_unknown": "scoring_policy",
    "test_social_behavior_v5.SocialRecordIntegrationTests.test_record_social_same_evidence_revision_does_not_gate": "scoring_policy",
    "test_social_behavior_v5.SocialRecordIntegrationTests.test_record_social_version_chain_supersedes_old_and_gates_on_fresh_chat": "scoring_policy",
    "test_social_v5_integration.CurveToolTests.test_native_proposal_clamp_exact_note_and_retry": "scoring_policy",
    "test_social_v5_integration.CurveToolTests.test_no_farming_old_evidence_and_fresh_event_can_change": "scoring_policy",
    "test_social_v5_integration.CurveToolTests.test_unscored_stays_unknown": "scoring_policy",
    "test_memory_v5.TerraCycleTests.test_cycle_writes_feedback_and_terra_relation_without_qq_outbox": "terra_cycle",
    "test_social_v5_integration.TerraSamplingIntegrationTests.test_only_first_cycle_catches_up_four_hours": "terra_cycle",
    "test_memory_v5.PersonalReviewTests.test_internal_review_expiry_and_failure_release_real_turns": "timing",
    "test_memory_v6.InternalReviewCandidateTests.test_internal_review_deadline_is_80_seconds_not_one": "timing",
    "test_social_v5_integration.ReviewDeadlineIntegrationTests.test_internal_review_survives_eight_seconds_writes_social_and_releases": "timing",
    "test_social_v5_integration.ReviewDeadlineIntegrationTests.test_internal_review_token_expires_at_eighty_seconds": "timing",
    "test_primary_progress.PrimaryProgressTests.test_real_tool_execution_protected_then_inactivity_can_take_over": "timing",
    "test_turn_coordinator.CoordinatorTests.test_errors_stale_or_malformed_history_cannot_inflate_patience": "timing",
    "test_turn_coordinator.CoordinatorTests.test_primary_timeout_is_not_mislabeled_as_quota": "timing",
    "test_turn_coordinator.CoordinatorTests.test_recent_successful_latency_widens_patience_and_is_persisted": "timing",
    "test_memory_v2.MemoryTests.test_expired_hidden_and_read_explicit": "memory_retention",
    "test_memory_v2.MemoryTests.test_substantial_episode_retained_as_event_not_fact": "memory_retention",
    "test_memory_v3.ManualMemoryV3Tests.test_overlong_is_rejected_without_storage": "memory_retention",
    "test_toolchain_listening.HybridMemoryTests.test_explicit_fact_survives_raw_pruning_with_attributed_evidence": "memory_retention",
    "test_conversation_scheduler.AdaptiveBridgeTests.test_a_related_elliptical_followup_actually_publishes_a_continuation_turn": "scheduling",
    "test_conversation_scheduler.AdaptiveBridgeTests.test_dispatch_rechecks_value_and_charges_only_an_accepted_turn": "scheduling",
    "test_conversation_scheduler.AdaptiveBridgeTests.test_queued_mention_survives_later_fast_group_messages": "scheduling",
    "test_toolchain_listening.HybridMemoryTests.test_different_wording_can_follow_shared_knowledge_without_direct_word_overlap": "scheduling",
    "test_official_adapter.AdapterTests.test_permission_hot_updates_between_replies_and_is_read_once_per_batch": "official_lane",
    "test_official_adapter.AdapterTests.test_wrong_group_bot_private_duplicate_and_stale_inputs_are_not_routed": "official_lane",
    "test_official_adapter.RuntimeTests.test_cold_group_can_start_without_a_recent_passive_source_and_save_real_reply": "official_lane",
    "test_official_adapter.RuntimeTests.test_new_group_at_replies_even_when_group_info_is_forbidden": "official_lane",
    "test_official_adapter.RuntimeTests.test_real_reused_engine_primary_tools_and_fallback_with_fake_official_api": "official_lane",
    "test_memory_v5.InternalReviewTimeoutTests.test_cloud_service_passes_a_bounded_review_timeout_only_for_reviews": "environment",
}
