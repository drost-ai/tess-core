# Knob classification

Every module-level tunable in the runtime is classified here. Consult this
table before adding a numeric constant anywhere, and add a row for any new
one at review time. The registry itself lives in `_knob_registry.py`;
machinery in `_knobs.py`.

## Classes

- **knob** — process-wide runtime tuning. Declared in `KNOB_REGISTRY`,
  overridable via `knobs:` in `settings/config.yaml` and `AGENT_CORE_<DOTTED>`
  env vars.
- **invariant** — never configurable: security bounds, protocol constants,
  schema versions, memory-safety caps, algorithmic-complexity limits, poll
  granularities that encode correctness, record-format bounds.
- **profile-field** — per-provider values in `settings/providers.yaml`.
  Where a hardcoded default backs a profile field, the *default layer* may be
  a knob (marked "default layer"), mirroring `provider.max_output_tokens`.
- **config-key** — per-session/behavioral settings resolved by
  `resolveConfig` (e.g. `permissionMode`), not knobs.

Wave column: the Epic E slice that migrates the row (`—` = already done or
nothing to do).

## collaboration/

| Constant | Value | Class | Disposition | Wave |
|---|---|---|---|---|
| runtime.MIN_ASSISTANT_UPDATE_SPACING_MS | 20000 | knob | `collab.min_update_spacing_ms` | E6 |
| runtime.COLLABORATION_TICK_MS (public) | 1500 | knob | `collab.tick_ms`; retire export | E6 |
| bridge/admission.MAX_BRIDGE_MESSAGE_BYTES | 256K | invariant | admission bound | — |
| bridge/admission.MAX_IDEMPOTENCY_KEY_BYTES | 4K | invariant | admission bound | — |
| bridge/recovery_analysis.MAX_* (5) | various | invariant | corruption-scan safety bounds | — |
| bridge/runtime.BRIDGE_POLL_SECONDS | 0.75 | knob | `collab.bridge_poll_ms` (convert to ms) | E6 |
| bridge/runtime.BRIDGE_POLL_DEADLINE_SECONDS | 10.0 | knob | `collab.bridge_poll_deadline_ms` | E6 |

## tasks/

| Constant | Value | Class | Disposition | Wave |
|---|---|---|---|---|
| health_check_executor.HEALTH_LOG_MAX_BYTES | 4M | knob | `tasks.health_log_max_bytes` | E6 |
| health_check_executor.HEALTH_LOG_ROTATIONS | 3 | knob | `tasks.health_log_rotations` | E6 |
| health_check_executor.HEALTH_EVENT_EXCERPT_BYTES | 4K | knob | `tasks.health_event_excerpt_bytes` | E6 |
| health_check_migration.MAX_LEGACY_HEALTH_RECORD_BYTES | 1M | invariant | migration safety bound | — |
| health_check_revalidation.GRANT_TTL_SECONDS | 300 | invariant | authorization TTL (security) | — |
| health_check_revalidation.MAX_LIVE_GRANTS | 128 | invariant | security bound | — |
| health_check_runtime.HEALTH_CHECK_DRAIN_TIMEOUT_SECONDS | 10 | knob | `tasks.health_drain_timeout_ms` | E6 |
| monitor_observation.MIN_PROGRESS_BYTES | 120 | knob | `tasks.monitor_min_progress_bytes` | E6 |
| monitors.MONITOR_TICK_MS | 1500 | knob | `tasks.monitor_tick_ms` | E6 |
| monitors.DEFAULT_CHECK_INTERVAL_MS | 5000 | knob | `tasks.monitor_check_interval_ms` | E6 |
| monitors.DEFAULT_HEARTBEAT_INTERVAL_MS | 60000 | knob | `tasks.monitor_heartbeat_interval_ms` | E6 |
| monitors.MIN_NARRATION_SPACING_MS | 20000 | knob | `tasks.monitor_narration_spacing_ms` | E6 |
| monitors.MAX_OUTPUT_EXCERPT_CHARS | 6000 | knob | `tasks.monitor_output_excerpt_chars` | E6 |
| supervisor_parts/live_work._DEAD_WORK_GRACE_MS | 15min | knob | `daemon.dead_work_grace_ms` | E2 |
| supervisor_parts/live_work._LIVE_WORK_CACHE_TTL_MS | 30000 | knob | `daemon.live_work_cache_ttl_ms` | E2 |
| supervisor_parts/live_work._MAX_VERDICT_DETAILS | 128 | invariant | diagnostic cap | — |
| supervisor_parts/mailbox_watch._MAX_ANNOUNCED_KEYS | 8192 | invariant | memory bound | — |
| supervisor.py inline health tick `asyncio.sleep(2)` | 2s | knob | `daemon.health_tick_ms` | E2 |
| task_stop_coordination inline sleep | 1ms | invariant | poll granularity | — |
| tools/tasks.py blocking poll `asyncio.sleep(0.25)` | 250ms | knob | `tools.task_output_poll_ms` | E6 |

