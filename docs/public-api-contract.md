# Cross-runtime public API contract

**Status:** v1, authored 2026-08-10 (Epic L, slice L1). Owner: Migel Tissera.
**Applies to:** `~/agents/agent-core` (TypeScript, `agent-core`), `~/agents/agent-core-python` (`agent_core`), `~/agents/agent-core-rust`, and a future Golang runtime.

## What this is

The single list of names an embedder may import from an agent-core runtime. It was
extracted from the two real consumers — **tess-code** (the TypeScript TUI, ~146 distinct
imports) and **SampleHost** (the Python embedder, 10 imports) — so it is the *measured*
API, not an aspirational one. Everything not in this contract is runtime-internal and
free to change without coordination.

## Rules

1. **Four entrypoints, same shape in every runtime.** TS `exports` keys map to Python
   packages (and should map to Rust/Go modules the same way):

   | Entrypoint | TypeScript | Python |
   |---|---|---|
   | root | `agent-core` | `agent_core` |
   | shell | `agent-core/shell` | `agent_core.shell_surface` |
   | platform | `agent-core/platform` | `agent_core.platform_support` |
   | testing | `agent-core/testing` | `agent_core.testing` |

2. **Concepts are shared; names are idiomatic per language.** One concept row per API
   element. TS uses camelCase, Python/Rust use snake_case for functions and CapWords for
   types. The Python columns show both today's name (many still camelCase from the TS
   port) and the target name after the native-Python conversion (Epic M). Renaming a
   Python name to its target is not an API break; removing or semantically changing a
   concept is, and requires editing this contract first.

3. **Wire and disk formats are NOT governed by this document — they are frozen
   separately.** Session/task/bridge/monitor record JSON keys, the daemon NDJSON
   protocol, MCP JSON-RPC, and runtime event names/payload keys are shared across
   runtimes and never change as part of API work.

4. **Statuses.** `active` = exists in the Python runtime today (guardrail-enforced).
   `pending` = in the contract (a real consumer imports it from the TS runtime) but not
   yet implemented in Python — mostly types that Epic M introduces. `not-planned` =
   deliberately absent (noted inline). `retired` = was active in Python and was removed from the root on
   2026-09-24 (`bootstrapRuntime`, `BootstrappedRuntime`, `createMailboxWakeController`:
   replaced by `start_runtime` and the session manager, interface page D9); the
   TypeScript runtime keeps its own `bootstrapRuntime` for tess-code. The internal
   module `agent_core.runtime.bootstrap` remains for the scenario harness and the
   headless runner until they move.

5. **Python extensions.** Rows marked `(Python extension)` are concepts this runtime
   introduced first (the runtime and session manager, monitor delivery, sub-agent
   lifetime: `start_runtime`, `Runtime`, `SessionManager`, `Session`, `SessionOptions`,
   `Unavailable`, `SessionBusy`, `SessionEnded`, `RuntimeStopped`, …). The concepts are
   shared; the TypeScript and Rust runtimes adopt them with idiomatic names when they
   need them, and this Python runtime leads for them. Their shapes beyond the names
   (the host `Listener` protocol, the publication receipt, `status()`/`inspect()`) are
   documented in `product-planning/03-runtime-and-session-manager/`.

5. **Guardrail semantics (Python).** `tests/fixtures/public_api/contract.json` is the
   machine-readable twin of this table and is enforced by `tests/test_refactor_guardrails.py`:
   the **root** entrypoint's `__all__` must equal exactly the active root names; the
   shell/platform/testing entrypoints must export at least their active contract names
   (they may keep additional runtime-internal surface). When Epic M renames a name or
   promotes a `pending` type, update `contract.json` in the same commit.

6. **Adoption by sibling runtimes.** TS already exports every `active`+`pending` name
   (tess-code compiles against it — that is the evidence). Rust/Go should implement
   concepts in contract order: root functions first, then types, then shell/platform/testing.

## The contract

### `agent_core` (root) — 108 names, 89 active

