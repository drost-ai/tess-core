"""Normal session composition with the additional child MCP delegation boundary."""
from __future__ import annotations

from typing import Any

from ...runtime.session_components import prepare_runtime_session_components
from ...runtime.support import default_runtime_component_factories


def delegation_exclusion(server: dict[str, Any], authority: dict[str, Any] | None) -> str | None:
    # Old persisted launches predate session-bound descriptors. Never activate
    # their inherited servers implicitly; a newly compiled launch is required.
    if not authority or authority.get("executableSources", {}).get("mcpDelegationVersion") != 1:
        return "legacy_delegation_absent"
    if "delegation" not in server:
        return None
    policy = server["delegation"]
    if not isinstance(policy, dict) or set(policy) - {"mode", "scope"}:
        return "invalid_delegation_policy"
    if policy.get("mode") not in ("allow", "deny") or policy.get("scope", "shared") not in ("session", "shared"):
        return "invalid_delegation_policy"
    if policy["mode"] == "deny" or policy.get("scope") == "session":
        return "nondelegable_server"
    return None


async def prepare_subagent_components(*, session: dict[str, Any], config: dict[str, Any], binding: Any, emit: Any, factories: dict[str, Any] | None = None) -> dict[str, Any]:
    factories = dict(factories or default_runtime_component_factories())
    create_mcp = factories["createMcp"]

    async def create_delegated_mcp(options: dict[str, Any]) -> Any:
        authority = (session.get("metadata") or {}).get("effectiveAgentAuthority")
        selected = []
        for server in options["servers"]:
            reason = delegation_exclusion(server, authority)
            if reason:
                emit("mcp.delegation_excluded", {"server": server.get("name"), "reason": reason})
            else:
                selected.append(server)
        return await create_mcp({**options, "servers": selected})

    factories["createMcp"] = create_delegated_mcp
    try:
        components = await prepare_runtime_session_components({
            "session": session, "config": config, "binding": binding,
            "emit": emit, "factories": factories,
            "bootstrapArgs": {"detectWorkspaceIntegrations": False},
        })
    except BaseException:
        from ...runtime.shared_mcp import revoke_child_shared_mcp
        await revoke_child_shared_mcp(config)
        raise

    original_close = components["close"]
    closed = False

    async def close() -> None:
        nonlocal closed
        if closed:
            return
        closed = True
        try:
            await original_close()
        finally:
            from ...runtime.shared_mcp import revoke_child_shared_mcp
            await revoke_child_shared_mcp(config)

    components["close"] = close
    return components
