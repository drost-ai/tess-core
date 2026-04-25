from __future__ import annotations

import json
from typing import Any


def normalizePluginMcpServers(servers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [dict(server) for server in servers]


def parsePluginManifest(content: str) -> dict[str, Any]:
    return json.loads(content)


def validatePluginManifest(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    diagnostics = []
    for key in ("id", "version"):
        if not manifest.get(key):
            diagnostics.append({"pluginId": manifest.get("id", ""), "severity": "error", "code": f"missing_{key}", "message": f"Plugin manifest is missing {key}."})
    return diagnostics


def summarizePluginCapabilities(args: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
    args = {**(args or {}), **kwargs}
    manifest = args.get("manifest", {})
    return {"declared": {"tools": bool(manifest.get("capabilities", {}).get("tools")), "commands": bool(manifest.get("capabilities", {}).get("commands")), "mcpServers": bool(manifest.get("capabilities", {}).get("mcpServers"))}, "provided": {"tools": 0, "commands": len(manifest.get("commands", [])), "mcpServers": len(manifest.get("mcpServers", []))}}


def validatePluginCapabilityContract(args: dict[str, Any] | None = None, **kwargs: Any) -> list[dict[str, Any]]:
    return []
