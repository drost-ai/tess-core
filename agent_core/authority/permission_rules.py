from __future__ import annotations

import hashlib
import json
import os
import re
import time
from typing import Any, Iterable, Mapping
from urllib.parse import urlsplit

from .repository_trust import approveRepositoryContent, checkRepositoryApproval
from .shell_command_policy import parseSingleShellCommand, splitShellCommandConstituents
from ..storage import file_or_json, get_project_root, load_settings


_TOOL_NAME_PATTERN = re.compile(r"^[A-Za-z][\w-]*(?:__[\w-]+)*$")
_SETTINGS_SCOPES = ("user", "project", "local")
_RULES_CACHE_TTL_SECONDS = 5.0
_rules_cache: dict[str, dict[str, Any]] = {}


def normalizeShellCommand(command: str) -> str:
    return re.sub(r"\s+", " ", command).strip().lower()


def parsePermissionRule(raw: str, effect: str, scope: str, approved: bool) -> dict[str, Any] | None:
    trimmed = raw.strip()
    if not trimmed:
        return None
    open_index = trimmed.find("(")
    if open_index == -1:
        if not _TOOL_NAME_PATTERN.fullmatch(trimmed):
            return None
        return {"effect": effect, "tool": trimmed, "scope": scope, "raw": trimmed, "approved": approved}
    if open_index == 0 or not trimmed.endswith(")"):
        return None
    tool = trimmed[:open_index]
    pattern = trimmed[open_index + 1 : -1].strip()
    if not _TOOL_NAME_PATTERN.fullmatch(tool) or not pattern:
        return None
    return {
        "effect": effect,
        "tool": tool,
        "pattern": pattern,
        "scope": scope,
        "raw": trimmed,
        "approved": approved,
    }


def _glob_to_regex(glob: str) -> re.Pattern[str]:
    output = ""
    index = 0
    while index < len(glob):
        char = glob[index]
        if char == "*":
            if index + 1 < len(glob) and glob[index + 1] == "*":
                if index + 2 < len(glob) and glob[index + 2] == "/":
                    output += r"(?:.*/)?"
                    index += 2
                else:
                    output += ".*"
                    index += 1
            else:
                output += "[^/]*"
        elif char == "?":
            output += "[^/]"
        else:
            output += re.escape(char)
        index += 1
    return re.compile(f"^{output}$")


def matchesBashRulePattern(
    pattern: str,
    command: str | None,
    *,
    requireSingleCommand: bool = True,
) -> bool:
    if command is None:
        return False
    if requireSingleCommand:
        parsed_command = parseSingleShellCommand(command)
        raw_pattern = pattern[:-2] if pattern.endswith(":*") else pattern
        parsed_pattern = parseSingleShellCommand(raw_pattern)
        if not parsed_command.get("ok") or not parsed_pattern.get("ok"):
            return False
        command_argv = [value.lower() for value in parsed_command["argv"]]
        pattern_argv = [value.lower() for value in parsed_pattern["argv"]]
        if not pattern.endswith(":*"):
            return command_argv == pattern_argv
        if len(pattern_argv) > len(command_argv):
            return False
        return all(
            command_argv[index].startswith(value) if index == len(pattern_argv) - 1 else command_argv[index] == value
            for index, value in enumerate(pattern_argv)
        )
    return any(
        _loose_bash_pattern_matches(pattern, candidate)
        for candidate in _deny_command_candidates(command)
    )


def _loose_bash_pattern_matches(pattern: str, normalized: str) -> bool:
    if pattern.endswith(":*"):
        prefix = normalizeShellCommand(pattern[:-2])
        return bool(prefix and normalized.startswith(prefix))
    return normalized == normalizeShellCommand(pattern)


