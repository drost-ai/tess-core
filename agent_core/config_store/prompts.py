from __future__ import annotations

from .shared import *

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

def buildPlanModeSystemPrompt(basePrompt: str, args: dict[str, Any] | None = None, **kwargs: Any) -> str:
    args = {**(args or {}), **kwargs}
    state = args.get("currentState") or "inactive"
    prior_plan = (args.get("priorPlan") or "").strip()
    overlay = ["# Plan Mode", "You are in plan mode.", "Do not implement the change yet."]
    if state == "chatting":
        overlay.extend(
            [
                "There is an existing draft plan under discussion.",
                "Answer the user's follow-up questions directly and conversationally while staying in plan mode.",
                "Do not start implementation while plan mode is still active.",
                "Do not use implementation tools, create implementation todos, or describe implementation as already underway until plan mode has been exited.",
                "If the user asks you to proceed, go ahead, use a specific document as the implementation reference, or otherwise indicates the planning discussion is complete, treat that as a request to update/finalize the plan and then call ExitPlanMode.",
                "If the user explicitly asks for a plan revision, update the plan in structured markdown and then call ExitPlanMode.",
            ]
        )
    else:
        overlay.extend(
            [
                "Investigate the user's request and produce a concrete implementation plan before any implementation work begins.",
                "Use read-only investigation and subagents as needed to understand the codebase and scope of work.",
                "Respond with a structured markdown plan that includes:",
                "1. Objective",
                "2. Findings",
                "3. Proposed changes",
                "4. Ordered implementation steps",
                "5. Validation strategy",
                "6. Risks or open questions",
                "When the plan is ready, call ExitPlanMode with allowedPrompts describing the categories of Bash actions expected during implementation.",
                "Do not ask for plan approval in plain text; use ExitPlanMode.",
            ]
        )
    if state in ("chatting", "rejected") and prior_plan:
        overlay.extend(["", "The previous plan was rejected. Revise it and submit an improved plan." if state == "rejected" else "This is the current draft plan being discussed.", "", "## Current Draft Plan", prior_plan])
    return f"{basePrompt}\n\n" + "\n".join(overlay)

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
    ]