## tools/

| Constant | Value | Class | Disposition | Wave |
|---|---|---|---|---|
| artifacts.DEFAULT_ARTIFACT_PAGE_BYTES | 16384 | knob | `tools.artifact_page_bytes` | E5 |
| artifacts.MAX/MIN_ARTIFACT_PAGE_BYTES | 24000/256 | invariant | request bounds | — |
| file_change_diff.LINE_DIFF_PRODUCT_LIMIT | 1e6 | invariant | complexity bound | — |
| file_change_diff.DIFF_CONTEXT_LINES | 3 | knob | `tools.diff_context_lines` | E5 |
| file_change_payloads.FILE_CHANGE_RESULT_MAX_BYTES | 12000 | knob | `payloads.file_change_result_max_bytes` | E5 |
| file_change_payloads.FILE_CHANGE_DIFF_MAX_LINES | 120 | knob | `payloads.file_change_diff_max_lines` | E5 |
| file_change_payloads.FILE_CHANGE_DIFF_TARGET_BYTES | 8000 | knob | `payloads.file_change_diff_target_bytes` | E5 |
| web_search_api.EXA_RESPONSE_MAX_BYTES | 2M | invariant | acquisition safety bound | — |

## subagents/

| Constant | Value | Class | Disposition | Wave |
|---|---|---|---|---|
| continuation_recovery.LEGACY_CLAIM_GRACE_MS | 30000 | knob | `subagents.legacy_claim_grace_ms` | E6 |
| coordination.DEFAULT_CONTINUATION_LEASE_MS | 30000 | knob | `subagents.continuation_lease_ms` | E6 |
| depth_policy.DEFAULT_SUBAGENT_MAX_DEPTH | 2 | knob | `subagents.max_depth` | E6 |
| reconciliation._SUMMARY_LIMIT | 1200 | invariant | formatting cap | — |
| continuation maxRecoveryAttempts default | 3 | knob | `subagents.max_recovery_attempts` | E6 |
| TESS/AGENT_CORE_SUBAGENT_MAX_CONCURRENCY (env) | 4 | knob | `subagents.max_concurrency`; absorb env as alias | E6 |
| *_CONCURRENCY_GROUP_LIMITS (env) | — | config-key | structured; stays env-only (documented) | E6 |

## context/ (compaction, memory, relief)