def _deny_command_candidates(command: str) -> list[str]:
    """Every view a deny rule must see: the whole command, each constituent
    simple command (env-assignment prefixes stripped), and each constituent
    with its binary path collapsed to the basename."""
    candidates = [normalizeShellCommand(command)]
    for argv in splitShellCommandConstituents(command):
        candidates.append(normalizeShellCommand(" ".join(argv)))
        basename = os.path.basename(argv[0])
        if basename and basename != argv[0]:
            candidates.append(normalizeShellCommand(" ".join([basename, *argv[1:]])))
    return candidates


def matchesPathRulePattern(pattern: str, candidatePath: str | None, projectRoot: str | None = None) -> bool:
    if not candidatePath:
        return False
    expanded = os.path.expanduser(pattern)
    candidate = os.path.abspath(candidatePath).replace(os.sep, "/")
    if os.path.isabs(expanded):
        return bool(_glob_to_regex(expanded.replace(os.sep, "/")).fullmatch(candidate))
    if not projectRoot:
        return False
    relative = os.path.relpath(os.path.abspath(candidatePath), projectRoot)
    if relative in {"", "."} or relative == os.pardir or relative.startswith(os.pardir + os.sep) or os.path.isabs(relative):
        return False
    return bool(_glob_to_regex(expanded.replace(os.sep, "/")).fullmatch(relative.replace(os.sep, "/")))


def matchesDomainRulePattern(domainPattern: str, url: str | None) -> bool:
    if not url:
        return False
    try:
        host = (urlsplit(url).hostname or "").lower()
    except ValueError:
        return False
    domain = domainPattern.strip().lower()
    return bool(domain and (host == domain or host.endswith(f".{domain}")))


def _tool_name_matches(rule_tool: str, tool_name: str) -> bool:
    return rule_tool == tool_name or (rule_tool.startswith("mcp__") and tool_name.startswith(f"{rule_tool}__"))


def _rule_matches(rule: Mapping[str, Any], call: Mapping[str, Any], project_root: str | None) -> bool:
    if not _tool_name_matches(str(rule.get("tool")), str(call.get("toolName"))):
        return False
    pattern = rule.get("pattern")
    if not isinstance(pattern, str):
        return True
    if pattern.startswith("domain:"):
        return matchesDomainRulePattern(pattern[len("domain:") :], call.get("url"))
    if rule.get("tool") == "Bash":
        return matchesBashRulePattern(
            pattern,
            call.get("command"),
            requireSingleCommand=rule.get("effect") == "allow",
        )
    if str(rule.get("tool") or "").startswith("mcp__"):
        # No argument-pattern language exists for MCP calls. A pattern that
        # cannot be evaluated may narrow an allow (never match) but must not
        # weaken a deny/ask: those fire on the tool match alone.
        return rule.get("effect") != "allow"
    return matchesPathRulePattern(pattern, call.get("path"), project_root)


def matchPermissionRule(
    rules: Iterable[Mapping[str, Any]],
    call: Mapping[str, Any],
    *,
    effects: Iterable[str] | None = None,
    projectRoot: str | None = None,
) -> dict[str, Any] | None:
    selected_effects = set(effects or ("deny", "ask", "allow"))
    rules_list = list(rules)
    for effect in ("deny", "ask", "allow"):
        if effect not in selected_effects:
            continue
        for rule in rules_list:
            if rule.get("effect") != effect or (effect == "allow" and not rule.get("approved")):
                continue
            if _rule_matches(rule, call, projectRoot):
                return dict(rule)
    return None


def _allow_list_hash(allow: list[str]) -> str:
    return hashlib.sha256(json.dumps(allow, separators=(",", ":")).encode()).hexdigest()


async def approvePermissionRules(cwd: str, scope: str) -> None:
    if scope not in {"project", "local"}:
        raise ValueError("Permission-rule approval scope must be project or local")
    settings = await load_settings(scope, cwd)
    allow = ((settings.get("permissions") or {}).get("allow") or [])
    await approveRepositoryContent(
        cwd=cwd,
        family="permission_allow",
        scope=scope,
        contentDigest=_allow_list_hash(allow),
    )
    clearPermissionRulesCache()