| Concept (TS name) | Kind | Python today | Python target (Epic M) | Python home | Status | Consumers |
|---|---|---|---|---|---|---|
| `ProviderRegistry` (Python extension) | type | `ProviderRegistry` | `ProviderRegistry` | `agent_core.config.registry` | active | tess-agent, agentic-memory |
| `ProviderRegistryError` (Python extension) | type | `ProviderRegistryError` | `ProviderRegistryError` | `agent_core.config.registry` | active | tess-agent, agentic-memory |
| `resolve_codex_auth` (Python extension) | function | `resolve_codex_auth` | `resolve_codex_auth` | `agent_core.config.registry` | active | tess-agent, agentic-memory |
| `resolve_provider_secret` (Python extension) | function | `resolve_provider_secret` | `resolve_provider_secret` | `agent_core.config.registry` | active | tess-agent, agentic-memory |
| `MonitorDeliveryController` (Python extension) | type | `MonitorDeliveryController` | `MonitorDeliveryController` | `agent_core.tasks.monitor_delivery` | active | tess-agent |
| `MonitorDeliveryError` (Python extension) | type | `MonitorDeliveryError` | `MonitorDeliveryError` | `agent_core.tasks.monitor_delivery` | active | tess-agent |
| `Runtime` (Python extension) | type | `Runtime` | `Runtime` | `agent_core.runtime.host` | active | tess-agent |
| `RuntimeOwnershipError` (Python extension) | type | `RuntimeOwnershipError` | `RuntimeOwnershipError` | `agent_core.runtime.owner_lease` | active | tess-agent |
| `start_runtime` (Python extension) | function | `start_runtime` | `start_runtime` | `agent_core.runtime.host` | active | tess-agent |
| `Session` (Python extension) | type | `Session` | `Session` | `agent_core.runtime.session_manager` | active | tess-agent |
| `SessionBusy` (Python extension) | type | `SessionBusy` | `SessionBusy` | `agent_core.runtime.session_manager` | active | tess-agent |
| `SessionEnded` (Python extension) | type | `SessionEnded` | `SessionEnded` | `agent_core.runtime.session_manager` | active | tess-agent |
| `RuntimeStopped` (Python extension) | type | `RuntimeStopped` | `RuntimeStopped` | `agent_core.runtime.session_manager` | active | tess-agent |
| `SessionManager` (Python extension) | type | `SessionManager` | `SessionManager` | `agent_core.runtime.session_manager` | active | tess-agent |
| `SessionOptions` (Python extension) | type | `SessionOptions` | `SessionOptions` | `agent_core.runtime.session_manager` | active | tess-agent |
| `SessionUnavailableError` (Python extension) | type | `SessionUnavailableError` | `SessionUnavailableError` | `agent_core.runtime.session_manager` | active | tess-agent |
| `Unavailable` (Python extension) | type | `Unavailable` | `Unavailable` | `agent_core.runtime.session_manager` | active | tess-agent |
| `UnknownSessionError` (Python extension) | type | `UnknownSessionError` | `UnknownSessionError` | `agent_core.runtime.session_manager` | active | tess-agent |
| `activeSessionPayload` | function | `activeSessionPayload` | `active_session_payload` | `agent_core.session.runtime` | active | samplehost |
| `bootstrapRuntime` | function | `bootstrapRuntime` | `bootstrap_runtime` | `agent_core.runtime.bootstrap` | retired | samplehost, tess-code |
| `buildSystemPromptForAgent` | function | `buildSystemPromptForAgent` | `build_system_prompt_for_agent` | `agent_core.config.prompts` | active | tess-code |
| `clearAuth` | function | `clear_auth` | `clear_auth` | `agent_core.storage.auth_files` | active | tess-code |
| `configureRuntimeHome` | function | `configure_runtime_home` | `configure_runtime_home` | `agent_core.storage.paths` | active | tess-code |
| `connectTaskSupervisorDaemonClient` | function | `connectTaskSupervisorDaemonClient` | `connect_task_supervisor_daemon_client` | `agent_core.daemon.lifecycle` | active | tess-code |
| `createAgentRuntimeBindings` | function | `createAgentRuntimeBindings` | `create_agent_runtime_bindings` | `agent_core.runtime.agent_bindings` | active | samplehost, tess-code |
| `createBridgeRuntime` | function | `createBridgeRuntime` | `create_bridge_runtime` | `agent_core.remote.bridge_runtime` | active | tess-code |
| `createBuiltinTools` | function | `createBuiltinTools` | `create_builtin_tools` | `agent_core.tools.registry` | active | tess-code |
| `createCollaborationRuntime` | function | `createCollaborationRuntime` | `create_collaboration_runtime` | `agent_core.tasks.collaboration` | active | tess-code |
| `createEmptySession` | function | `create_empty_session` | `create_empty_session` | `agent_core.storage.sessions` | active | tess-code |
| `createGitWorktree` | function | `createGitWorktree` | `create_git_worktree` | `agent_core.runtime.workspace` | active | tess-code |
| `createInitialRuntimeState` | function | `createInitialRuntimeState` | `create_initial_runtime_state` | `agent_core.runtime.state.store` | active | tess-code |
| `createMailboxWakeController` | function | `createMailboxWakeController` | `create_mailbox_wake_controller` | `agent_core.runtime.wake` | retired | samplehost |
| `createMonitorRecord` | function | `createMonitorRecord` | `create_monitor_record` | `agent_core.tasks.monitors` | active | tess-code |
| `createRemoteSession` | function | `createRemoteSession` | `create_remote_session` | `agent_core.remote.sessions` | active | tess-code |
| `createRuntimeEvent` | function | `createRuntimeEvent` | `create_runtime_event` | `agent_core.runtime.events.factory` | active | tess-code |
| `createRuntimeSessionBinding` | function | `createRuntimeSessionBinding` | `create_runtime_session_binding` | `agent_core.runtime.generation` | active | tess-code |
| `createRuntimeStateStore` | function | `createRuntimeStateStore` | `create_runtime_state_store` | `agent_core.runtime.state.store` | active | tess-code |
| `ensureBaseLayout` | function | `ensure_base_layout` | `ensure_base_layout` | `agent_core.storage.paths` | active | tess-code |
| `ensureTaskSupervisorDaemon` | function | `ensureTaskSupervisorDaemon` | `ensure_task_supervisor_daemon` | `agent_core.daemon.lifecycle` | active | tess-code |
| `finalizeBackgroundSubagentFailureFromEnv` | function | `finalizeBackgroundSubagentFailureFromEnv` | `finalize_background_subagent_failure_from_env` | `agent_core.subagents.finalizers` | active | tess-code |
| `finalizeBackgroundSubagentSuccessFromEnv` | function | `finalizeBackgroundSubagentSuccessFromEnv` | `finalize_background_subagent_success_from_env` | `agent_core.subagents.finalizers` | active | tess-code |
| `findGitRoot` | function | `find_git_root` | `find_git_root` | `agent_core.storage.paths` | active | tess-code |
| `findSessionsForWorkspace` | function | `findSessionsForWorkspace` | `find_sessions_for_workspace` | `agent_core.session.persistence` | active | tess-code |
| `formatBridgeTail` | function | `formatBridgeTail` | `format_bridge_tail` | `agent_core.remote.bridge_diagnostics` | active | tess-code |
| `formatSessionCandidate` | function | `formatSessionCandidate` | `format_session_candidate` | `agent_core.shell_surface.diagnostics` | active | tess-code |
| `getDefaultAutoMode` | function | `get_default_auto_mode` | `get_default_auto_mode` | `agent_core.storage.settings` | active | tess-code |
| `getStateRoot` | function | `get_state_root` | `get_state_root` | `agent_core.storage.paths` | active | tess-code |
| `isPlanRevisionPrompt` | function | — | `is_plan_revision_prompt` | — | not-planned | tess-code |
| `isTaskSupervisorGateway` | function | `isTaskSupervisorGateway` | `is_task_supervisor_gateway` | `agent_core.runtime.agent_bindings` | active | samplehost |
| `list_child_agents` | function | `list_child_agents` | `list_child_agents` | `agent_core.storage.records` | active | samplehost |
| `listActiveBridgeRows` | function | `listActiveBridgeRows` | `list_active_bridge_rows` | `agent_core.remote.bridge_diagnostics` | active | tess-code |
| `listSessions` | function | `list_sessions` | `list_sessions` | `agent_core.storage.sessions` | active | tess-code |
| `loadAuth` | function | `load_auth` | `load_auth` | `agent_core.storage.auth_files` | active | tess-code |
| `loadAutoMode` | function | `load_auto_mode` | `load_auto_mode` | `agent_core.storage.settings` | active | tess-code |
| `loadBridge` | function | `load_bridge` | `load_bridge` | `agent_core.storage.remote` | active | tess-code |
| `loadBridgeEvents` | function | `load_bridge_events` | `load_bridge_events` | `agent_core.storage.remote` | active | tess-code |
| `loadMarketplaces` | function | `load_marketplaces` | `load_marketplaces` | `agent_core.storage.settings` | active | tess-code |
| `loadMcpServers` | function | `load_mcp_servers` | `load_mcp_servers` | `agent_core.storage.settings` | active | tess-code |
| `loadPlugins` | function | `load_plugins` | `load_plugins` | `agent_core.storage.settings` | active | tess-code |
| `loadSession` | function | `load_session` | `load_session` | `agent_core.storage.session_archive` | active | tess-code |
| `persistSession` | function | `persistSession` | `persist_session` | `agent_core.session.persistence` | active | tess-code |
| `readOpenChildQuestions` | function | `readOpenChildQuestions` | `read_open_child_questions` | `agent_core.agent` | active | samplehost |
| `resetConfiguredRuntimeHome` | function | `reset_configured_runtime_home` | `reset_configured_runtime_home` | `agent_core.storage.paths` | active | tess-code |
| `resolveConfig` | function | `resolveConfig` | `resolve_config` | `agent_core.config.resolution` | active | samplehost, tess-code |
| `resolveModelForAgent` | function | `resolveModelForAgent` | `resolve_model_for_agent` | `agent_core.config.prompts` | active | tess-code |
| `resolveProviderProfile` | function | `resolveProviderProfile` | `resolve_provider_profile` | `agent_core.config.providers` | active | tess-code |
| `resolveProviderProfiles` | function | `resolveProviderProfiles` | `resolve_provider_profiles` | `agent_core.config.providers` | active | tess-code |
| `resolveSession` | function | `resolveSession` | `resolve_session` | `agent_core.session.persistence` | active | samplehost, tess-code |
| `resolveTeleportSession` | function | `resolveTeleportSession` | `resolve_teleport_session` | `agent_core.shell_surface.remote_select` | active | tess-code |
| `runAgentPrompt` | function | `runAgentPrompt` | `run_agent_prompt` | `agent_core.agent.loop` | active | samplehost, tess-code |
| `runCommand` | function | — | `run_command` | — | pending | tess-code |
| `runTaskSupervisorDaemon` | function | `runTaskSupervisorDaemon` | `run_task_supervisor_daemon` | `agent_core.daemon.server` | active | tess-code |
| `saveAuth` | function | `save_auth` | `save_auth` | `agent_core.storage.auth_files` | active | tess-code |
| `saveBridge` | function | `save_bridge` | `save_bridge` | `agent_core.storage.remote` | active | tess-code |
| `saveChildAgent` | function | `save_child_agent` | `save_child_agent` | `agent_core.storage.records` | active | tess-code |
| `saveMarketplaces` | function | `save_marketplaces` | `save_marketplaces` | `agent_core.storage.settings` | active | tess-code |
| `saveMcpServers` | function | `save_mcp_servers` | `save_mcp_servers` | `agent_core.storage.settings` | active | tess-code |
| `saveMonitor` | function | `save_monitor` | `save_monitor` | `agent_core.storage.records` | active | tess-code |
| `savePlugins` | function | `save_plugins` | `save_plugins` | `agent_core.storage.settings` | active | tess-code |
| `saveSession` | function | `save_session` | `save_session` | `agent_core.storage.sessions` | active | tess-code |
| `sendBridgeMessage` | function | `sendBridgeMessage` | `send_bridge_message` | `agent_core.remote.bridge_admission` | active | tess-code |
| `updateRemoteSession` | function | `updateRemoteSession` | `update_remote_session` | `agent_core.remote.sessions` | active | tess-code |
| `AgentRunResult` | type | — | `AgentRunResult` | — | pending | tess-code |
| `AgentRuntimeBindings` | type | — | `AgentRuntimeBindings` | — | pending | tess-code |
| `AnthropicMessage` | type | — | `AnthropicMessage` | — | pending | tess-code |
| `AnthropicUsage` | type | — | `AnthropicUsage` | — | pending | tess-code |
| `AskQuestionDetail` | type | `AskQuestionDetail` | `AskQuestionDetail` | `agent_core.contracts.context` | active | tess-code |
| `BootstrappedRuntime` | type | — | `BootstrappedRuntime` | — | retired | tess-code |
| `BridgeEventRecord` | type | — | `BridgeEventRecord` | — | pending | tess-code |
| `BridgeRecord` | type | — | `BridgeRecord` | — | pending | tess-code |
| `ChildAgentRecord` | type | `ChildAgentRecord` | `ChildAgentRecord` | `agent_core.contracts.task_records` | active | tess-code |
| `LaunchContext` | type | — | `LaunchContext` | — | pending | tess-code |
| `MarketplaceRecord` | type | — | `MarketplaceRecord` | — | pending | tess-code |
| `McpServerConfig` | type | — | `McpServerConfig` | — | pending | tess-code |
| `OutputFormat` | type | — | `OutputFormat` | — | pending | tess-code |
| `PermissionMode` | type | — | `PermissionMode` | — | pending | tess-code |
| `PluginManifest` | type | — | `PluginManifest` | — | pending | tess-code |
| `QuestionPermissionDetail` | type | — | `QuestionPermissionDetail` | — | pending | tess-code |
| `RemoteSessionRecord` | type | — | `RemoteSessionRecord` | — | pending | tess-code |
| `ResolvedCompactionProviderProfile` | type | — | `ResolvedCompactionProviderProfile` | — | pending | tess-code |
| `ResolvedConfig` | type | `ResolvedConfig` | `ResolvedConfig` | `agent_core.contracts.config` | active | tess-code |
| `ResolvedProviderCapabilities` | type | — | `ResolvedProviderCapabilities` | — | pending | tess-code |
| `RootOptions` | type | — | `RootOptions` | — | pending | tess-code |
| `RuntimeActivationContext` | type | `RuntimeActivationContext` | `RuntimeActivationContext` | `agent_core.runtime.generation` | active | tess-code |
| `RuntimeEvent` | type | — | `RuntimeEvent` | — | pending | tess-code |
| `RuntimeEventSource` | type | — | `RuntimeEventSource` | — | pending | tess-code |
| `RuntimeSecretPurpose` | type | — | `RuntimeSecretPurpose` | — | pending | tess-code |
| `RuntimeSecretRef` | type | `RuntimeSecretRef` | `RuntimeSecretRef` | `agent_core.contracts.config` | active | tess-code |
| `RuntimeSecretValue` | type | — | `RuntimeSecretValue` | — | pending | tess-code |
| `RuntimeSessionBinding` | type | `RuntimeSessionBinding` | `RuntimeSessionBinding` | `agent_core.runtime.generation` | active | tess-code |
| `RuntimeState` | type | — | `RuntimeState` | — | pending | tess-code |
| `RuntimeTurnSlice` | type | — | `RuntimeTurnSlice` | — | pending | tess-code |
| `SessionRecord` | type | `SessionRecord` | `SessionRecord` | `agent_core.contracts.session_records` | active | tess-code |
| `ToolDefinition` | type | `ToolDefinition` | `ToolDefinition` | `agent_core.tools.definitions` | active | tess-code |