| Constant | Value | Class | Disposition | Wave |
|---|---|---|---|---|
| compaction_models.DEFAULT_COMPACTION_SYNTHESIS_TIMEOUT_MS | 300000 | knob | `compaction.synthesis_timeout_ms`; absorb `TESS_COMPACTION_SYNTHESIS_TIMEOUT_MS` | E3 |
| compaction_models.DEFAULT_COMPACTION_VERIFICATION_TIMEOUT_MS | 300000 | knob | `compaction.verification_timeout_ms`; absorb `TESS_COMPACTION_VERIFICATION_TIMEOUT_MS` | E3 |
| TESS_COMPACTION / TESS_COMPACTION_VERIFICATION (env) | on/off | config-key | behavior toggles; stay env/config, not knobs | E3 |
| context_policy.RAW_CONTEXT_CHECKPOINT_TOKEN_LIMIT | 200000 | knob | `context.raw_checkpoint_token_limit` | E3 |
| durable_memory.MAX_DURABLE_MEMORY_RECORDS | 48 | knob | `memory.max_records` | E3 |
| durable_memory.MAX_MEMORY_TEXT_LENGTH | 480 | knob | `memory.max_text_chars` | E3 |
| durable_memory._MAX_MANUAL_MEMORY_ACQUISITION_BYTES | 4096 | invariant | input bound | — |
| durable_memory.MAX_RETRIEVED_MEMORY_RECORDS | 8 | knob | `memory.retrieved_records` | E3 |
| durable_memory.MAX_RETRIEVED_MEMORY_TOKEN_BUDGET | 1600 | knob | `memory.retrieved_token_budget` | E3 |
| request_preflight.SCHEMA_VERSION / RECEIPT_MAX_BYTES | 1 / 16K | invariant | schema / record bound | — |
| token_accounting.DEFAULT_ESTIMATE_MARGIN_TOKENS | 2000 | knob | `context.estimate_margin_tokens` | E3 |
| token_accounting.MAX_CODEX_ERROR_SAMPLES | 12 | invariant | diagnostic cap | — |
| compaction/shared.MIN_RECENT_MESSAGE_COUNT | 20 | knob | `compaction.min_recent_messages` | E3 |
| compaction/shared.RAW_TAIL_TOKEN_TARGET | 100000 | knob | `compaction.raw_tail_token_target` | E3 |
| compaction/shared.RAW_TAIL_TOKEN_BUDGET | 40000 | knob | `compaction.raw_tail_token_budget` | E3 |
| compaction/shared.WORKING_CONTEXT_COMPACTION_TOKEN_LIMIT | 200000 | knob | `compaction.working_context_token_limit` | E3 |
| compaction/shared.MAX/MIN_PRESERVED_TOOL_EXCHANGE_PAIRS | 10/3 | knob/invariant | max: `compaction.max_preserved_tool_pairs`; min is a floor | E3 |
| compaction/shared.MAX_ACTIVITY_LINES/FAILURES/TODOS | 16/8/12 | invariant | synthesis structure caps | — |
| compaction/shared.COMPACTION_SYNTHESIS_MAX_RETRIES (public) | 3 | knob | `compaction.synthesis_max_retries`; retire export | E3 |
| compaction/shared.COMPACTION_VERIFICATION_MAX_RETRIES (public) | 3 | knob | `compaction.verification_max_retries`; retire export | E3 |
| compaction/shared.COMPACTION_EVIDENCE_TOKEN_BUDGET (public) | 90000 | knob | `compaction.evidence_token_budget`; retire export | E3 |
| compaction/shared.COMPACTION_MESSAGE_TEXT_CHAR_LIMIT (public) | 8000 | knob | `compaction.message_text_char_limit`; retire export | E3 |
| compaction/shared.COMPACTION_TOOL_INPUT_CHAR_LIMIT (public) | 2400 | knob | `compaction.tool_input_char_limit`; retire export | E3 |
| compaction/shared.COMPACTION_TOOL_RESULT_CHAR_LIMIT (public) | 8000 | knob | `compaction.tool_result_char_limit`; retire export | E3 |
| compaction/shared.COMPACTION_COMPACT_TOOL_RESULT_CHAR_LIMIT (public) | 600 | knob | `compaction.compact_tool_result_char_limit`; retire export | E3 |
| compaction/shared.COMPACTION_COMMAND_PREVIEW_CHAR_LIMIT (public) | 6000 | knob | `compaction.command_preview_char_limit`; retire export | E3 |
| compaction/shared.COMPACTION_FILE_PREVIEW_LINES (public) | 48 | knob | `compaction.file_preview_lines`; retire export | E3 |
| compaction/shared.COMPACTION_MATCH_PREVIEW_LIMIT (public) | 32 | knob | `compaction.match_preview_limit`; retire export | E3 |
| compaction/shared._PRESERVED_TRACE_PROMPT_TOKEN_BUDGET | 20000 | knob | `compaction.preserved_trace_token_budget` | E3 |
| continuation/shared.RELIEF_PRESERVE_MIN_MESSAGES | 12 | knob | `context.relief_preserve_min_messages` | E3 |
| continuation/shared.RELIEF_PREFERRED_RAW_TAIL_TOKENS | 24000 | knob | `context.relief_raw_tail_tokens` | E3 |
| continuation/shared.RELIEF_MESSAGE_TEXT_CHAR_LIMIT | 1200 | knob | `context.relief_message_text_chars` | E3 |
| continuation/shared.RELIEF_TOOL_INPUT_CHAR_LIMIT | 500 | knob | `context.relief_tool_input_chars` | E3 |
| continuation/shared.RELIEF_TOOL_RESULT_CHAR_LIMIT | 750 | knob | `context.relief_tool_result_chars` | E3 |
| continuation/shared.RELIEF_CONTEXT_TOKEN_LIMIT | 200000 | knob | `context.relief_context_token_limit` | E3 |

