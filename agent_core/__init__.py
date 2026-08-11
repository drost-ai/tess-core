"""agent-core: a dependency-free agent runtime.

The public API. Every exported name is a row in docs/public-api-contract.md,
the cross-runtime contract shared with the TypeScript and Rust runtimes;
tests/test_refactor_guardrails.py enforces exact membership. Runtime
internals live in the subsystem packages and may change freely.
"""
from __future__ import annotations

from .agent import readOpenChildQuestions
from .agent.loop import runAgentPrompt
from .config.prompts import buildSystemPromptForAgent, resolveModelForAgent
from .config.providers import resolveProviderProfile, resolveProviderProfiles
from .config.resolution import resolveConfig
from .contracts.config import ResolvedConfig, RuntimeSecretRef
from .contracts.context import AskQuestionDetail
from .contracts.session_records import SessionRecord
from .contracts.task_records import ChildAgentRecord
from .daemon.lifecycle import connectTaskSupervisorDaemonClient, ensureTaskSupervisorDaemon
from .daemon.server import runTaskSupervisorDaemon
from .remote.bridge_admission import sendBridgeMessage
from .remote.bridge_diagnostics import formatBridgeTail, listActiveBridgeRows
from .remote.bridge_runtime import createBridgeRuntime
from .remote.sessions import createRemoteSession, updateRemoteSession
from .runtime.agent_bindings import createAgentRuntimeBindings, isTaskSupervisorGateway
from .runtime.bootstrap import bootstrapRuntime
from .runtime.events.factory import createRuntimeEvent
from .runtime.generation import RuntimeActivationContext, RuntimeSessionBinding, createRuntimeSessionBinding
from .runtime.state.store import createInitialRuntimeState, createRuntimeStateStore
from .runtime.wake import createMailboxWakeController
from .runtime.workspace import createGitWorktree
from .session.persistence import find_sessions_for_workspace, persist_session, resolve_session
from .session.runtime import active_session_payload
from .shell_surface.diagnostics import format_session_candidate
from .shell_surface.remote_select import resolveTeleportSession
from .storage.auth_files import clear_auth, load_auth, save_auth
from .storage.paths import (
    configure_runtime_home, ensure_base_layout, find_git_root, get_state_root, reset_configured_runtime_home,
)
from .storage.records import list_child_agents, save_child_agent, save_monitor
from .storage.remote import load_bridge, load_bridge_events, save_bridge
from .storage.session_archive import load_session
from .storage.sessions import create_empty_session, list_sessions, save_session
from .storage.settings import (
    get_default_auto_mode, load_auto_mode, load_marketplaces, load_mcp_servers, load_plugins, save_marketplaces, save_mcp_servers, save_plugins,
)
from .subagents.finalizers import finalizeBackgroundSubagentFailureFromEnv, finalizeBackgroundSubagentSuccessFromEnv
from .tasks.collaboration import createCollaborationRuntime
from .tasks.monitors import createMonitorRecord
from .tools.definitions import ToolDefinition
from .tools.registry import createBuiltinTools

__all__ = [
    "active_session_payload",
    "AskQuestionDetail",
    "bootstrapRuntime",
    "buildSystemPromptForAgent",
    "ChildAgentRecord",
    "clear_auth",
    "configure_runtime_home",
    "connectTaskSupervisorDaemonClient",
    "create_empty_session",
    "createAgentRuntimeBindings",
    "createBridgeRuntime",
    "createBuiltinTools",
    "createCollaborationRuntime",
    "createGitWorktree",
    "createInitialRuntimeState",
    "createMailboxWakeController",
    "createMonitorRecord",
    "createRemoteSession",
    "createRuntimeEvent",
    "createRuntimeSessionBinding",
    "createRuntimeStateStore",
    "ensure_base_layout",
    "ensureTaskSupervisorDaemon",
    "finalizeBackgroundSubagentFailureFromEnv",
    "finalizeBackgroundSubagentSuccessFromEnv",
    "find_git_root",
    "find_sessions_for_workspace",
    "formatBridgeTail",
    "format_session_candidate",
    "get_default_auto_mode",
    "get_state_root",
    "isTaskSupervisorGateway",
    "list_child_agents",
    "list_sessions",
    "listActiveBridgeRows",
    "load_auth",
    "load_auto_mode",
    "load_bridge",
    "load_bridge_events",
    "load_marketplaces",
    "load_mcp_servers",
    "load_plugins",
    "load_session",
    "persist_session",
    "readOpenChildQuestions",
    "reset_configured_runtime_home",
    "resolveConfig",
    "ResolvedConfig",
    "resolveModelForAgent",
    "resolveProviderProfile",
    "resolveProviderProfiles",
    "resolve_session",
    "resolveTeleportSession",
    "runAgentPrompt",
    "runTaskSupervisorDaemon",
    "RuntimeActivationContext",
    "RuntimeSecretRef",
    "RuntimeSessionBinding",
    "save_auth",
    "save_bridge",
    "save_child_agent",
    "save_marketplaces",
    "save_mcp_servers",
    "save_monitor",
    "save_plugins",
    "save_session",
    "sendBridgeMessage",
    "SessionRecord",
    "ToolDefinition",
    "updateRemoteSession",
]
