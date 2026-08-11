from __future__ import annotations
from .._utils import field as _field

import asyncio
import hashlib
import threading
from typing import Any, Callable, Mapping

from .._utils import maybe_await
from ..config import get_default_tool_list
from .paths import RuntimePaths


class StagedRuntimeSource:
    def __init__(self, binding: Mapping[str, Any], dispatch: Callable[..., None]):
        self.binding = binding
        self._dispatch = dispatch
        self._state = "staged"
        self._events: list[tuple[str, dict[str, Any]]] = []

    def emit(self, event: str, data: dict[str, Any] | None = None) -> None:
        if self._state == "discarded":
            return
        normalized = {**(data or {}), "session_id": self.binding.get("sessionId")}
        if self._state == "staged":
            self._events.append((event, normalized))
            return
        self._dispatch(self.binding, event, normalized)

    def activate(self) -> list[tuple[str, dict[str, Any]]]:
        if self._state != "staged":
            return []
        self._state = "active"
        queued, self._events = self._events, []
        return queued

    def discard(self) -> None:
        self._state = "discarded"
        self._events.clear()


def install_component_surface(runtime: dict[str, Any], components: dict[str, Any]) -> None:
    runtime.update(
        {
            "tools": components["tools"],
            "slashCommands": components["slashCommands"],
            "mcpRuntime": components["mcp"],
            "mcp": components["mcp"],
            "bridgeRuntime": components["bridge"],
            "bridge": components["bridge"],
            "workspace": components["workspace"],
            "plugins": components["plugins"],
        }
    )


async def acquire_commit(lock: threading.Lock) -> None:
    while not lock.acquire(blocking=False):
        await asyncio.sleep(0.001)


async def maybe_call(callableValue: Any) -> Any:
    if not callable(callableValue):
        return None
    return await maybe_await(callableValue())


async def close_many(closers: list[Any]) -> list[BaseException]:
    failures: list[BaseException] = []
    for closer in closers:
        if not callable(closer):
            continue
        try:
            await maybe_await(closer())
        except BaseException as exc:
            failures.append(exc)
    return failures


def has_method(value: Any, name: str) -> bool:
    return callable(get_method(value, name))


def get_method(value: Any, name: str) -> Any:
    if isinstance(value, Mapping):
        return value.get(name)
    return getattr(value, name, None) if value is not None else None


async def call_method(value: Any, name: str, *args: Any) -> Any:
    method = get_method(value, name)
    if not callable(method):
        return None
    return await maybe_await(method(*args))


def runtime_path_graph_id(paths: RuntimePaths) -> str:
    raw = "\0".join(str(getattr(paths, field)) for field in paths.__dataclass_fields__)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def augment_config_tools(config: dict[str, Any], runtimeToolNames: list[str]) -> None:
    tools = config.setdefault("tools", [])
    allowed = config.setdefault("allowedTools", [])
    defaults = set(get_default_tool_list())
    default_allow = (
        len(tools) == len(defaults)
        and len(allowed) == len(defaults)
        and all(tool in defaults for tool in tools)
        and all(tool in defaults for tool in allowed)
    )
    for name in runtimeToolNames:
        if name not in tools:
            tools.append(name)
        if default_allow and name not in allowed:
            allowed.append(name)


def task_snapshot_from_state(getState: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    tasks = getState().get("tasks", {})
    return {
        "total": tasks.get("total", 0),
        "active": tasks.get("activeCount", 0),
        "backgrounded": tasks.get("backgroundedCount", 0),
        "awaitingInput": tasks.get("awaitingInputCount", 0),
        "failed": tasks.get("failedCount", 0),
        "labels": tasks.get("labels", []),
        "tasks": tasks.get("tasks", []),
    }


def daemon_snapshot_payload(status: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "phase": _field(status, "phase"),
        "epoch": _field(status, "epoch"),
        "pid": _field(status, "pid"),
        "socket_path": _field(status, "socketPath", "socket_path"),
        "supervisor_id": _field(status, "supervisorId", "supervisor_id"),
        "attached_clients": _field(status, "attachedClients", "attached_clients"),
        "watched_sessions": _field(status, "watchedSessions", "watched_sessions"),
        "active_tasks": _field(status, "activeTasks", "active_tasks"),
        "updated_at": _field(status, "updatedAt", "updated_at"),
    }


def default_runtime_component_factories() -> dict[str, Any]:
    # Call-time imports: these factories pull in the full component graph,
    # which must not load when only the light support helpers are needed.
    from ..authority.agent import enforce_session_agent_authority, filter_tools_for_agent_authority
    from ..authority.plugins import load_plugin_runtime
    from ..remote.bridge_runtime import create_bridge_runtime
    from ..mcp import create_mcp_runtime
    from ..tools import create_builtin_tools
    from .workspace import create_workspace_controller

    return {
        "enforceAuthority": enforce_session_agent_authority,
        "filterAuthorityTools": filter_tools_for_agent_authority,
        "createWorkspace": create_workspace_controller,
        "load_plugins": load_plugin_runtime,
        "createMcp": create_mcp_runtime,
        "createBridge": create_bridge_runtime,
        "create_builtin_tools": create_builtin_tools,
        "augment_config_tools": augment_config_tools,
    }