Layering note (applies to `compaction.*` token budgets and every
provider-default row below): where a per-profile field already exists
(`compactionThresholdTokens`, timeouts, image caps), the knob is only the
**default layer** used when the profile is silent — profile wins over knob,
knob wins over the hardcoded fallback.

## config/

| Constant | Value | Class | Disposition | Wave |
|---|---|---|---|---|
| _yaml._INDENT_STEP | 2 | invariant | parser spec | — |
| shared.DEFAULT_MAX_IMAGE_INPUT_COUNT | 8 | knob | `images.max_count` (default layer) | E5 |
| shared.DEFAULT_MAX_IMAGE_INPUT_BYTES | 5M | knob | `images.max_bytes` (default layer) | E5 |
| shared.DEFAULT_MAX_AGGREGATE_IMAGE_INPUT_BYTES | 20M | knob | `images.max_aggregate_bytes` (default layer) | E5 |
| shared.DEFAULT_MAX_ENCODED_IMAGE_REQUEST_BYTES | 24M | knob | `images.max_encoded_request_bytes` (default layer) | E5 |
| shared.MAX_PROVIDER_* (4) | various | invariant | ceilings bounding what profiles may request | — |

## acquisition/

| Constant | Value | Class | Disposition | Wave |
|---|---|---|---|---|
| bounded_web_fetch.WEB_FETCH_COMPRESSED/DECODED_MAX_BYTES | 8M/16M | invariant | fetch safety bounds | — |
| bounded_web_fetch.WEB_FETCH_CONNECT_TIMEOUT_MS | 10000 | knob | `web.fetch_connect_timeout_ms` | E5 |
| bounded_web_fetch.WEB_FETCH_HEADERS_TIMEOUT_MS | 10000 | knob | `web.fetch_headers_timeout_ms` | E5 |
| bounded_web_fetch.WEB_FETCH_BODY_IDLE_TIMEOUT_MS | 10000 | knob | `web.fetch_body_idle_timeout_ms` | E5 |
| bounded_web_fetch.WEB_FETCH_TOTAL_TIMEOUT_MS | 30000 | knob | `web.fetch_total_timeout_ms` | E5 |
| bounded_web_fetch.WEB_FETCH_MAX_REDIRECTS | 5 | knob | `web.fetch_max_redirects` | E5 |
| bounded_web_fetch.WEB_FETCH_MAX_HEADER_BYTES / _READ_CHUNK_BYTES | 64K | invariant | header bound / IO granularity | — |
| search.SEARCH_RECORD_MAX_BYTES / SEARCH_READ_CHUNK_BYTES | 1M / 64K | invariant | record bound / IO granularity | — |
| search.SEARCH_TIMEOUT_MS | 120000 | knob | `tools.search_timeout_ms` | E5 |
| search inline sleeps (10/20ms) | — | invariant | poll granularity | — |

## providers/

| Constant | Value | Class | Disposition | Wave |
|---|---|---|---|---|
| anthropic_cache.MAX_ANTHROPIC_CACHE_BREAKPOINTS | 4 | invariant | protocol | — |
| partial_output.*_SCHEMA_VERSION (3) | — | invariant | schema | — |
| partial_output.PARTIAL_OUTPUT_ACK_TIMEOUT_MS | 5000 | knob | `provider.partial_output_ack_timeout_ms` | E4 |
| prompt_cache_diagnostics.MAX_* (7) | various | invariant | diagnostic caps | — |
| timeouts.DEFAULT_TOKEN_COUNT_TIMEOUT_MS | 15000 | knob | `provider.token_count_timeout_ms` (default layer) | E4 |
| timeouts.DEFAULT_TOTAL_REQUEST_TIMEOUT_MS | 600000 | knob | `provider.total_request_timeout_ms` (default layer) | E4 |
| token_budget.DEFAULT_PROVIDER_MAX_CONTEXT_LENGTH | 200000 | knob | `provider.max_context_length` (default layer) | E4 |
| token_budget.DEFAULT_COMPACTION_THRESHOLD_TOKENS | 200000 | knob | `provider.compaction_threshold_tokens` (default layer) | E4 |
| token_budget.DEFAULT_COMPACTION_PRESERVATION_RATIO | 0.1 | knob | `provider.compaction_preservation_ratio` (float) | E4 |
| token_budget.DEFAULT_COMPACTION_HEADROOM_RATIO | 0.25 | knob | `provider.compaction_headroom_ratio` (float) | E4 |
| token_budget.DEFAULT_OUTPUT_BUDGET_SAFETY_MARGIN_TOKENS | 4096 | knob | `provider.output_budget_safety_margin_tokens` | E4 |
| token_budget.MIN_USEFUL_PROVIDER_OUTPUT_TOKENS | 256 | invariant | floor | — |
| token_budget.DEFAULT_CODEX_INLINE_TOOL_RESULT_BATCH_BYTES | 24000 | knob | `provider.codex_inline_tool_result_batch_bytes` | E4 |
| token_budget._MODEL_LIMIT_RULES (16 rules) | — | profile-field | model database → builtin profile defaults overridable in providers.yaml; hardcoded table stays last-resort fallback | E4 |
| openai chat/responses._OPENAI_DEFAULT_MAX_ATTEMPTS | 6 | knob | `provider.openai_max_attempts` | E4 |
| anthropic request maxAttempts default | 2 | knob | `provider.anthropic_max_attempts` | E4 |
| OPENAI/ANTHROPIC_ABORT_POLL_INTERVAL_MS | 50 | invariant | abort-poll correctness granularity | — |
| openai/reconcile.TERMINAL_RECONCILIATION_SCHEMA_VERSION | 1 | invariant | schema | — |
| watchdog inline sleeps (0/50ms) | — | invariant | poll granularity | — |