async def _allow_list_approved(cwd: str, scope: str, allow: list[str]) -> bool:
    return not allow or await checkRepositoryApproval(
        cwd=cwd,
        family="permission_allow",
        scope=scope,
        contentDigest=_allow_list_hash(allow),
    )


def _collect_rules(settings: Any, scope: str, allow_approved: bool, output: list[dict[str, Any]]) -> None:
    permissions = settings.get("permissions") if isinstance(settings, Mapping) else None
    if not isinstance(permissions, Mapping):
        return
    for effect, approved in (("deny", True), ("ask", True), ("allow", allow_approved)):
        for raw in permissions.get(effect) or []:
            rule = parsePermissionRule(raw, effect, scope, approved) if isinstance(raw, str) else None
            if rule:
                output.append(rule)


async def loadPermissionRules(
    args: Mapping[str, Any] | None = None,
    *,
    cwd: str | None = None,
    sources: list[str] | None = None,
    inlineSettings: str | None = None,
) -> list[dict[str, Any]]:
    values = dict(args or {})
    selected_cwd = str(values.get("cwd", cwd))
    selected_sources = values.get("sources", sources)
    selected_inline = values.get("inlineSettings", inlineSettings)
    scopes = [scope for scope in _SETTINGS_SCOPES if not selected_sources or scope in selected_sources]
    rules: list[dict[str, Any]] = []
    for scope in scopes:
        settings = await load_settings(scope, selected_cwd)
        permissions = settings.get("permissions") if isinstance(settings.get("permissions"), Mapping) else {}
        approved = scope == "user" or await _allow_list_approved(
            selected_cwd,
            scope,
            list(permissions.get("allow") or []),
        )
        _collect_rules(settings, scope, approved, rules)
    if selected_inline:
        try:
            inline = file_or_json(selected_inline)
            _collect_rules(inline, "inline", True, rules)
        except Exception:
            pass
    return rules


def clearPermissionRulesCache() -> None:
    _rules_cache.clear()


async def loadPermissionRulesCached(args: Mapping[str, Any] | None = None, **kwargs: Any) -> list[dict[str, Any]]:
    values = {**(args or {}), **kwargs}
    key = f"{values.get('cwd')}|{','.join(values.get('sources') or [])}|{values.get('inlineSettings') or ''}"
    cached = _rules_cache.get(key)
    if cached and cached["expiresAt"] > time.monotonic():
        return cached["rules"]
    rules = await loadPermissionRules(values)
    _rules_cache[key] = {"rules": rules, "expiresAt": time.monotonic() + _RULES_CACHE_TTL_SECONDS}
    return rules


async def _find_dispatch_rule(effect: str, args: Mapping[str, Any]) -> dict[str, Any] | None:
    rules = (
        args.get("rules")
        if isinstance(args.get("rules"), list)
        else await loadPermissionRulesCached(
            cwd=args["cwd"],
            sources=args.get("settingsSources"),
            inlineSettings=args.get("inlineSettings"),
        )
    )
    if not any(rule.get("effect") == effect and (effect != "allow" or rule.get("approved")) for rule in rules):
        return None
    input_value = args.get("input") if isinstance(args.get("input"), Mapping) else {}

    def first_string(*items: Any) -> str | None:
        return next((item for item in items if isinstance(item, str)), None)

    call = {
        "toolName": args["toolName"],
        "command": first_string(input_value.get("command")),
        "path": first_string(input_value.get("file_path"), input_value.get("notebook_path"), input_value.get("path")),
        "url": first_string(input_value.get("url")),
    }
    return matchPermissionRule(
        rules,
        call,
        effects=(effect,),
        projectRoot=args.get("projectRoot") or get_project_root(args["cwd"]),
    )


async def findDispatchDenyRule(args: Mapping[str, Any]) -> dict[str, Any] | None:
    return await _find_dispatch_rule("deny", args)


async def findDispatchAllowRule(args: Mapping[str, Any]) -> dict[str, Any] | None:
    return await _find_dispatch_rule("allow", args)