### `agent_core.shell_surface` (shell) — 18 names, 11 active

| Concept (TS name) | Kind | Python today | Python target (Epic M) | Python home | Status | Consumers |
|---|---|---|---|---|---|---|
| `buildTranscriptSnapshot` | function | `buildTranscriptSnapshot` | `build_transcript_snapshot` | `agent_core.session.transcript` | active | tess-code |
| `compactModelName` | function | `compactModelName` | `compact_model_name` | `agent_core.shell_surface.status` | active | tess-code |
| `createDebugBundle` | function | `createDebugBundle` | `create_debug_bundle` | `agent_core.shell_surface.debug_bundle` | active | tess-code |
| `estimateEncodedImageRequestBytes` | function | `estimateEncodedImageRequestBytes` | `estimate_encoded_image_request_bytes` | `agent_core.payloads.image_budget` | active | tess-code |
| `formatUsageSummary` | function | `formatUsageSummary` | `format_usage_summary` | `agent_core.shell_surface.status` | active | tess-code |
| `handleSlashCommand` | function | `handleSlashCommand` | `handle_slash_command` | `agent_core.shell_surface.dispatcher` | active | tess-code |
| `listSlashCommands` | function | `listSlashCommands` | `list_slash_commands` | `agent_core.shell_surface.registry` | active | tess-code |
| `makeTextEntry` | function | `makeTextEntry` | `make_text_entry` | `agent_core.session.transcript` | active | tess-code |
| `makeToolEntry` | function | `makeToolEntry` | `make_tool_entry` | `agent_core.session.transcript` | active | tess-code |
| `mergeTranscriptEntry` | function | `mergeTranscriptEntry` | `merge_transcript_entry` | `agent_core.session.transcript` | active | tess-code |
| `normalizeTranscriptBody` | function | `normalizeTranscriptBody` | `normalize_transcript_body` | `agent_core.session.transcript` | active | tess-code |
| `AnthropicBlock` | type | — | `AnthropicBlock` | — | pending | tess-code |
| `RuntimeSlashCommand` | type | — | `RuntimeSlashCommand` | — | pending | tess-code |
| `SlashCommandSpec` | type | — | `SlashCommandSpec` | — | pending | tess-code |
| `TranscriptRenderEntry` | type | — | `TranscriptRenderEntry` | — | pending | tess-code |
| `TranscriptTone` | type | — | `TranscriptTone` | — | pending | tess-code |
| `TranscriptToolPayload` | type | — | `TranscriptToolPayload` | — | pending | tess-code |
| `TuiThemeName` | type | — | `TuiThemeName` | — | pending | tess-code |

