from __future__ import annotations

from typing import Any

from .providers import normalize_model_name, resolve_provider_for_model_selection
from .shared import DEFAULT_ASSISTANT_DESCRIPTION, DEFAULT_ASSISTANT_NAME

def resolve_agent_definition(config: dict[str, Any], agentName: str | None) -> dict[str, Any] | None:
    return None if not agentName else config.get("agents", {}).get(agentName)

def build_effective_system_prompt(config: dict[str, Any]) -> str:
    if not config.get("appendSystemPrompt"):
        return config["systemPrompt"]
    return f"{config['systemPrompt']}\n\n{config['appendSystemPrompt']}"

def build_default_system_prompt(args: dict[str, Any] | None = None, **kwargs: Any) -> str:
    args = {**(args or {}), **kwargs}
    assistant_name = (args.get("assistantName") or DEFAULT_ASSISTANT_NAME).strip()
    assistant_description = (args.get("assistantDescription") or DEFAULT_ASSISTANT_DESCRIPTION).strip()
    return "\n".join(
        [
            f"You are {assistant_name}, {assistant_description}.",
            "Work directly in the user's codebase.",
            "Prefer concrete actions over long explanations.",
            "Use tools when needed, keep edits minimal, and preserve user intent.",
            "Before modifying files, inspect the relevant code and respect the existing style.",
            "Use SetStatus to keep the live UI updated when you change phases or begin a meaningful chunk of work. Keep statuses brief and concrete, for example: 'Reading plan...', 'Searching code...', 'Updating plan...', or 'Running tests...'.",
        ]
    )

def get_default_system_prompt() -> str:
    return build_default_system_prompt()

def build_system_prompt_for_agent(config: dict[str, Any], agentName: str | None, fallbackSystemPrompt: str | None = None) -> str:
    fallback = fallbackSystemPrompt or build_effective_system_prompt(config)
    definition = resolve_agent_definition(config, agentName)
    if not definition or not definition.get("prompt"):
        return fallback
    return f"{fallback}\n\n# Agent Instructions\n{definition['prompt']}"

def resolve_model_for_agent(config: dict[str, Any], agentName: str | None, fallbackModel: str | None = None) -> str:
    definition = resolve_agent_definition(config, agentName)
    model = (definition or {}).get("model") or fallbackModel or config["model"]
    return normalize_model_name(model, resolve_provider_for_model_selection(config.get("provider"), model))

def filter_tools_by_policy(config: dict[str, Any]) -> list[str]:
    allow = set(config.get("allowedTools") or [])
    deny = set(config.get("disallowedTools") or [])
    return [tool for tool in config.get("tools", []) if tool not in deny and (not allow or tool in allow)]

def get_default_tool_list() -> list[str]:
    return default_tool_list()

def default_tool_list() -> list[str]:
    return [
        "Bash",
        "Read",
        "Write",
        "Edit",
        "Glob",
        "Grep",
        "TodoWrite",
        "WebFetch",
        "WebSearch",
        "SetStatus",
        "Agent",
        "SendMessage",
        "AskUserQuestion",
        "Config",
        "NotebookEdit",
        "TaskOutput",
        "TaskStop",
        "TaskMonitorStart",
        "TaskMonitorStop",
        "Mcp",
        "ListMcpResources",
        "ReadMcpResource",
        "EnterWorktree",
        "ExitWorktree",
        "ReadToolArtifact",
    ]
