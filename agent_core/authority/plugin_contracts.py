from __future__ import annotations

import json
from typing import Any

from .._utils import now_iso
from .executable_config import parseMcpServerConfigList, parsePluginManifestV1


def normalizePluginMcpServers(servers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return parseMcpServerConfigList(servers, "plugin.mcpServers")


def parsePluginManifest(content: str) -> dict[str, Any]:
    return parsePluginManifestV1(content, "plugin")


def validatePluginManifest(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    diagnostics: list[dict[str, Any]] = []
    plugin_id = str(manifest.get("id") or "plugin")
    if not str(manifest.get("id") or "").strip():
        diagnostics.append(_error_diagnostic(plugin_id, "manifest.id_missing", "Plugin manifest must include a non-empty id."))
    if not str(manifest.get("version") or "").strip():
        diagnostics.append(_error_diagnostic(plugin_id, "manifest.version_missing", "Plugin manifest must include a non-empty version."))

    command_names: set[str] = set()
    for command in manifest.get("commands") or []:
        name = str(command.get("name") or "").strip()
        normalized = name.lower()
        if not normalized:
            diagnostics.append(_error_diagnostic(plugin_id, "manifest.command_name_missing", "Plugin commands must include a non-empty name."))
            continue
        if normalized in command_names:
            diagnostics.append(_error_diagnostic(plugin_id, "manifest.command_duplicate", f"Duplicate plugin command name: {command.get('name')}"))
        command_names.add(normalized)
        if not str(command.get("command") or "").strip():
            diagnostics.append(_error_diagnostic(plugin_id, "manifest.command_empty", f"Plugin command {command.get('name')} must define a shell command."))

    server_names: set[str] = set()
    for server in manifest.get("mcpServers") or []:
        name = str(server.get("name") or "").strip()
        normalized = name.lower()
        if not normalized:
            diagnostics.append(_error_diagnostic(plugin_id, "manifest.mcp_name_missing", "Plugin MCP servers must include a non-empty name."))
            continue
        if normalized in server_names:
            diagnostics.append(_error_diagnostic(plugin_id, "manifest.mcp_duplicate", f"Duplicate plugin MCP server name: {server.get('name')}"))
        server_names.add(normalized)

    return diagnostics


def summarizePluginCapabilities(args: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
    args = {**(args or {}), **kwargs}
    manifest = args.get("manifest") or {}
    capabilities = manifest.get("capabilities") or {}
    declared = {
        "tools": bool(capabilities.get("tools") if capabilities.get("tools") is not None else manifest.get("entry")),
        "commands": bool(capabilities.get("commands") if capabilities.get("commands") is not None else len(manifest.get("commands") or []) > 0),
        "mcpServers": bool(capabilities.get("mcpServers") if capabilities.get("mcpServers") is not None else len(manifest.get("mcpServers") or []) > 0),
    }
    return {
        "declared": declared,
        "provided": {
            "tools": int(args.get("toolCount") or 0),
            "commands": int(args.get("commandCount") or 0),
            "mcpServers": int(args.get("mcpServerCount") or 0),
        },
    }


def validatePluginCapabilityContract(args: dict[str, Any] | None = None, **kwargs: Any) -> list[dict[str, Any]]:
    args = {**(args or {}), **kwargs}
    manifest = args.get("manifest") or {}
    summary = args.get("capabilitySummary") or {}
    contract = manifest.get("capabilities")
    if not contract:
        return []
    provided = summary.get("provided") or {}
    diagnostics: list[dict[str, Any]] = []
    plugin_id = str(manifest.get("id") or "plugin")

    if contract.get("tools") is False and provided.get("tools", 0) > 0:
        diagnostics.append(_error_diagnostic(plugin_id, "capability.tools_violated", "Plugin declared tools=false but loaded runtime tools."))
    if contract.get("commands") is False and provided.get("commands", 0) > 0:
        diagnostics.append(_error_diagnostic(plugin_id, "capability.commands_violated", "Plugin declared commands=false but loaded runtime commands."))
    if contract.get("mcpServers") is False and provided.get("mcpServers", 0) > 0:
        diagnostics.append(_error_diagnostic(plugin_id, "capability.mcp_violated", "Plugin declared mcpServers=false but loaded MCP servers."))

    if contract.get("tools") is True and provided.get("tools", 0) == 0:
        diagnostics.append(_warn_diagnostic(plugin_id, "capability.tools_missing", "Plugin declares tools=true but did not provide runtime tools."))
    if contract.get("commands") is True and provided.get("commands", 0) == 0:
        diagnostics.append(_warn_diagnostic(plugin_id, "capability.commands_missing", "Plugin declares commands=true but did not provide runtime or manifest commands."))
    if contract.get("mcpServers") is True and provided.get("mcpServers", 0) == 0:
        diagnostics.append(_warn_diagnostic(plugin_id, "capability.mcp_missing", "Plugin declares mcpServers=true but did not provide any MCP servers."))

    return diagnostics


def _normalize_capability_contract(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    filesystem = value.get("filesystem") if value.get("filesystem") in {"none", "workspace", "extended"} else None
    return {
        "tools": value.get("tools") if isinstance(value.get("tools"), bool) else None,
        "commands": value.get("commands") if isinstance(value.get("commands"), bool) else None,
        "mcpServers": value.get("mcpServers") if isinstance(value.get("mcpServers"), bool) else None,
        "network": value.get("network") if isinstance(value.get("network"), bool) else None,
        "backgroundTasks": value.get("backgroundTasks") if isinstance(value.get("backgroundTasks"), bool) else None,
        "filesystem": filesystem,
    }


def _normalize_command(command: Any) -> dict[str, Any]:
    record = command if isinstance(command, dict) else {}
    return {
        "name": str(record.get("name") if record.get("name") is not None else "command"),
        "description": record.get("description") if isinstance(record.get("description"), str) else None,
        "command": str(record.get("command") if record.get("command") is not None else ""),
    }


def _error_diagnostic(plugin_id: str, code: str, message: str) -> dict[str, Any]:
    return {"pluginId": plugin_id, "severity": "error", "code": code, "message": message}


def _warn_diagnostic(plugin_id: str, code: str, message: str) -> dict[str, Any]:
    return {"pluginId": plugin_id, "severity": "warn", "code": code, "message": message}