### `agent_core.platform_support` (platform) — 27 names, 24 active

| Concept (TS name) | Kind | Python today | Python target (Epic M) | Python home | Status | Consumers |
|---|---|---|---|---|---|---|
| `applyDaemonDoctorFixes` | function | — | `apply_daemon_doctor_fixes` | — | pending | tess-code |
| `approveMcpSource` | function | `approveMcpSource` | `approve_mcp_source` | `agent_core.authority.executable_source_policy` | active | tess-code |
| `approvePluginSource` | function | `approvePluginSource` | `approve_plugin_source` | `agent_core.authority.executable_source_policy` | active | tess-code |
| `authorizeRuntimeSecretWrite` | function | `authorizeRuntimeSecretWrite` | `authorize_runtime_secret_write` | `agent_core.runtime.secrets` | active | tess-code |
| `collectDaemonDoctorChecks` | function | — | `collect_daemon_doctor_checks` | — | pending | tess-code |
| `collectDoctorReport` | function | `collectDoctorReport` | `collect_doctor_report` | `agent_core.platform_support.doctor` | active | tess-code |
| `getRepositoryTrustStatus` | function | `getRepositoryTrustStatus` | `get_repository_trust_status` | `agent_core.authority.repository_trust` | active | tess-code |
| `inspectMcpSourceTrust` | function | `inspectMcpSourceTrust` | `inspect_mcp_source_trust` | `agent_core.authority.executable_source_policy` | active | tess-code |
| `inspectPluginSourceTrust` | function | `inspectPluginSourceTrust` | `inspect_plugin_source_trust` | `agent_core.authority.executable_source_policy` | active | tess-code |
| `isRuntimeSecretRef` | function | `isRuntimeSecretRef` | `is_runtime_secret_ref` | `agent_core.runtime.secrets` | active | tess-code |
| `maybeAutoConnectIde` | function | `maybeAutoConnectIde` | `maybe_auto_connect_ide` | `agent_core.platform_support.ide` | active | tess-code |
| `maybeExecIntoTmux` | function | `maybeExecIntoTmux` | `maybe_exec_into_tmux` | `agent_core.platform_support.tmux` | active | tess-code |
| `parseInstalledPluginRecord` | function | `parseInstalledPluginRecord` | `parse_installed_plugin_record` | `agent_core.authority.executable_config` | active | tess-code |
| `parseMarketplaceManifest` | function | `parseMarketplaceManifest` | `parse_marketplace_manifest` | `agent_core.authority.executable_config` | active | tess-code |
| `parseMcpServerConfig` | function | `parseMcpServerConfig` | `parse_mcp_server_config` | `agent_core.authority.executable_config` | active | tess-code |
| `parseMcpServerConfigList` | function | `parseMcpServerConfigList` | `parse_mcp_server_config_list` | `agent_core.authority.executable_config` | active | tess-code |
| `parsePluginManifestV1` | function | `parsePluginManifestV1` | `parse_plugin_manifest_v1` | `agent_core.authority.executable_config` | active | tess-code |
| `parseSettingSources` | function | `parseSettingSources` | `parse_setting_sources` | `agent_core.authority.executable_source_policy` | active | tess-code |
| `resolveChromeCompanionServer` | function | `resolveChromeCompanionServer` | `resolve_chrome_companion_server` | `agent_core.platform_support.chrome` | active | tess-code |
| `resolveTmuxLaunchContextFromEnv` | function | `resolveTmuxLaunchContextFromEnv` | `resolve_tmux_launch_context_from_env` | `agent_core.platform_support.tmux` | active | tess-code |
| `revokeRepositoryApproval` | function | `revokeRepositoryApproval` | `revoke_repository_approval` | `agent_core.authority.repository_trust` | active | tess-code |
| `runChromeCompanionMcpServer` | function | `runChromeCompanionMcpServer` | `run_chrome_companion_mcp_server` | `agent_core.platform_support.chrome` | active | tess-code |
| `runChromeNativeHost` | function | `runChromeNativeHost` | `run_chrome_native_host` | `agent_core.platform_support.chrome` | active | tess-code |
| `runRepoInstall` | function | `runRepoInstall` | `run_repo_install` | `agent_core.platform_support.install` | active | tess-code |
| `runRepoUpdate` | function | `runRepoUpdate` | `run_repo_update` | `agent_core.platform_support.install` | active | tess-code |
| `storeRuntimeSecret` | function | `storeRuntimeSecret` | `store_runtime_secret` | `agent_core.runtime.secrets` | active | tess-code |
| `RepositoryApprovalScope` | type | — | `RepositoryApprovalScope` | — | pending | tess-code |

