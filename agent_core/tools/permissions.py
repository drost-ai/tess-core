from __future__ import annotations

from ..contracts.context import ToolContext

import re
from typing import Any

from ..authority.agent import AgentAuthorityError, assertValidAgentAuthority
from .._utils import maybe_await
from ..authority.permission_rules import loadPermissionRulesCached, matchPermissionRule
from ..authority.shell_command_policy import classifyReadOnlyShellCommand, parseSingleShellCommand
from ..storage import getProjectRoot, loadAutoMode


PERMISSION_MODES = {"default", "acceptEdits", "bypassPermissions", "dontAsk", "auto"}


def _pinned_child_authority(session: dict[str, Any]) -> dict[str, Any] | None:
    """Authority pins CHILD sessions only. Spawning stamps the same record on
    the parent as derivation bookkeeping, but the interactive root keeps its
    mode freedom — the human at the prompt IS the authority there."""
    metadata = session.get("metadata") if isinstance(session.get("metadata"), dict) else {}
    if not metadata.get("parentSessionId"):
        return None
    return metadata.get("effectiveAgentAuthority")


async def _ensure_tool_permission(
    context: ToolContext,
    tool_name: str,
    category: str,
    summary: str,
    always_ask: bool = False,
    command: str | None = None,
    path: str | None = None,
    url: str | None = None,
) -> None:
    mode = await _get_effective_permission_mode(context)
    resolution = await _resolve_tool_permission(
        context,
        tool_name,
        category,
        mode,
        always_ask,
        command,
        path,
        url,
    )
    authority = _pinned_child_authority(context.get("session", {}))
    if authority and resolution["decision"] == "ask":
        resolution.pop("switchMode", None)
    output_json = context.get("outputJson", lambda *_: None)
    if resolution["decision"] == "allow":
        output_json("permission.decided", {"tool": tool_name, "mode": mode, "decision": "allow", "summary": summary, "source": resolution.get("source")})
        return

    if resolution["decision"] == "deny":
        output_json("permission.decided", {"tool": tool_name, "mode": mode, "decision": "deny", "summary": summary, "source": resolution.get("source"), "reason": resolution.get("reason")})
        raise PermissionError(resolution.get("reason") or f"Permission denied for {tool_name}.")

    if not context.get("interactive"):
        if authority:
            raise PermissionError(
                f"{tool_name} requires an approval that this detached child cannot obtain. "
                "Child authority is persisted and cannot be widened by relaunching with another permission mode."
            )
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


async def _ensure_path_confinement(context: ToolContext, tool_name: str, path: str) -> None:
    """A tool asked for a path outside the session's allowed roots. Prompting
    modes surface an approval; pinned children, containment modes
    (bypassPermissions, an os-required sandbox), and non-interactive auto
    modes refuse outright — no human gate exists there to widen filesystem
    authority, so confinement must be structural."""
    mode = await _get_effective_permission_mode(context)
    output_json = context.get("outputJson", lambda *_: None)
    summary = f"{tool_name} {path}"
    outside = f"{tool_name} path {path} is outside this session's allowed roots"

    def refuse(reason: str, source: str) -> None:
        output_json("permission.decided", {"tool": tool_name, "mode": mode, "decision": "deny", "summary": summary, "source": source, "reason": reason})
        raise PermissionError(reason)

    authority = _pinned_child_authority(context.get("session", {}))
    if authority:
        refuse(f"{outside}; child authority cannot be widened after launch.", "authority")
    sandbox = context.get("config", {}).get("sandbox") or {}
    if mode == "bypassPermissions" or sandbox.get("mode") == "os-required":
        refuse(f"{outside}; containment sessions cannot widen filesystem access. Launch with --add-dir to include it.", "containment")
    if mode in ("auto", "dontAsk"):
        refuse(f"{outside}; non-interactive modes cannot approve new roots.", "mode")
    if not context.get("interactive"):
        refuse(f"{outside}. Re-run with --add-dir {path} or approve interactively.", "non_interactive")
    approve_label = "Approve once"
    deny_label = "Deny"
    options = [approve_label, deny_label]
    prompt = "\n".join((f"{tool_name} wants to access a path outside the allowed roots", "", path, "", f"mode: {mode}"))
    output_json("permission.requested", {"tool": tool_name, "mode": mode, "summary": summary, "options": options})
    detail = {
        "kind": "permission",
        "tool": tool_name,
        "mode": mode,
        "summary": summary,
        "reason": "Path is outside the session's allowed roots.",
        "category": "path",
    }
    answer = await _ask_question_with_detail(context, prompt, options, detail, deny_label)
    if answer == approve_label:
        output_json("permission.decided", {"tool": tool_name, "mode": mode, "decision": "allow", "summary": summary, "source": "prompt"})
        return
    refuse(f"Permission denied for {tool_name} on {path}.", "prompt")


