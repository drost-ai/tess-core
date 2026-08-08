from __future__ import annotations

import re
from typing import Any

from .providers import normalizeModelName, resolveProviderForModelSelection
from .shared import DEFAULT_ASSISTANT_DESCRIPTION, DEFAULT_ASSISTANT_NAME

def resolveAgentDefinition(config: dict[str, Any], agentName: str | None) -> dict[str, Any] | None:
    return None if not agentName else config.get("agents", {}).get(agentName)

def buildEffectiveSystemPrompt(config: dict[str, Any]) -> str:
    if not config.get("appendSystemPrompt"):
        return config["systemPrompt"]
    return f"{config['systemPrompt']}\n\n{config['appendSystemPrompt']}"

def buildDefaultSystemPrompt(args: dict[str, Any] | None = None, **kwargs: Any) -> str:
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

def getDefaultSystemPrompt() -> str:
    return buildDefaultSystemPrompt()

def buildSystemPromptForAgent(config: dict[str, Any], agentName: str | None, fallbackSystemPrompt: str | None = None) -> str:
    fallback = fallbackSystemPrompt or buildEffectiveSystemPrompt(config)
    definition = resolveAgentDefinition(config, agentName)
    if not definition or not definition.get("prompt"):
        return fallback
    return f"{fallback}\n\n# Agent Instructions\n{definition['prompt']}"

def resolveModelForAgent(config: dict[str, Any], agentName: str | None, fallbackModel: str | None = None) -> str:
    definition = resolveAgentDefinition(config, agentName)
    model = (definition or {}).get("model") or fallbackModel or config["model"]
    return normalizeModelName(model, resolveProviderForModelSelection(config.get("provider"), model))

def isPlanRevisionPrompt(prompt: str) -> bool:
    normalized = prompt.strip().lower()
    if not normalized:
        return False
    patterns = [
        r"\bupdate (the )?plan\b",
        r"\brevise (the )?plan\b",
        r"\bmodify (the )?plan\b",
        r"\bregenerate (the )?plan\b",
        r"\brefresh (the )?plan\b",
        r"\bnew plan\b",
        r"\bfinali[sz]e (the )?plan\b",
        r"\bshow (me )?(the )?updated plan\b",
        r"\bproceed with implementation\b",
        r"\bgo ahead and (build|implement|start)\b",
        r"\bstart (building|implementing)\b",
        r"\buse the original (plan|document|doc)\b",
        r"\buse .* as (the )?(spec|reference|implementation guide)\b",
    ]
    return any(re.search(pattern, normalized) for pattern in patterns)

def filterToolsByPolicy(config: dict[str, Any]) -> list[str]:
    allow = set(config.get("allowedTools") or [])
    deny = set(config.get("disallowedTools") or [])
    return [tool for tool in config.get("tools", []) if tool not in deny and (not allow or tool in allow)]

def getDefaultToolList() -> list[str]:
    return defaultToolList()

def defaultToolList() -> list[str]:
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
        "ExitPlanMode",
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