### `agent_core.testing` (testing) — 10 names, 8 active

| Concept (TS name) | Kind | Python today | Python target (Epic M) | Python home | Status | Consumers |
|---|---|---|---|---|---|---|
| `buildMessagesForWorkingContext` | function | `build_messages_for_working_context` | `build_messages_for_working_context` | `agent_core.context.compaction.projection` | active | tess-code |
| `createProviderFailureRecord` | function | `createProviderFailureRecord` | `create_provider_failure_record` | `agent_core.providers.errors` | active | tess-code |
| `createProviderRequestError` | function | `createProviderRequestError` | `create_provider_request_error` | `agent_core.providers.errors` | active | tess-code |
| `isProviderRequestError` | function | `isProviderRequestError` | `is_provider_request_error` | `agent_core.providers.errors` | active | tess-code |
| `runCompaction` | function | `run_compaction` | `run_compaction` | `agent_core.context.compaction.orchestration` | active | tess-code |
| `sendAnthropicMessagesRequest` | function | `sendAnthropicMessagesRequest` | `send_anthropic_messages_request` | `agent_core.providers.anthropic.request` | active | tess-code |
| `sendOpenAiResponsesRequest` | function | `sendOpenAiResponsesRequest` | `send_open_ai_responses_request` | `agent_core.providers.openai.responses` | active | tess-code |
| `CompactionModelClient` | type | `CompactionModelClient` | `CompactionModelClient` | `agent_core.context.compaction_transport` | active | tess-code |
| `CompactionModelSelection` | type | — | `CompactionModelSelection` | — | pending | tess-code |
| `CompactionRunOutcome` | type | — | `CompactionRunOutcome` | — | pending | tess-code |