## payloads/

| Constant | Value | Class | Disposition | Wave |
|---|---|---|---|---|
| bounded_io.GENERIC_ONE_SHOT_MAX_BYTES | 16M | invariant | read safety bound | — |
| bounded_io/text_file_window.DEFAULT_STREAM_CHUNK_BYTES | 64K | invariant | IO granularity | — |
| cursor.MAX_CURSOR_BYTES | 1024 | invariant | record bound | — |
| image_budget.IMAGE_PREFIX_BYTES | 64K | invariant | sniff window | — |
| notebook.MAX_NOTEBOOK_CELLS_PER_PAGE | 20 | knob | `payloads.notebook_cells_per_page` | E5 |
| notebook.MAX_CELL_SOURCE_BYTES | 2000 | knob | `payloads.notebook_cell_source_bytes` | E5 |
| notebook.MAX_OUTPUT_SAMPLE_BYTES | 600 | knob | `payloads.notebook_output_sample_bytes` | E5 |
| notebook.MAX_OUTPUTS_PER_CELL | 12 | invariant | structure cap | — |
| result_guard.MAX_TOOL_IDENTITY/ARTIFACT_PATH/ERROR_MESSAGE_BYTES, MIN_EFFECTIVE | 256/1024/2000/512 | invariant | record-format bounds | — |
| result_guard.NON_ARTIFACT_TOOL_RESULT_MAX_BYTES | 32000 | knob | `payloads.non_artifact_tool_result_max_bytes` | E5 |
| result_guard.ABSOLUTE_TOOL_RESULT_CEILING_BYTES | 512000 | invariant | ceiling | — |
| result_guard.TOOL_RESULT_FINAL_BYTES (12/18/24K by class) | — | knob | `payloads.result_final_bytes_<class>` | E7 |
| tools.TOOL_PAYLOAD_POLICIES (class budget tables) | — | knob | byte budgets per class; line caps derived (bytes/20) | E7 |
| tools.deriveToolPayloadClass name sniffing | — | profile-field | `payloadClass` per profile; heuristic stays fallback | E7 |

## runtime/, agent/, session/

