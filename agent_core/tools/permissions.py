from __future__ import annotations

from ..contracts.context import ToolContext

import re
from typing import Any

from .._utils import maybe_await
from ..storage import loadAutoMode, loadSession


PERMISSION_MODES = {"default", "acceptEdits", "bypassPermissions", "dontAsk", "auto", "plan"}


async def _ensure_tool_permission(context: ToolContext, tool_name: str, category: str, summary: str, always_ask: bool = False, command: str | None = None) -> None:
    mode = await _get_effective_permission_mode(context)
    resolution = await _resolve_tool_permission(context, tool_name, category, mode, always_ask, command)
    output_json = context.get("outputJson", lambda *_: None)
    if resolution["decision"] == "allow":
        output_json("permission.decided", {"tool": tool_name, "mode": mode, "decision": "allow", "summary": summary, "source": resolution.get("source")})
        return

    if resolution["decision"] == "deny":
        output_json("permission.decided", {"tool": tool_name, "mode": mode, "decision": "deny", "summary": summary, "source": resolution.get("source"), "reason": resolution.get("reason")})
        raise PermissionError(resolution.get("reason") or f"Permission denied for {tool_name}.")

    if not context.get("interactive"):
        switch = resolution.get("switchMode") or "bypassPermissions"
        output_json(
            "permission.decided",
            {
                "tool": tool_name,
                "mode": mode,
                "decision": "deny",
                "summary": summary,
                "source": "non_interactive",
                "reason": resolution.get("reason") or f"{tool_name} requires approval in {mode} mode.",
            },
        )
        raise PermissionError(f"{tool_name} requires approval in {mode} mode. Re-run with --permission-mode {switch} or switch modes interactively with /permissions.")

    approve_label = "Approve once"
    switch_label = f"Switch to {resolution['switchMode']}" if resolution.get("switchMode") else None
    deny_label = "Deny"
    options = [label for label in (approve_label, switch_label, deny_label) if label]
    prompt = "\n".join(part for part in (f"{tool_name} wants permission", "", summary, "", f"mode: {mode}", f"policy: {resolution.get('reason')}" if resolution.get("reason") else "") if part is not None)
    output_json("permission.requested", {"tool": tool_name, "mode": mode, "summary": summary, "options": options})
    detail = {
        "kind": "permission",
        "tool": tool_name,
        "mode": mode,
        "summary": summary,
        "reason": resolution.get("reason"),
        "category": category,
    }
    answer = await _ask_question_with_detail(context, prompt, options, detail, deny_label)
    if answer == approve_label:
        output_json("permission.decided", {"tool": tool_name, "mode": mode, "decision": "allow", "summary": summary, "source": "prompt"})
        return
    if switch_label and answer == switch_label:
        context["config"]["permissionMode"] = resolution["switchMode"]
        context["session"]["permissionMode"] = resolution["switchMode"]
        await maybe_await(context.get("persistSession", lambda: None)())
        output_json("permission.decided", {"tool": tool_name, "mode": resolution["switchMode"], "decision": "allow", "summary": summary, "source": "prompt-switch"})
        return
    output_json("permission.decided", {"tool": tool_name, "mode": mode, "decision": "deny", "summary": summary, "source": "prompt"})
    raise PermissionError(f"Permission denied for {tool_name}. Use /permissions to change modes.")


async def _get_effective_permission_mode(context: ToolContext) -> str:
    mode = context["session"].get("permissionMode") or context["config"].get("permissionMode", "default")
    parent_session_id = context["session"].get("metadata", {}).get("parentSessionId") if context.get("interactive") else None
    if not parent_session_id:
        return mode
    parent = await loadSession(parent_session_id)
    inherited = parent.get("permissionMode") if parent else None
    if not inherited or inherited == mode:
        return mode
    context["session"]["permissionMode"] = inherited
    context["config"]["permissionMode"] = inherited
    await maybe_await(context.get("persistSession", lambda: None)())
    return inherited


