"""Bootstrapped runtime surface contract.

``bootstrap_runtime`` returns a component surface addressed by these keys.
The keys are a cross-component protocol shared with the other tess-core
runtimes (tess-code walks the same surface on the TypeScript side) — they
stay camelCase and never rename independently of that protocol.
"""

from __future__ import annotations

from typing import Any, Callable, TypedDict


class BootstrappedRuntime(TypedDict, total=False):
    """Return surface of ``bootstrap_runtime`` (docs/public-api-contract.md)."""

    daemon: Any
    monitorRuntime: dict[str, Any] | None
    monitors: dict[str, Any] | None
    taskRuntime: dict[str, Any] | None
    tasks: dict[str, Any] | None
    collaborationRuntime: dict[str, Any] | None
    collaboration: dict[str, Any] | None
    emitEvent: Callable[..., None]
    emitRuntimeEvent: Callable[..., None]
    subscribe: Callable[..., Callable[[], None]]
    subscribeEvents: Callable[..., Callable[[], None]]
    getState: Callable[[], dict[str, Any]]
    stateStore: dict[str, Any]
    getActiveBinding: Callable[[], Any]
    setForegroundBusy: Callable[[bool], None]
    applyPendingMcpCatalogChanges: Callable[..., Any]
    refreshMcpToolRegistry: Callable[..., Any]
    getMonitorSnapshot: Callable[[], Any]
    getTaskSnapshot: Callable[[], Any]
    setActiveSession: Callable[..., Any]
    startTurn: Callable[..., None]
    setTurnCancelling: Callable[..., None]
    finishTurn: Callable[..., None]
    failTurn: Callable[..., None]
    isTurnInFlight: Callable[[], bool]
    close: Callable[[], Any]
    runtimePaths: Any
    runtimeInstanceId: str
    pathGraphId: str
    tools: list[dict[str, Any]]
    slashCommands: list[Any]
    mcpRuntime: Any
    mcp: Any
    bridgeRuntime: Any
    bridge: Any
    workspace: Any
    plugins: Any