## Optional per-call host context (2026-09-20)

Python `run_agent_prompt` accepts `ephemeralSystemContext: str` in its argument
object. It appends trusted host-supplied context to this invocation's provider
system prompt, without modifying the session's persisted base prompt or child
launch configuration. It grants no tools or permissions. Omission preserves
existing behavior. The host owns audience selection, budgets and provenance.

`run_agent_prompt` also accepts optional `turnLimits` with positive integer
`maxProviderCalls`, `maxToolCalls` and `timeoutMs`. These limits only tighten
existing inherited execution limits. Omission retains unlimited caller defaults.
Provider-call counts are logical requests; transport retries remain separately
observed. This uses the existing execution-limit checks, not a second turn loop.

Headless hosts may pass `detectWorkspaceIntegrations: false` to
`bootstrap_runtime`. This skips optional desktop/tmux availability probes while
retaining workspace metadata and the engine's normal permissions/tools. Omission
preserves existing detection behavior.

## Durable monitor delivery (2026-09-20 PT)

`TaskMonitorStart` accepts `delivery_mode: notification | continuation`, default
`notification`. Continuation is selected explicitly from the user's requested
follow-up at registration. Hosts advertise `monitorContinuationSupported: true`
in `create_agent_runtime_bindings`; omission/false rejects continuation creation
with `monitor.continuation_unsupported`. Task output never changes the mode.
Automatic continuation does not expose `TaskMonitorStart`, preventing recursive
monitor creation. Existing permissions and tool policies still apply.

`MonitorDeliveryController(runtime_home, clock=None)` pins storage scope. Native
engine session identity is required for mutations; a host must separately verify
its native-to-public mapping. Public async methods:

- `observe(session_id=None)` performs the same atomic observation/outbox pass as
  the daemon. It is safe to race with the daemon; check eligibility is rechecked
  under the monitor record lock.
- `list_pending(session_id=None, limit=100, after=None)` returns bounded descriptors including
  held/pending/claimed/authored/failed state, age timestamps, lease and diagnostics.
  Pass the last descriptor's `cursor` as `after` to page; reset after the last
  page to discover newly created work.
- `claim(monitor_id, session_id, owner, lease_ms=60000, continuation_supported=False)`
  returns a fenced claim or None. `renew(claim, lease_ms=60000)` renews authority.
- `begin_continuation(claim)` durably records the execution boundary before tools
  may run. Expired execution without a checkpoint is marked uncertain and is
  not automatically repeated. Terminal work can supersede failed progress.
- `checkpoint(claim, text, rationale="")` stores authored content. `text=None`
  requires rationale and acknowledges only processed nonterminal progress.
- `publish(claim, publisher)` validates current authority, invokes the idempotent
  durable publisher under the same lock as Stop/Mute, then records its receipt.
  The callback receives bounded payload, must finish within five seconds, and
  must return a JSON receipt with the same `deliveryId`. It must find an existing
  append on replay. It must never run a model or arbitrary network work.
- `publish_to_session(claim, session, config, publisher)` additionally persists
  notification prose in the canonical session, deduplicated by delivery ID. The
  host must serialize this with all other turns using that session.
- `retry(claim, error)` releases the lease with bounded backoff (three attempts).
  Exhausted/uncertain work remains inspectable as failed.
- `retry_failed(monitor_id, session_id)` explicitly requeues safe exhausted work.
  It refuses uncertain tool execution; a new user request is needed for that.
- `set_muted(monitor_id, session_id, muted)` holds delivery independently of task
  status and revokes in-flight publication. Unmute consolidates to the latest
  observation. Interrupted tool execution yields an honest interruption notice,
  not automatic repetition of uncertain actions.
  Coalescing retains superseded execution/checkpoint identity in durable
  `delivery.interruption`, independently of the latest observation. With such
  history, continuation stays paused and claims return a deterministic latest
  status plus the interruption/earlier-checkpoint context. `list_pending()` marks
  `continuationBlocked`; `begin_continuation()` rejects this state. A new explicit
  user follow-up is needed; unmute/retry/restart cannot erase the evidence.
  An unchanged sole authored checkpoint retains its original delivery identity.
- `stop(monitor_id, session_id)` cancels unpublished work without stopping the
  task or retracting durable messages.
- `notification_text(envelope)` produces explicit deterministic, tool-free prose
  for every provider configuration without making a provider call.

