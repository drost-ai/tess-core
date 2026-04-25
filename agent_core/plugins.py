from __future__ import annotations

from typing import Any


async def loadPluginRuntime(config: dict[str, Any]) -> dict[str, Any]:
    return {"plugins": [], "tools": [], "commands": [], "mcpServers": [], "diagnostics": []}
