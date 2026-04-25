from __future__ import annotations

from typing import Any

from .daemon import ensureTaskSupervisorDaemon
from .mcp import createMcpRuntime
from .runtime_state import createRuntimeStateStore
from .tools import createBuiltinTools


async def bootstrapRuntime(args: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
    args = {**(args or {}), **kwargs}
    subscribers = []
    owned_closers = []

    def emit(event: str, data: dict[str, Any]):
        state_store["dispatch"]({"type": event, "data": data})
        for fn in list(subscribers):
            fn(event, data)

    def subscribe(fn):
        subscribers.append(fn)
        return lambda: subscribers.remove(fn) if fn in subscribers else None

    state_store = createRuntimeStateStore()
    mcp_runtime = args.get("mcpRuntime")
    config = args.get("config") or {}
    cwd = args.get("cwd") or config.get("cwd")
    if mcp_runtime is None and (args.get("mcpConfig") or config.get("mcpConfig")):
        mcp_runtime = await createMcpRuntime(
            {
                "cwd": cwd,
                "servers": args.get("mcpConfig") or config.get("mcpConfig") or [],
                "sessionId": args.get("sessionId"),
                "registeredChannels": args.get("registeredChannels") or [],
                "onRuntimeEvent": emit,
            }
        )
        owned_closers.append(mcp_runtime["close"])
    daemon = args.get("daemon")
    if daemon is None and args.get("daemonEnabled"):
        daemon = await ensureTaskSupervisorDaemon({"cwd": cwd})
    tool_options = {**args, "mcpRuntime": mcp_runtime}
    tools = createBuiltinTools(tool_options)
    if mcp_runtime:
        tools.extend(mcp_runtime["createToolDefinitions"]())

    async def close():
        for closer in owned_closers:
            result = closer()
            if hasattr(result, "__await__"):
                await result

    return {"tools": tools, "mcpRuntime": mcp_runtime, "daemon": daemon, "emitEvent": emit, "subscribe": subscribe, "stateStore": state_store, "close": close}