async def _resolve_tool_permission(context: ToolContext, tool_name: str, category: str, mode: str, always_ask: bool, command: str | None) -> dict[str, str]:
    if mode == "bypassPermissions":
        return {"decision": "allow", "source": "mode"}
    if mode == "plan":
        return {"decision": "deny", "source": "mode", "reason": f"{tool_name} is blocked in plan mode. Switch to /permissions default or /permissions acceptEdits to execute changes."}
    if category == "edit":
        if mode in ("acceptEdits", "auto", "dontAsk"):
            return {"decision": "allow", "source": "mode"}
        return {"decision": "ask", "source": "mode", "switchMode": "acceptEdits", "reason": "This tool changes files on disk."}
    if category == "worktree":
        if mode in ("auto", "dontAsk"):
            return {"decision": "deny", "source": "mode", "reason": "Worktree creation is blocked in non-interactive permission modes."}
        return {"decision": "ask", "source": "mode", "switchMode": "bypassPermissions", "reason": "This tool creates or changes repository state."}
    if category == "process":
        if mode in ("auto", "dontAsk"):
            return {"decision": "deny", "source": "mode", "reason": "Stopping processes is blocked in non-interactive permission modes."}
        return {"decision": "ask", "source": "mode", "switchMode": "bypassPermissions", "reason": "This tool sends signals to a running process."}

    command = command or ""
    if not always_ask and _is_safe_read_only_shell_command(command):
        return {"decision": "allow", "source": "heuristic"}
    if mode in ("auto", "dontAsk"):
        auto_mode = await loadAutoMode(context["session"]["cwd"])
        if _matches_command_policy(command, auto_mode.get("deny") or []):
            return {"decision": "deny", "source": "auto-mode", "reason": "Command matched the deny policy for auto mode."}
        if not always_ask and _matches_command_policy(command, auto_mode.get("allow") or []):
            return {"decision": "allow", "source": "auto-mode"}
        return {"decision": "deny", "source": "auto-mode", "reason": "Command is not in the allow policy for auto mode."}
    return {"decision": "ask", "source": "mode", "switchMode": "bypassPermissions", "reason": "This shell command can modify the environment or repository."}


def _parse_permission_mode_value(value: Any) -> str | None:
    return value if isinstance(value, str) and value in PERMISSION_MODES else None


def _is_safe_read_only_shell_command(command: str) -> bool:
    normalized = _normalize_shell_command(command)
    if not normalized:
        return False
    safe_prefixes = [
        "git status",
        "git diff",
        "git log",
        "git show",
        "git branch",
        "ls",
        "pwd",
        "find",
        "rg",
        "cat",
        "head",
        "tail",
        "sed -n",
        "wc",
        "which",
        "date",
        "npm test",
        "pnpm test",
        "bun test",
        "pytest",
        "cargo test",
        "go test",
    ]
    risky_fragments = [" rm ", " rm-", "rm -", "git reset", "git clean", "chmod ", "chown ", "sudo ", "curl ", "wget ", ">|", " >", ">>", "tee "]
    if any(fragment in normalized for fragment in risky_fragments):
        return False
    return any(normalized == prefix or normalized.startswith(f"{prefix} ") for prefix in safe_prefixes)


def _matches_command_policy(command: str, patterns: list[str]) -> bool:
    normalized = _normalize_shell_command(command)
    for pattern in patterns:
        normalized_pattern = _normalize_shell_command(pattern)
        if normalized == normalized_pattern or normalized.startswith(f"{normalized_pattern} "):
            return True
    return False


def _normalize_shell_command(command: str) -> str:
    return re.sub(r"\s+", " ", command).strip().lower()


async def _ask_question_with_detail(context: ToolContext, prompt: str, options: list[str], detail: dict[str, Any], fallback: str) -> str:
    ask = context.get("askQuestion", lambda *_: fallback)
    try:
        return await maybe_await(ask(prompt, options, detail))
    except TypeError:
        return await maybe_await(ask(prompt, options))