Claim tokens stay in host code. Pass only `claim['envelope']` as `monitorEnvelope`
to `run_agent_prompt` with an empty prompt. The engine inserts bounded synthetic
task evidence with no user intent authority, alongside normal context. Budgets
are capped at four provider calls, eight tool calls and 60 seconds, and tighten
any inherited limits. The host must provide an outer service deadline, serialize
the turn, renew/revoke claims, suppress provisional channel output, checkpoint
the final result and publish through the fenced callback.

The local monitor runtime now only observes, matching daemon mode. Without an
explicit consumer, durable work waits; it does not independently mutate a live
session or guess provider credentials. Legacy active monitors are adopted when
next observed; historical terminal records are not replayed. Disk changes are
additive under `MonitorRecord.delivery` and do not change event cardinality into
assistant messages: only the host's receipt path publishes those messages.

## Scheduler (2026-10-07 PT)

A started runtime has `runtime.scheduler`: durable jobs under
`<home>/scheduler/jobs`, and one timer per runtime whose listener can
`publish` (a companion runtime on a shared home never fires). No new root
names; everything is reached through the runtime.

Host API (async unless noted):

- `register(kind, handler)` (sync): the handler for a host job kind; it is
  called as `handler(job, fire)` and may return `{"outcome", "detail",
  "nextFireAt"}` (`nextFireAt`: ISO instant or aware datetime) to name its next
  fire. Jobs of an unregistered kind wait.
- `add(kind="turn"|<host kind>, rule=..., timezone=..., session_id=None,
  label="", instructions="", payload=None, created_by="host"|"owner"|"agent",
  job_id=None)` creates a job; `ensure(job_id, **same)` creates a standing job
  when missing or finished and otherwise leaves it as it is.
- `update(job_id, label=, instructions=, payload=, rule=, timezone=, state=)`,
  `pause`, `resume`, `cancel`, `get`, `list(session_id=None,
  include_finished=False)`; `view(job)` (sync) renders a job in its own zone.
- `status()` (sync, also under `runtime.status()["scheduler"]`): running,
  counts by state, next fire, jobs firing, registered host kinds, last error,
  recent fires.

Rules: `{"kind": "once", "at": "YYYY-MM-DDTHH:MM"}` (local) or `{"kind":
"once", "atUtc": "...Z"}`; `{"kind": "calendar", "time": "HH:MM", "days":
[...]}` or `"monthDays": [...]`; `{"kind": "interval", "everySeconds": N}`.
Zones are IANA names. A one-time local time in a DST gap or overlap is refused
(`scheduler.nonexistent_time`, `scheduler.ambiguous_time`); a recurring one
fires where the zone maps it (after the gap; first of the overlap).

A `turn` job fires as an agent turn in its session with origin `scheduled`
(new in `TURN_ORIGINS`), reopening a closed session with open reason
`scheduled` (new in `OPEN_REASONS`); `Unavailable(retry=True)` defers the
fire, without retry skips it, an ended or unknown session cancels the job.
Listener hooks:

- `publish_schedule_notice(session_id, notice) -> receipt` (optional): a fire
  that could not reach the agent (`reason`: `failed`, `interrupted`,
  `skipped_late`), with the job's own words in `text`. Keep it once per
  `noticeId`.
- `on_runtime_event("scheduler.job_changed", {job_id, session_id, kind, state,
  next_fire_at, reason})` on create, update, cancel and fire;
  `scheduler.job_firing` {session_id, job_id, label, created_by, scheduled_for,
  fired_at, late_seconds, timezone, turn_id} just before a turn job's turn; also
  `scheduler.scan_failed`, `scheduler.fire_failed`, `scheduler.notice_failed`,
  `scheduler.notice_unpublished`.

`SessionOptions.timezone` is the owner's IANA zone, the default for the agent's
scheduler tools (`ScheduleCreate`, `ScheduleList`, `ScheduleUpdate`,
`ScheduleCancel`), which exist only in sessions of a runtime whose timer runs.
Knobs: `scheduler.clock_check_ms` (cadence, default 30000) and
`scheduler.max_late_ms` (bound, unset = none).

## Host worker execution

Python additionally exports `ExecutionBudget` and `ExecutionBudgetExceeded` from `agent_core.runtime.execution_budget`. `SessionOptions.host_tools` injects host-defined `ToolDefinition` instances; ordinary configured allow/deny and child authority still apply. `SessionOptions.execution_budget` shares admission accounting across tools and actual provider transport attempts, including retries. `Session.run_turn(origin="worker")` marks host-authored work as synthetic rather than a new human request. Budget context is ephemeral, never persisted as conversation text.


## Explicit provider registries (Python)

`ProviderRegistry(path)` loads only the explicitly named YAML file (or explicit
`.json` compatibility file). Optional `schema_version` must be 1. Use
`providerProfiles:` (or `profiles:`) as a nonempty mapping of lowercase profile
names to engine profile records (`kind`, `defaultModel`, optional `apiMode`,
`baseUrl`, headers/capabilities/limits). Each profile requires explicit `auth`:
`kind` and `envVar`. Supported transports are `anthropic_api_key`,
`anthropic_auth_token`, OpenAI `openai_api_key` / `openai_api_key_or_codex`,
`custom_bearer`, and `none` (no envVar needed; sends no credential headers).
Explicit no-auth OpenAI profiles use the wire transport; SDK no-auth is rejected
to prevent SDK ambient credential discovery. Codex tokens in strict profiles
come from the declared envVar, never an implicitly discovered auth file.

- `registry.resolve_profile(name, *, model=None)` returns an independent resolved
  engine profile. Missing/unknown names fail with `ProviderRegistryError`.