| Constant | Value | Class | Disposition | Wave |
|---|---|---|---|---|
| agent_bindings.AGENT_RUNTIME_BINDINGS_SCHEMA_VERSION | 1 | invariant | schema | — |
| bootstrap.MAX_STALE_DIAGNOSTICS | 128 | invariant | diagnostic cap | — |
| bootstrap.BOOTSTRAPPED_RUNTIME_CLOSE_FAILURE_MS (public) | 10000 | knob | `runtime.close_failure_ms`; retire export | E2 |
| identifiers.MAX_RUNTIME_ID_BYTES | 128 | invariant | record bound | — |
| secrets.* (version, 1M, 1.0s) | — | invariant | security | — |
| _question_gate.MAX_QUESTION_NUDGES | 3 | knob | `agent.max_question_nudges` | E6 |
| progress.PROGRESS_LEDGER_SCHEMA_VERSION | 1 | invariant | schema | — |
| progress.REPEATED_TOOL_CYCLE_LIMIT | 3 | knob | `agent.repeated_tool_cycle_limit` | E6 |
| progress.ARTIFACT_ACQUISITION_FAILURE_LIMIT (public) | 3 | knob | `agent.artifact_acquisition_failure_limit`; retire export | E6 |
| progress.GENERAL_NO_PROGRESS_LIMIT | 12 | knob | `agent.no_progress_limit` | E6 |
| progress.MAX_RECORDED_* / MAX_REPEATED_CYCLE_BATCHES | 64/32/8 | invariant | ledger bounds | — |
| session/message_origin.ORIGIN_SCHEMA_VERSION | 1 | invariant | schema | — |
| session/persisted_record_scanner.* (8) | various | invariant | scan safety bounds | — |
| authority/hooks.DEFAULT_HOOK_TIMEOUT_SECONDS | 60 | knob | `agent.hook_timeout_ms` | E6 |
| authority/permission_rules._RULES_CACHE_TTL_SECONDS | 5.0 | invariant | security freshness | — |
| authority versions/trust bounds | — | invariant | schema/security | — |

## mcp/

| Constant | Value | Class | Disposition | Wave |
|---|---|---|---|---|
| payloads.DEFAULT_INLINE_BYTES | 8000 | knob | `mcp.inline_bytes` | E6 |
| payloads.MAX_MCP_RESOURCE_PAGE | 40 | knob | `mcp.max_resource_page` | E6 |
| payloads.MAX_MCP_CONTENTS | 12 | invariant | structure cap | — |
| transport_policy.MCP_HEADER/FRAME/DECODED_BODY/SSE_EVENT_MAX_BYTES | various | invariant | protocol/safety | — |
| transport_policy.MCP_NOTIFICATION_QUEUE_MAX / PENDING_REQUEST_MAX | 256 | invariant | memory bounds | — |
| transport_policy.MCP_STDERR_TAIL_MAX_BYTES | 16K | invariant | diagnostic bound | — |
| transport_policy.MCP_DEFAULT_STARTUP_TIMEOUT_MS | 10000 | knob | `mcp.startup_timeout_ms` (default layer; per-server overrides win) | E6 |
| transport_policy.MCP_DEFAULT_REQUEST_TIMEOUT_MS | 30000 | knob | `mcp.request_timeout_ms` (default layer) | E6 |
| transport_policy.MCP_DEFAULT_CONNECT/HEADERS/BODY_IDLE_TIMEOUT_MS | 10000 | knob | `mcp.connect/headers/body_idle_timeout_ms` (default layer) | E6 |
| transport_policy.MCP_CLOSE_GRACE_MS / CLOSE_STDIN_GRACE_MS / CLOSE_KILL_WAIT_MS | 750/100/2000 | knob | `mcp.close_grace_ms` / `mcp.close_stdin_grace_ms` / `mcp.close_kill_wait_ms` | E6 |
| AGENT_CORE_MCP_BLOB_KEEP (env) | 32 | knob | `mcp.blob_keep`; absorb env as alias | E6 |

## storage/, daemon/, process/

