from __future__ import annotations

import copy
import hashlib
import json
import os
from typing import Any

from .executable_source_policy import executable_config_digest, load_selected_plugin_records
from .hooks import load_hooks
from .permission_rules import load_permission_rules_cached
from ..sandbox import resolve_sandbox_policy
from ..storage import get_project_root


_FILESYSTEM_RANK = {"read-only": 0, "workspace-write": 1, "full": 2}
_VALID_MODES = {"default", "acceptEdits", "bypassPermissions", "dontAsk", "auto"}


class AgentAuthorityError(PermissionError):
    def __init__(self, code: str, axis: str, message: str):
        super().__init__(message)
        self.code = code
        self.axis = axis


def _stable(value: Any) -> Any:
    if isinstance(value, list):
        return [_stable(item) for item in value]
    if isinstance(value, dict):
        return {key: _stable(value[key]) for key in sorted(value) if value[key] is not None}
    return value


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(_stable(value), separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def fingerprint_agent_authority(authority: dict[str, Any]) -> str:
    without = {key: value for key, value in authority.items() if key != "fingerprint"}
    return _digest({"authority_version": 1, "authority": without})


def _prompt_policy(mode: str) -> str:
    return "policy_ask" if mode in {"default", "acceptEdits"} else "never_ask"


def assert_valid_agent_authority(authority: Any) -> None:
    if not isinstance(authority, dict) or authority.get("version") != 1 or not authority.get("fingerprint"):
        raise AgentAuthorityError(
            "child_authority_invalid",
            "record",
            "Child authority record is missing or uses an unsupported version.",
        )
    if fingerprint_agent_authority(authority) != authority["fingerprint"]:
        raise AgentAuthorityError(
            "child_authority_invalid",
            "fingerprint",
            "Child authority fingerprint does not match its persisted policy.",
        )
    if authority.get("mode") not in _VALID_MODES or authority.get("promptPolicy") != _prompt_policy(authority["mode"]):
        raise AgentAuthorityError(
            "child_authority_invalid",
            "prompt_policy",
            "Child prompt policy is inconsistent with its effective permission mode.",
        )
    if authority.get("maySpawnChildren") and "Agent" not in (authority.get("enabledTools") or []):
        raise AgentAuthorityError(
            "child_authority_invalid",
            "spawn",
            "Child authority permits descendants without exposing the Agent tool.",
        )


async def canonicalize_authority_path(candidate: str, label: str = "Authority path") -> str:
    if not os.path.exists(candidate):
        raise AgentAuthorityError(
            "child_authority_invalid",
            "roots",
            f"{label} cannot be canonicalized: {os.path.abspath(candidate)}",
        )
    return os.path.realpath(candidate)


def _contained(root: str, candidate: str) -> bool:
    try:
        return os.path.commonpath((root, candidate)) == root
    except ValueError:
        return False


def is_path_within_authority(authority: dict[str, Any], candidate: str) -> bool:
    return any(_contained(root, candidate) for root in authority.get("readableRoots") or [])


async def assert_path_within_agent_authority(
    authority: dict[str, Any], candidate: str, label: str = "Child path"
) -> str:
    assert_valid_agent_authority(authority)
    canonical = await canonicalize_authority_path(candidate, label)
    if not is_path_within_authority(authority, canonical):
        raise AgentAuthorityError(
            "child_authority_escalation",
            "roots",
            f"{label} is outside parent canonical roots.",
        )
    return canonical


def meet_permission_modes(left: str, right: str) -> str:
    if left not in _VALID_MODES or right not in _VALID_MODES:
        raise AgentAuthorityError("child_authority_invalid", "mode", "Unknown permission mode.")
    if left == right:
        return left
    if left == "bypassPermissions":
        return right
    if right == "bypassPermissions":
        return left
    if {left, right} == {"default", "acceptEdits"}:
        return "default"
    if left in {"auto", "dontAsk"} and right in {"auto", "dontAsk"}:
        return "auto"
    # Incomparable modes meet at the most restrictive remaining mode.
    return "default"


def _default_filesystem(mode: str) -> str:
    if mode == "bypassPermissions":
        return "full"
    return "workspace-write"


def _sandbox_authority(config: dict[str, Any], session: dict[str, Any], mode: str) -> tuple[dict[str, Any], list[str]]:
    """Translate Python's runtime sandbox policy into the authority lattice.

    The TypeScript authority record uses semantic ``required/filesystem/network``
    axes, while the Python sandbox runtime uses ``mode`` and explicit roots.
    Persist both views so a child resume cannot accidentally turn the sandbox
    off by replaying TypeScript-shaped keys that Python ignores.
    """

    policy = resolve_sandbox_policy(config, session=session, cwd=session["cwd"])
    filesystem = _default_filesystem(mode)
    if filesystem == "read-only":
        writable: list[str] = []
    elif filesystem == "full":
        writable = [os.path.realpath(os.path.abspath(os.sep))]
    else:
        writable = sorted({os.path.realpath(path) for path in policy.get("writableRoots") or []})
    authority = {
        "required": policy.get("mode") == "os-required",
        "filesystem": filesystem,
        # Sandbox mode "off" means no sandbox exists, so nothing restricts
        # the network; the authority must record what the agent can actually
        # do, or network-gated tools ban themselves while Bash roams free.
        "network": (policy.get("mode") or "off") == "off" or bool(policy.get("allowNetwork")),
        "mode": policy.get("mode") or "off",
        "allowSubprocess": bool(policy.get("allowSubprocess", True)),
    }
    if policy.get("backend"):
        authority["backend"] = policy["backend"]
    return authority, writable


def sandbox_config_for_agent_authority(authority: dict[str, Any]) -> dict[str, Any]:
    """Return a Python sandbox config pinned to an effective authority."""

    sandbox = authority["sandbox"]
    mode = sandbox.get("mode")
    if mode not in {"off", "auto", "os-required"}:
        mode = "os-required" if sandbox.get("required") else "off"
    config: dict[str, Any] = {
        "mode": mode,
        "readableRoots": list(authority.get("readableRoots") or []),
        "writableRoots": list(authority.get("writableRoots") or []),
        "allowNetwork": bool(sandbox.get("network")),
        "allowSubprocess": bool(sandbox.get("allowSubprocess", True)),
    }
    if sandbox.get("backend"):
        config["backend"] = sandbox["backend"]
    return config


def _tool_name(tool: Any) -> str | None:
    return getattr(tool, "name", None) or (tool.get("name") if isinstance(tool, dict) else None)


async def capture_agent_authority(
    args: dict[str, Any] | None = None, **kwargs: Any
) -> dict[str, Any]:
    args = {**(args or {}), **kwargs}
    config, session, tools = args["config"], args["session"], args.get("tools") or []
    metadata = session.get("metadata") if isinstance(session.get("metadata"), dict) else {}
    existing = metadata.get("effectiveAgentAuthority")
    if existing and metadata.get("parentSessionId"):
        # A child's pin IS its authority. A root's pin is only the derivation
        # bookkeeping its last spawn left behind: the root answers to its
        # current configuration, so its authority is captured afresh.
        assert_valid_agent_authority(existing)
        return existing
    mode = session.get("permissionMode") or config.get("permissionMode") or "default"
    roots = [session["cwd"], *(config.get("allowedRoots") or [])]
    readable = sorted({await canonicalize_authority_path(root, "Agent root") for root in roots})
    sandbox, writable = _sandbox_authority(config, session, mode)
    configured = set(config.get("tools") or [])
    allowed = set(config.get("allowedTools") or [])
    denied = set(config.get("disallowedTools") or [])
    enabled = sorted(
        {
            name
            for tool in tools
            if (name := _tool_name(tool))
            and (not configured or name in configured)
            and name not in denied
            and (not allowed or name in allowed)
        }
    )
    settings_sources = list(config["settingsSources"] if config.get("settingsSources") is not None else ["user"])
    permission_rules = await load_permission_rules_cached(
        cwd=session["cwd"],
        sources=settings_sources,
        inlineSettings=config.get("inlineSettings"),
    )
    hooks = await load_hooks(cwd=session["cwd"], sources=settings_sources)
    plugin_dirs = sorted({os.path.abspath(path) for path in config.get("pluginDirs") or []})
    executable_sources = {
        "mcpDelegationVersion": 1,
        "settingsSources": settings_sources,
        **(
            {"inlineSettingsDigest": _digest(config["inlineSettings"])}
            if config.get("inlineSettings")
            else {}
        ),
        "mcpConfig": copy.deepcopy(config.get("mcpConfig") or []),
        "mcpConfigDigest": executable_config_digest(config.get("mcpConfig") or []),
        "mcpConfigPolicy": copy.deepcopy(config.get("mcpConfigPolicy") or {"mode": "merged"}),
        "pluginDirs": plugin_dirs,
        "pluginDirsDigest": executable_config_digest(plugin_dirs),
        "pluginRecords": copy.deepcopy(
            await load_selected_plugin_records(cwd=session["cwd"], sources=settings_sources)
        ),
    }
    authority = {
        "version": 1,
        "mode": mode,
        "promptPolicy": _prompt_policy(mode),
        "enabledTools": enabled,
        "permissionRules": copy.deepcopy(permission_rules),
        "permissionRuleProjectRoot": os.path.realpath(get_project_root(session["cwd"])),
        "readableRoots": readable,
        "writableRoots": writable,
        "sandbox": sandbox,
        "executableSources": executable_sources,
        "hooks": copy.deepcopy([hook for hook in hooks if hook.get("approved")]),
        "maySpawnChildren": "Agent" in enabled,
        "fingerprint": "",
    }
    authority["fingerprint"] = fingerprint_agent_authority(authority)
    return authority


def _includes_policy(entries: list[Any], candidate: Any) -> bool:
    target = json.dumps(_stable(candidate), separators=(",", ":"), ensure_ascii=False)
    return any(
        json.dumps(_stable(entry), separators=(",", ":"), ensure_ascii=False) == target
        for entry in entries
    )


def is_no_more_permissive(child: dict[str, Any], parent: dict[str, Any]) -> bool:
    if meet_permission_modes(child["mode"], parent["mode"]) != child["mode"]:
        return False
    if any(tool not in parent.get("enabledTools", []) for tool in child.get("enabledTools", [])):
        return False
    if any(
        not any(_contained(candidate, root) for candidate in parent.get("readableRoots", []))
        for root in child.get("readableRoots", [])
    ):
        return False
    if any(
        not any(_contained(candidate, root) for candidate in parent.get("writableRoots", []))
        for root in child.get("writableRoots", [])
    ):
        return False
    child_sandbox, parent_sandbox = child.get("sandbox", {}), parent.get("sandbox", {})
    if parent_sandbox.get("required") and not child_sandbox.get("required"):
        return False
    if _FILESYSTEM_RANK.get(child_sandbox.get("filesystem"), 99) > _FILESYSTEM_RANK.get(parent_sandbox.get("filesystem"), -1):
        return False
    if child_sandbox.get("network") and not parent_sandbox.get("network"):
        return False
    if child.get("maySpawnChildren") and not parent.get("maySpawnChildren"):
        return False
    parent_constraints = [rule for rule in parent.get("permissionRules", []) if rule.get("effect") != "allow"]
    if any(not _includes_policy(child.get("permissionRules", []), rule) for rule in parent_constraints):
        return False
    child_allows = [
        rule for rule in child.get("permissionRules", [])
        if rule.get("effect") == "allow" and rule.get("approved")
    ]
    if any(not _includes_policy(parent.get("permissionRules", []), rule) for rule in child_allows):
        return False
    parent_blocking = [hook for hook in parent.get("hooks", []) if hook.get("blocking")]
    if any(not _includes_policy(child.get("hooks", []), hook) for hook in parent_blocking):
        return False
    if any(not _includes_policy(parent.get("hooks", []), hook) for hook in child.get("hooks", [])):
        return False
    return _digest(child.get("executableSources")) == _digest(parent.get("executableSources"))


def derive_child_agent_authority(
    args: dict[str, Any] | None = None, **kwargs: Any
) -> dict[str, Any]:
    args = {**(args or {}), **kwargs}
    parent = args["parent"]
    assert_valid_agent_authority(parent)
    if not parent.get("maySpawnChildren") or "Agent" not in parent.get("enabledTools", []):
        raise AgentAuthorityError(
            "child_authority_escalation", "spawn", "Parent authority does not permit spawning child agents."
        )
    requested = list(dict.fromkeys((args.get("contract") or {}).get("tools") or parent["enabledTools"]))
    unavailable = sorted(tool for tool in requested if tool not in parent["enabledTools"])
    if unavailable:
        raise AgentAuthorityError(
            "child_authority_escalation",
            "tools",
            f"Child requested tools outside parent authority: {', '.join(unavailable)}",
        )
    requested_mode = args.get("requestedMode") or parent["mode"]
    mode = meet_permission_modes(parent["mode"], requested_mode)
    reductions = []
    if mode != requested_mode:
        reductions.append(f"permission mode reduced from {requested_mode} to {mode}")
    requested_filesystem = _default_filesystem(mode)
    filesystem = min(
        (parent["sandbox"]["filesystem"], requested_filesystem),
        key=lambda value: _FILESYSTEM_RANK[value],
    )
    if filesystem != requested_filesystem:
        reductions.append(f"sandbox filesystem reduced from {requested_filesystem} to {filesystem}")
    authority = copy.deepcopy(parent)
    authority.update(
        {
            "mode": mode,
            "promptPolicy": _prompt_policy(mode),
            "enabledTools": sorted(requested),
            "permissionRuleProjectRoot": args.get("permissionRuleProjectRoot") or parent.get("permissionRuleProjectRoot"),
            "writableRoots": [] if filesystem == "read-only" else list(parent.get("writableRoots", [])),
            "maySpawnChildren": bool(parent.get("maySpawnChildren") and "Agent" in requested),
            "parentFingerprint": parent["fingerprint"],
            "fingerprint": "",
        }
    )
    authority["sandbox"] = {**parent["sandbox"], "filesystem": filesystem}
    authority["fingerprint"] = fingerprint_agent_authority(authority)
    if not is_no_more_permissive(authority, parent):
        raise AgentAuthorityError(
            "child_authority_invalid",
            "meet",
            "Computed child authority is not a semantic subset of parent authority.",
        )
    granted = [
        os.path.realpath(path)
        for path in (args.get("grantedWorkspaceRoots") or [])
        if isinstance(path, str) and path
    ]
    if granted:
        # An isolation worktree lies OUTSIDE the parent's roots by
        # construction (a sibling of the repo), so it is granted explicitly
        # AFTER the subset check rather than threaded through it: the
        # launcher vouches only for workspaces it just created from content
        # the parent already had full authority over.
        authority["readableRoots"] = sorted({*authority["readableRoots"], *granted})
        if authority["sandbox"]["filesystem"] != "read-only":
            authority["writableRoots"] = sorted({*authority["writableRoots"], *granted})
        authority["fingerprint"] = ""
        authority["fingerprint"] = fingerprint_agent_authority(authority)
    return {"authority": authority, "reductions": reductions}


async def enforce_session_agent_authority(
    args: dict[str, Any] | None = None, **kwargs: Any
) -> dict[str, Any] | None:
    args = {**(args or {}), **kwargs}
    session, config = args["session"], args["config"]
    metadata = session.get("metadata") if isinstance(session.get("metadata"), dict) else {}
    authority = metadata.get("effectiveAgentAuthority")
    if not metadata.get("parentSessionId"):
        # Authority pins child sessions only. Spawning stamps the same record
        # on the root as derivation bookkeeping, but the root keeps its
        # freedom: whoever opens it decides its mode and roots, every time.
        # Enforcing the stale stamp here is what kept a root on the mode of
        # its first spawn whatever its owner configured afterwards.
        return None
    if not authority:
        raise AgentAuthorityError(
            "child_authority_missing",
            "record",
            "Legacy child session has no persisted effective authority; resume is blocked until a parent-authority intersection is recorded.",
        )
    assert_valid_agent_authority(authority)
    cwd = await canonicalize_authority_path(session["cwd"], "Child working directory")
    if not is_path_within_authority(authority, cwd):
        raise AgentAuthorityError(
            "child_authority_escalation", "roots", "Child working directory is outside persisted parent roots."
        )
    session["permissionMode"] = authority["mode"]
    config.update(
        {
            "permissionMode": authority["mode"],
            "allowedRoots": list(authority["readableRoots"]),
            "tools": list(authority["enabledTools"]),
            "allowedTools": list(authority["enabledTools"]),
            "disallowedTools": [],
            "sandbox": sandbox_config_for_agent_authority(authority),
            "settingsSources": list(authority["executableSources"]["settingsSources"]),
            "mcpConfig": copy.deepcopy(authority["executableSources"]["mcpConfig"]),
            "mcpConfigPolicy": copy.deepcopy(authority["executableSources"]["mcpConfigPolicy"]),
            "pluginDirs": list(authority["executableSources"]["pluginDirs"]),
            "permissionRules": copy.deepcopy(authority.get("permissionRules") or []),
            "hooks": copy.deepcopy(authority.get("hooks") or []),
        }
    )
    config.pop("inlineSettings", None)
    return authority


def assert_matching_persisted_child_authorities(
    sessionAuthority: dict[str, Any] | None, recordAuthority: dict[str, Any] | None
) -> None:
    if not sessionAuthority or not recordAuthority:
        raise AgentAuthorityError(
            "child_authority_missing",
            "record",
            "Background child launch requires matching persisted session and child-record authority.",
        )
    assert_valid_agent_authority(sessionAuthority)
    assert_valid_agent_authority(recordAuthority)
    if sessionAuthority["fingerprint"] != recordAuthority["fingerprint"]:
        raise AgentAuthorityError(
            "child_authority_invalid",
            "fingerprint",
            "Background child session and child record disagree on effective authority.",
        )


def filter_tools_for_agent_authority(tools: list[Any], authority: dict[str, Any] | None) -> list[Any]:
    if not authority:
        return tools
    allowed = set(authority["enabledTools"])
    return [tool for tool in tools if _tool_name(tool) in allowed]