async def _get_effective_permission_mode(context: ToolContext) -> str:
    mode = context["session"].get("permissionMode") or context["config"].get("permissionMode", "default")
    metadata = context["session"].get("metadata", {})
    authority = _pinned_child_authority(context["session"])
    if authority:
        assertValidAgentAuthority(authority)
        if mode != authority["mode"]:
            context["session"]["permissionMode"] = authority["mode"]
            context["config"]["permissionMode"] = authority["mode"]
            await maybe_await(context.get("persistSession", lambda: None)())
        return authority["mode"]
    if not metadata.get("parentSessionId"):
        return mode
    raise AgentAuthorityError(
        "child_authority_missing",
        "record",
        "Legacy child session has no persisted effective authority; tool execution is blocked.",
    )


async def _resolve_tool_permission(
    context: ToolContext,
    tool_name: str,
    category: str,
    mode: str,
    always_ask: bool,
    command: str | None,
    path: str | None = None,
    url: str | None = None,
) -> dict[str, Any]:
    authority = _pinned_child_authority(context.get("session", {}))
    rules = (authority or {}).get("permissionRules")
    if rules is None:
        rules = await loadPermissionRulesCached(
            cwd=context["session"]["cwd"],
            sources=context.get("config", {}).get("settingsSources"),
            inlineSettings=context.get("config", {}).get("inlineSettings"),
        )
    call = {"toolName": tool_name, "command": command, "path": path, "url": url}
    project_root = (authority or {}).get("permissionRuleProjectRoot") or getProjectRoot(context["session"]["cwd"])

    def matched(effect: str) -> dict[str, Any] | None:
        return matchPermissionRule(rules, call, effects=(effect,), projectRoot=project_root)

    deny_rule = matched("deny")
    if deny_rule:
        return {
            "decision": "deny",
            "source": f"rule:{deny_rule['scope']}",
            "rule": deny_rule["raw"],
            "reason": f"Blocked by permission rule {deny_rule['raw']} ({deny_rule['scope']} settings).",
        }
    if mode == "bypassPermissions":
        return {"decision": "allow", "source": "mode"}
    ask_rule = matched("ask")
    if ask_rule:
        return {
            "decision": "ask",
            "source": f"rule:{ask_rule['scope']}",
            "rule": ask_rule["raw"],
            "reason": f"Permission rule {ask_rule['raw']} ({ask_rule['scope']} settings) requires approval.",
        }
    if not always_ask:
        allow_rule = matched("allow")
        if allow_rule:
            return {
                "decision": "allow",
                "source": f"rule:{allow_rule['scope']}",
                "rule": allow_rule["raw"],
            }
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
    if category == "network":
        if mode in ("auto", "dontAsk"):
            return {"decision": "deny", "source": "mode", "reason": "Long-lived network checks are blocked in non-interactive permission modes."}
        return {"decision": "ask", "source": "mode", "switchMode": "bypassPermissions", "reason": "This tool schedules recurring network requests."}
    if category == "mcp":
        if mode in ("auto", "dontAsk"):
            return {"decision": "deny", "source": "mode", "reason": "MCP tools require an explicit allow permission rule in non-interactive permission modes."}
        return {"decision": "ask", "source": "mode", "switchMode": "bypassPermissions", "reason": "This MCP server tool runs outside the built-in toolset."}

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
    return bool(classifyReadOnlyShellCommand(command).get("safe"))


def _matches_command_policy(command: str, patterns: list[str]) -> bool:
    parsed = parseSingleShellCommand(command)
    if not parsed.get("ok"):
        return False
    for pattern in patterns:
        parsed_pattern = parseSingleShellCommand(pattern)
        if not parsed_pattern.get("ok") or len(parsed_pattern["argv"]) > len(parsed["argv"]):
            continue
        if all(
            value.lower() == parsed["argv"][index].lower()
            for index, value in enumerate(parsed_pattern["argv"])
        ):
            return True
    return False


async def _ask_question_with_detail(context: ToolContext, prompt: str, options: list[str], detail: dict[str, Any], fallback: str) -> str:
    ask = context.get("askQuestion", lambda *_: fallback)
    try:
        return await maybe_await(ask(prompt, options, detail))
    except TypeError:
        return await maybe_await(ask(prompt, options))