| Constant | Value | Class | Disposition | Wave |
|---|---|---|---|---|
| storage/daemon.DAEMON_EVENTS_TAIL_BYTES | 64K | knob | `daemon.events_tail_bytes` | E2 |
| storage/daemon.DAEMON_EVENTS_ROTATE_BYTES (public) | 32M | knob | `daemon.events_rotate_bytes`; retire export | E2 |
| daemon/server rotation check period (inline 5min) | 300s | knob | `daemon.events_rotate_check_ms` | E2 |
| storage/json_io.STORAGE_LOCK_TIMEOUT_MS | 5000 | knob | `storage.lock_timeout_ms` (floor 500) | E2 |
| storage/paths.MAX_DAEMON_SOCKET_PATH_BYTES | 100 | invariant | OS constraint | — |
| storage/remote.BRIDGE_LOG/LEDGER/STORAGE caps | various | invariant | storage bounds | — |
| storage/sessions.MAX_SHALLOW_SESSION_BYTES | 16M | invariant | read bound | — |
| storage schema versions | — | invariant | schema | — |
| platform_support/session_hygiene._* (3) | various | invariant | scan bounds | — |
| daemon/_instance_gc._DEAD_INSTANCE_DIR_REAP_SECONDS | 3600 | knob | `daemon.instance_reap_ms` | E2 |
| daemon/lifecycle._DAEMON_BOOT_GRACE_SECONDS | 300 | knob | `daemon.boot_grace_ms` | E2 |
| daemon/lifecycle start timeout default (30s) | 30000 | knob | `daemon.start_timeout_ms`; absorb `AGENT_CORE_DAEMON_START_TIMEOUT_MS` | E2 |
| daemon/lifecycle instance-lock wait (5s) | 5000 | knob | `daemon.instance_lock_timeout_ms`; absorb `AGENT_CORE_DAEMON_INSTANCE_LOCK_TIMEOUT_MS` | E2 |
| daemon/protocol.DAEMON_AUTH_TIMEOUT_MS | 2000 | knob | `daemon.auth_timeout_ms`; absorb `AGENT_CORE_DAEMON_AUTH_TIMEOUT_MS` | E2 |
| daemon/protocol.DAEMON_RPC_TIMEOUT_MS | 30000 | knob | `daemon.rpc_timeout_ms`; absorb `AGENT_CORE_DAEMON_RPC_TIMEOUT_MS` | E2 |
| daemon/protocol frame/pending/request-id caps | various | invariant | protocol | — |
| daemon/security.* (modes, tree, state, token) | various | invariant | security | — |
| daemon/transport._DAEMON_CLOSE_GRACE_SECONDS | 0.25 | knob | `daemon.close_grace_ms` | E2 |
| daemon/transport._DAEMON_DISPATCH_DRAIN_SECONDS | 5.0 | knob | `daemon.dispatch_drain_ms` | E2 |
| daemon/lifecycle inline 50ms polls | — | invariant | poll granularity | — |
| process/_termination._POLL_INTERVAL_SECONDS | 0.02 | invariant | poll granularity | — |
| process/bash_lifecycle.DEFAULT_STDIO_LEAK_GRACE_MS | 2000 | knob | `process.stdio_leak_grace_ms` | E6 |
| process/bash_lifecycle.DEFAULT_TERMINATION_GRACE_MS | 1000 | knob | `process.bash_termination_grace_ms` | E6 |
| process/bash_lifecycle.DEFAULT_TERMINATION_FALLBACK_MS | 4000 | knob | `process.bash_termination_fallback_ms` | E6 |
| process/identity byte caps | various | invariant | safety bounds | — |
| process/output.SUBPROCESS_SPOOL_MAX_BYTES | 64M | knob | `process.spool_max_bytes` | E6 |
| process/output.SUBPROCESS_RING_MAX_BYTES | 128K | invariant | memory bound | — |
| process/owned_runner.MAX_ARGV_* | various | invariant | exec bounds | — |
| process/owned_runner.ACTIVATION_TIMEOUT_SECONDS | 30 | knob | `process.activation_timeout_ms` | E2 |

## Legacy env vars

| Env var | Disposition |
|---|---|
| AGENT_CORE_DAEMON_START/RPC/AUTH/INSTANCE_LOCK_TIMEOUT_MS | keep as alias of `daemon.*` knobs (E2); deprecation sweep in E6 |
| TESS_COMPACTION_SYNTHESIS/VERIFICATION_TIMEOUT_MS | keep as alias of `compaction.*_timeout_ms` (E3); deprecate E6 |
| TESS_COMPACTION / TESS_COMPACTION_VERIFICATION | behavior toggles: stay env (config-key candidates), not knobs |
| TESS/AGENT_CORE_SUBAGENT_MAX_CONCURRENCY | fold into `subagents.max_concurrency` (E6) |
| TESS/AGENT_CORE_SUBAGENT_CONCURRENCY_GROUP_LIMITS | structured; stays env-only, documented (E6) |
| AGENT_CORE_MCP_BLOB_KEEP | fold into `mcp.blob_keep` (E6) |
| TESS_WEB_SEARCH_PROVIDER, TESS_ALTERNATE_SCREEN, TESS_SCREEN_MODE, TESS_TMUX_* | behavioral/platform config-keys; out of knob scope |
| TESS_CHILD_*, TESS_LAUNCH_*, TESS_SESSION_ID/CWD/PROVIDER/MODEL/EFFORT_LEVEL, TESS_PLUGIN_* | worker/launch handshake contract — invariant |
| AGENT_CORE_HEADLESS_*, AGENT_CORE_TEST_* | test/dev seams — invariant |
| AGENT_CORE_SUBAGENTS_MAX_LIVE etc. | already canonical knob env overrides |
