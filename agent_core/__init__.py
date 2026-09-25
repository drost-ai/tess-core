"""agent-core: a dependency-free agent runtime.

The public API. Every exported name is a row in docs/public-api-contract.md,
the cross-runtime contract shared with the TypeScript and Rust runtimes;
tests/test_refactor_guardrails.py enforces exact membership. Runtime
internals live in the subsystem packages and may change freely.
"""
from __future__ import annotations

from .agent import readOpenChildQuestions
from .agent.loop import run_agent_prompt
from .config.prompts import build_system_prompt_for_agent, resolve_model_for_agent
from .config.providers import resolve_provider_profile, resolve_provider_profiles
from .config.resolution import resolve_config
from .contracts.config import ResolvedConfig, RuntimeSecretRef
from .contracts.context import AskQuestionDetail
from .contracts.config import PermissionMode, RuntimeSecretPurpose, RuntimeSecretValue
from .contracts.events import RuntimeEventEnvelope
from .contracts.providers import AnthropicMessage, AnthropicUsage
from .contracts.runtime_surface import BootstrappedRuntime
from .contracts.session_records import SessionRecord
from .contracts.task_records import ChildAgentRecord
from .daemon.lifecycle import connect_task_supervisor_daemon_client, ensure_task_supervisor_daemon
from .daemon.server import run_task_supervisor_daemon
from .remote.bridge_admission import send_bridge_message
from .remote.bridge_diagnostics import format_bridge_tail, list_active_bridge_rows
from .remote.bridge_runtime import create_bridge_runtime
from .remote.sessions import create_remote_session, update_remote_session
from .runtime.agent_bindings import create_agent_runtime_bindings, is_task_supervisor_gateway
from .runtime.bootstrap import bootstrap_runtime
from .runtime.events.factory import create_runtime_event
from .runtime.generation import RuntimeActivationContext, RuntimeSessionBinding, create_runtime_session_binding
from .runtime.host import Runtime, start_runtime
from .runtime.owner_lease import RuntimeOwnershipError
from .runtime.session_manager import (
    Session, SessionBusy, SessionEnded, SessionManager, SessionOptions, SessionUnavailableError, Unavailable, UnknownSessionError,
)
from .runtime.state.store import create_initial_runtime_state, create_runtime_state_store
from .runtime.wake import create_mailbox_wake_controller
from .runtime.workspace import create_git_worktree
from .session.persistence import find_sessions_for_workspace, persist_session, resolve_session
from .session.runtime import active_session_payload
from .shell_surface.diagnostics import format_session_candidate
from .shell_surface.remote_select import resolve_teleport_session
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
from .subagents.finalizers import finalize_background_subagent_failure_from_env, finalize_background_subagent_success_from_env
from .tasks.collaboration import create_collaboration_runtime
from .tasks.monitors import create_monitor_record
from .tasks.monitor_delivery import MonitorDeliveryController, MonitorDeliveryError
from .tools.definitions import ToolDefinition
from .tools.registry import create_builtin_tools

__all__ = [
    "MonitorDeliveryController",
    "MonitorDeliveryError",
    "RuntimeSecretValue",
    "RuntimeSecretPurpose",
    "RuntimeEventEnvelope",
    "PermissionMode",
    "AnthropicMessage",
    "AnthropicUsage",
    "BootstrappedRuntime",
    "active_session_payload",
    "AskQuestionDetail",
    "bootstrap_runtime",
    "build_system_prompt_for_agent",
    "ChildAgentRecord",
    "clear_auth",
    "configure_runtime_home",
    "connect_task_supervisor_daemon_client",
    "create_empty_session",
    "create_agent_runtime_bindings",
    "create_bridge_runtime",
    "create_builtin_tools",
    "create_collaboration_runtime",
    "create_git_worktree",
    "create_initial_runtime_state",
    "create_mailbox_wake_controller",
    "create_monitor_record",
    "create_remote_session",
    "create_runtime_event",
    "create_runtime_session_binding",
    "create_runtime_state_store",
    "ensure_base_layout",
    "ensure_task_supervisor_daemon",
    "finalize_background_subagent_failure_from_env",
    "finalize_background_subagent_success_from_env",
    "find_git_root",
    "find_sessions_for_workspace",
    "format_bridge_tail",
    "format_session_candidate",
    "get_default_auto_mode",
    "get_state_root",
    "is_task_supervisor_gateway",
    "list_child_agents",
    "list_sessions",
    "list_active_bridge_rows",
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
    "resolve_config",
    "ResolvedConfig",
    "resolve_model_for_agent",
    "resolve_provider_profile",
    "resolve_provider_profiles",
    "resolve_session",
    "resolve_teleport_session",
    "run_agent_prompt",
    "run_task_supervisor_daemon",
    "RuntimeActivationContext",
    "Runtime",
    "RuntimeOwnershipError",
    "start_runtime",
    "Session",
    "SessionBusy",
    "SessionEnded",
    "SessionManager",
    "SessionOptions",
    "SessionUnavailableError",
    "Unavailable",
    "UnknownSessionError",
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
    "send_bridge_message",
    "SessionRecord",
    "ToolDefinition",
    "update_remote_session",
]
