from .agent import *
from .runtime.agent_bindings import *
from .authority.agent import *
from .collaboration import *
from .config import *
from .daemon import *
from .context.durable_memory import *
from .payloads.execution_descriptors import *
from .authority.executable_closure import *
from .authority.executable_config import *
from .authority.executable_source_policy import *
from .acquisition.fetch_proxy import *
from .authority.hooks import *
from .runtime.lifecycle_barrier import *
from .mcp import *
from .tasks.monitors import *
from .platform import *
from .authority.plugin_contracts import *
from .authority.plugins import *
from .authority.permission_rules import *
from .process.identity import *
from .providers.errors import *
from .providers.activity import *
from .collaboration.remote import *
from .authority.repository_trust import *
from .runtime.bootstrap import *
from .runtime.generation import *
from .runtime.paths import *
from .runtime.identifiers import *
from .runtime.secrets import *
from .runtime.events import *
from .runtime.state import *
from .sandbox import *
from .session.persistence import *
from .session.runtime import *
from .shell import *
from .authority.shell_command_policy import *
from .storage import *
from .subagents.capacity import *
from .subagents.continuation import *
from .subagents.coordination import *
from .subagents.continuation_recovery import *
from .subagents.contract import *
from .subagents.depth_policy import *
from .subagents.groups import *
from .subagents.limits import *
from .subagents.reconciliation import *
from .subagents.reconciliation_renderer import *
from .subagents.reconciliation_store import *
from .subagents.result import *
from .subagents import *
from .tasks.ownership import *
from .process.task_launch import *
from .tasks.communication import *
from .tasks.runtime import *
from .tasks.supervisor import *
from .payloads.tools import *
from .tools import *
from .runtime.workspace import *
from .context.compaction import *
from .context.compaction_models import *
from .context.context_policy import *
from .context.continuation import *
from .context.token_accounting import *
from .providers.anthropic import *
from .providers.openai import *

# --- v1 subsystem consolidation --------------------------------------------
# A relocated module keeps its historical ``agent_core.<name>`` import path by
# registering the *same module object* in ``sys.modules`` -- rather than
# re-exporting it through a shim file -- which preserves object identity. So
# importing the legacy path, from-importing through it, its membership in
# ``dir(agent_core)``, and monkeypatching a name onto it all behave as before.
#
# Only paths with a known external consumer are kept; the incidental ones that
# the old star-import layout exposed were retired in the v1 refactor. See
# PUBLIC_API_CHANGES.md for the full list.
_LEGACY_MODULE_ALIASES = {
    "compaction": "agent_core.context.compaction",
    "compaction_models": "agent_core.context.compaction_models",
    "context_policy": "agent_core.context.context_policy",
    "continuation": "agent_core.context.continuation",
    "durable_memory": "agent_core.context.durable_memory",
    "monitors": "agent_core.tasks.monitors",
    "process_identity": "agent_core.process.identity",
    "provider_watchdog": "agent_core.providers.watchdog",
    "subagent_coordination": "agent_core.subagents.coordination",
    "task_process_launch": "agent_core.process.task_launch",
    "task_runtime": "agent_core.tasks.runtime",
    "task_supervisor": "agent_core.tasks.supervisor",
    "token_accounting": "agent_core.context.token_accounting",
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
    # Subsystem packages introduced by the v1 consolidation. Importing a
    # submodule binds the package name here, but these had no same-named
    # top-level module before, so they are not part of the public surface.
    "acquisition",
    "authority",
    "payloads",
    "process",
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