- `registry.resolve_auth(name, *, environment=None, codex_auth_path=None)` returns transport config
  fields: `apiKey`, `authToken`, or `openaiToken`, plus `customHeaders`. An explicit
  environment mapping (even `{}`) replaces process-environment lookup. Only the
  profile's declared envVar is consulted. An explicit `codex_auth_path` takes
  precedence over environment credentials for OpenAI and fails closed if unreadable.
  Never log the returned credentials.
- `start_runtime(provider_registry=registry_or_path)` pins the registry for all
  sessions. `SessionOptions(provider_registry=..., provider="profile")` supports
  session-local use when the runtime has no pinned registry.
  `SessionOptions(provider_auth_path=...)` (low-level `providerAuthPath`) pins an
  explicit Codex file before auth resolution, without requiring an environment
  token. `openai_api_key_or_codex` may omit envVar when this pin is supplied.
  Low-level
  `resolve_config` uses the dictionary key `providerRegistry`. No registry means
  the existing legacy resolution behavior is retained.
- In strict mode profile names are explicit: no built-ins, provider aliases,
  model-based provider inference, stored credentials, Codex discovery, credential
  helpers, ambient endpoint or provider/model selection. Compaction and verification
  accept explicit `compactionProviderProfile` / `compactionVerificationProviderProfile`
  settings; absent bindings retain the session provider. Registry paths are persisted
  in session metadata for isolated task-runner reconstruction.
- Engine `settings` may name a JSON or YAML file (`settingSources=""` disables discovery, keeps
  explicit settings). Settings YAML is a strict subset: `agent_core.config.parse_yaml_subset` reads it,
  `dump_yaml_subset` writes values that read back identically, else `YamlSubsetError`.
- `await resolve_codex_auth(path)` reads only the explicitly named Codex JSON file
  and returns its API key/access token. It performs no discovery or refresh.
- `await resolve_provider_secret(value, *, purpose="provider_api_key",
  idempotency_scope="provider-auth")` normalizes a supplied string/reference in the
  scoped runtime secret store. OAuth purpose accepts OAuth/setup-token refs without
  losing their purpose/account pinning. It does not discover credentials.

Registry records must not embed credentials: `auth` accepts only `kind` and `envVar` (or a
managed `credentialRef`, below); literal credential fields and Authorization/API-key/cookie
headers are rejected recursively, while non-secret headers such as `anthropic-beta` remain
supported. Registry read, parse, profile and missing-credential errors are redacted. Only
`check_provider_connection` (below) contacts a provider.

## Isolated MCP stdio children (Python)

A stdio server descriptor may specify `inheritEnv: false` to replace the parent's
process environment with its explicit `env` mapping. Default/true retains legacy
inheritance plus overrides. `cwd` selects that server's working directory; absent
it, the connection's workspace is used. These options are validated and rejected
on HTTP/SSE descriptors. Hosts sanitizing memory subprocesses should use an
absolute executable, an explicit environment (including any required PATH),
`inheritEnv: false`, and an explicit `cwd`. Neither option changes other servers.

### Explicit desktop managed credentials (Slice B foundation)

`agent_core.runtime.credentials.CredentialStore`, `CredentialStoreError`, `CredentialBackend`,
`desktop_backend`: RuntimeSecretRef kind/version/purposes plus required pins `backend`,
`installation_id`, `account_id` (UUIDs). Backends `macos_keychain`, `linux_secret_service`
(`desktop-credentials` extra), `encrypted_file` (`encrypted-credentials`: `CredentialStore.encrypted(id,
path)`, AES-256-GCM, locked until `backend.create/unlock(UnlockSecret)`; `open` resolves refs). No default,
auto-selection, plaintext/null fallback or OS call at import (receipts: docs/receipts/epic92/b1-*.json).
`write(account_id, purpose, payload)` makes a NEW immutable ref (API key, OAuth bundle, or a host
channel's `{token}` as `channel_token`); `replace` rewrites only an OAuth bundle in place. `consume` and
`resolve_runtime_secret` are transport-only (repr-redacted, never a browser API); `delete` rejects
in-use refs; `probe()` says available/locked/permission_denied/unavailable; `bounded` caps waits at 5 s.

Registry `auth` takes `credentialRef` (exclusive with `envVar`) for API keys, or kind `chatgpt` with a
`provider_oauth_token` ref (public Responses API only; no WebSocket/incremental capabilities). Refs stay
opaque until `resolve_runtime_secret`, never the legacy file store. Options `webSearch: {provider,
exaApiKeyRef}` give WebSearch a managed Exa key; failures are static `fallback_reason` codes, and
`await agent_core.tools.web_search_api.check_exa_connection(config)` spends one 1-result search.

`agent_core.providers.chatgpt_account` (`chatgpt-account` extra) is OpenAI's open-source Sign in with
ChatGPT: `SignIn.begin(host_id=, app_name=, client_id=, account_id=, id_token_hint=, login_hint=)` binds
127.0.0.1, `.url` is for the owner's browser only, `await .finish()` returns a verified `Grant` (issued
client, `sub`, `plan_use`); `new_host_id()`, `store_grant(store, grant)`, `current_access_token(ref)`
(single-flight refresh; `ChatGPTAccountError.terminal` means sign in again), `revoke(ref)`. Plan requests
drop unsupported fields and a 401 gets one forced refresh; errors are static codes, tokens never logged.

Host imports stay in `engine_session.py`; owner OS-user processes are trusted (no isolation).
`ProviderRegistry.from_document(mapping)` validates staged records (no discovery, I/O or readiness
claim). `await agent_core.providers.connection.check_provider_connection(registry, name)` lists models
(GET, never a completion; a ChatGPT account's own catalog) and returns state/code/model ids only.
