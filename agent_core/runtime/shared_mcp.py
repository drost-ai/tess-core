"""Host selection and child admission for the host-lifetime MCP broker.

Only the host's explicit name selection may convert an upstream descriptor.
Children can only derive a new, narrower lease from an existing broker lease.
Persisted descriptors contain opaque private-store references, never bearers.
"""
from __future__ import annotations

import copy
from typing import Any

from ..authority.executable_source_policy import executable_config_digest
from ..authority.executable_config import parse_mcp_server_config


def _canonical_descriptor(descriptor: dict[str, Any]) -> dict[str, Any]:
    return parse_mcp_server_config({**descriptor, "commandOrUrl": descriptor.get("commandOrUrl") or descriptor.get("url")})


def _delegable(server: dict[str, Any]) -> bool:
    policy = server.get("delegation")
    return policy is None or (
        isinstance(policy, dict)
        and not set(policy) - {"mode", "scope"}
        and policy.get("mode") == "allow"
        and policy.get("scope", "shared") == "shared"
    )


async def prepare_host_shared_mcp(runtime: Any, config: dict[str, Any], session: dict[str, Any]) -> None:
    from ..mcp.shared_broker import BrokerGrant
    from ..mcp.shared_broker_http import SharedMcpBroker

    servers = config.get("mcpConfig") or []
    selected = set(runtime.shared_mcp_servers)
    if not any(s.get("name") in selected and _delegable(s) and not s.get("sharedBroker") for s in servers):
        return
    async with runtime._shared_mcp_lock:
        if runtime._shared_mcp_broker is None:
            broker = SharedMcpBroker()
            await broker.start()
            runtime._shared_mcp_broker = broker
        broker = runtime._shared_mcp_broker
    rewritten = []
    for server in servers:
        if server.get("name") not in selected or not _delegable(server) or server.get("sharedBroker"):
            rewritten.append(server)
            continue
        # Host selection is the explicit share-safety declaration. It cannot
        # override a personal/session descriptor's nondelegation policy.
        upstream = parse_mcp_server_config({**server, "sharing": {"mode": "host"}, "delegation": {"mode": "allow", "scope": "shared"}})
        fingerprint = executable_config_digest(upstream)
        key = (session["id"], session["cwd"], fingerprint)
        # Idle close/open is transport lifetime, not authority lifetime. Reuse
        # this root's pinned grant without revoking its resident descendants.
        # Changed descriptors/cwd are distinct security identities.
        async with runtime._shared_mcp_lock:
            descriptor = runtime._shared_mcp_descriptors.get(key)
            if descriptor is None:
                descriptor = _canonical_descriptor(await broker.acquire_descriptor(
                    upstream, cwd=session["cwd"], source_fingerprint=fingerprint,
                    grant=BrokerGrant(child_id=session["id"], tools=None, resources=None),
                ))
                runtime._shared_mcp_descriptors[key] = descriptor
        rewritten.append(copy.deepcopy(descriptor))
    config["mcpConfig"] = rewritten


def _unconditionally_granted_tools(authority: dict[str, Any]) -> frozenset[str]:
    """A bearer cannot perform an interactive check or execute a host hook.

    Argument-scoped, ask and deny constraints therefore remove the entire tool
    from the capability, even when the ordinary dispatcher could prompt later.
    """
    from ..authority.permission_rules import _tool_name_matches

    if any(hook.get("blocking") for hook in authority.get("hooks", [])):
        return frozenset()
    granted = set()
    for name in authority["enabledTools"]:
        rules = [r for r in authority.get("permissionRules", [])
                 if _tool_name_matches(str(r.get("tool", "")), name)]
        if any(r.get("effect") in ("deny", "ask") for r in rules):
            continue
        unconditional = any(r.get("effect") == "allow" and r.get("approved")
                            and not isinstance(r.get("pattern"), str) for r in rules)
        if any(isinstance(r.get("pattern"), str) for r in rules) and not unconditional:
            continue
        if authority.get("mode") == "bypassPermissions" or unconditional:
            granted.add(name)
    return frozenset(granted)


async def delegate_child_shared_mcp(config: dict[str, Any], authority: dict[str, Any], session_id: str) -> None:
    """Install independently authorized descriptors before the launch is pinned.

    This works inside subprocess children too: admission uses the authenticated
    parent's broker endpoint, never process-local runtime discovery.
    """
    from ..authority.agent import fingerprint_agent_authority
    from ..mcp.shared_broker import BrokerGrant
    from ..mcp.shared_broker_http import delegate_shared_mcp_descriptor

    servers = config.get("mcpConfig") or []
    if not any(server.get("sharedBroker") for server in servers):
        return
    enabled = _unconditionally_granted_tools(authority)
    rewritten = []
    issued = []
    try:
        for server in servers:
            if not server.get("sharedBroker") or not _delegable(server):
                rewritten.append(server)
                continue
            resources = None if "ReadMcpResource" in enabled else frozenset()
            descriptor = _canonical_descriptor(await delegate_shared_mcp_descriptor(
                server, BrokerGrant(child_id=session_id, tools=None, resources=resources),
                enabled_tools=frozenset(enabled),
            ))
            issued.append(descriptor)
            rewritten.append(descriptor)
    except BaseException:
        await revoke_child_shared_mcp({"mcpConfig": issued})
        raise
    config["mcpConfig"] = rewritten
    authority["executableSources"]["mcpConfig"] = copy.deepcopy(rewritten)
    authority["executableSources"]["mcpConfigDigest"] = executable_config_digest(rewritten)
    authority["fingerprint"] = fingerprint_agent_authority(authority)


async def revoke_child_shared_mcp(config: dict[str, Any]) -> None:
    """Best-effort terminal cleanup; a stopped host already revoked everything."""
    from ..mcp.shared_broker_http import revoke_shared_mcp_descriptor

    for descriptor in config.get("mcpConfig") or []:
        if descriptor.get("sharedBroker"):
            try:
                await revoke_shared_mcp_descriptor(descriptor)
            except Exception:
                pass


async def revoke_terminal_child(runtime: Any, task_id: str) -> None:
    from ..storage import read_task_meta, load_child_agent

    terminal = {"completed", "failed", "cancelled", "stopped", "terminated", "killed"}
    meta = await read_task_meta(task_id) or {}
    if meta.get("type") != "subagent" or meta.get("status") not in terminal:
        return
    agent_id = meta.get("agent_id")
    if not isinstance(agent_id, str):
        return
    child = await load_child_agent(agent_id) or {}
    session_id = meta.get("sessionId") or meta.get("session_id")
    if (not isinstance(session_id, str) or child.get("sessionId") != session_id
            or child.get("taskId") != task_id):
        return
    # The task terminal transition is authoritative even if its child ledger
    # has not caught up yet. An older task cannot revoke a retried child.
    broker = runtime._shared_mcp_broker
    if broker is not None:
        await broker.revoke_child(session_id)
