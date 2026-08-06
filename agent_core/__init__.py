from .agent import *
from .agent_runtime_bindings import *
from .agent_authority import *
from .collaboration import *
from .config import *
from .daemon import *
from .durable_memory import *
from .execution_descriptors import *
from .executable_closure import *
from .executable_config import *
from .executable_source_policy import *
from .fetch_proxy import *
from .hooks import *
from .lifecycle_barrier import *
from .mcp import *
from .monitors import *
from .platform import *
from .plugin_contracts import *
from .plugins import *
from .permission_rules import *
from .process_identity import *
from .provider_errors import *
from .provider_activity import *
from .collaboration.remote import *
from .repository_trust import *
from .runtime import *
from .runtime_generation import *
from .runtime_paths import *
from .runtime_identifiers import *
from .runtime_secrets import *
from .runtime_events import *
from .runtime_state import *
from .sandbox import *
from .session import *
from .session_runtime import *
from .shell import *
from .shell_command_policy import *
from .storage import *
from .subagent_capacity import *
from .subagent_continuation import *
from .subagent_coordination import *
from .subagent_continuation_recovery import *
from .subagent_contract import *
from .subagent_depth_policy import *
from .subagent_groups import *
from .subagent_limits import *
from .subagent_reconciliation import *
from .subagent_reconciliation_renderer import *
from .subagent_reconciliation_store import *
from .subagent_result import *
from .subagents import *
from .task_ownership import *
from .task_process_launch import *
from .task_communication import *
from .task_runtime import *
from .task_supervisor import *
from .tool_payloads import *
from .tools import *
from .workspace import *
from .compaction import *
from .compaction_models import *
from .context_policy import *
from .continuation import *
from .token_accounting import *
from .providers.anthropic import *
from .providers.openai import *

# --- v1 subsystem consolidation --------------------------------------------
# Modules relocated into subsystem packages keep their historical
# ``agent_core.<name>`` import path. Registering the *same module object* in
# ``sys.modules`` -- rather than re-exporting through a shim file -- preserves
# object identity, so ``import agent_core.remote``,
# ``from agent_core.remote import x``, membership in ``dir(agent_core)``, and
# monkeypatching a name onto the legacy path all behave exactly as before.
_LEGACY_MODULE_ALIASES = {
    "collaboration_updates": "agent_core.collaboration.updates",
    "remote": "agent_core.collaboration.remote",
}


def _install_legacy_module_aliases() -> None:
    import importlib
    import sys

    for alias, target in _LEGACY_MODULE_ALIASES.items():
        module = importlib.import_module(target)
        sys.modules[f"{__name__}.{alias}"] = module
        globals()[alias] = module


_install_legacy_module_aliases()

_INTERNAL_MODULES = {
    "agent_tool_results",
    "bash_lifecycle",
    "bash_result_payload",
    "bridge_admission",
    "bridge_generation",
    "bridge_recovery",
    "bridge_recovery_analysis",
    "bridge_runtime",
    "bounded_io",
    "bounded_web_fetch",
    "exception_groups",
    "image_budget",
    "mcp_payloads",
    "message_origin",
    "notebook_payload",
    "payload_cursor",
    "partial_output",
    "partial_output_state",
    "persistence_metrics",
    "prompt_cache_state",
    "prompt_cache_state_schema",
    "process_identity_darwin",
    "provider_activity",
    "provider_error_diagnostics",
    "provider_prompt_cache",
    "provider_turn_session",
    "provider_error_sdk",
    "search_acquisition",
    "search_result_payload",
    "structured_result_policy",
    "structured_value",
    "subagent_result_payload",
    "subprocess_output",
    "text_file_window",
    "tool_attachments",
    "todo_policy",
    "tool_result_guard",
    "usage_costs",
    "utf8_bounds",
}

__all__ = sorted(
    name for name in globals()
    if not name.startswith("_") and name != "headless" and name not in _INTERNAL_MODULES
)


def __dir__():
    return sorted(
        name for name in globals()
        if not name.startswith("_") and name != "headless" and name not in _INTERNAL_MODULES
    )
