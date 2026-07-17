from __future__ import annotations

import asyncio
import hashlib
import threading
from typing import Any, Callable, Mapping

from ._utils import maybe_await
from .config import getDefaultToolList
from .runtime_paths import RuntimePaths


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


def installComponentSurface(runtime: dict[str, Any], components: dict[str, Any]) -> None:
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


async def acquireCommit(lock: threading.Lock) -> None:
    while not lock.acquire(blocking=False):
        await asyncio.sleep(0.001)


async def maybeCall(callableValue: Any) -> Any:
    if not callable(callableValue):
        return None
    return await maybe_await(callableValue())


async def closeMany(closers: list[Any]) -> list[BaseException]:
    failures: list[BaseException] = []
    for closer in closers:
        if not callable(closer):
            continue
        try:
            await maybe_await(closer())
        except BaseException as exc:
            failures.append(exc)
    return failures


def hasMethod(value: Any, name: str) -> bool:
    return callable(getMethod(value, name))


def getMethod(value: Any, name: str) -> Any:
    if isinstance(value, Mapping):
        return value.get(name)
    return getattr(value, name, None) if value is not None else None


async def callMethod(value: Any, name: str, *args: Any) -> Any:
    method = getMethod(value, name)
    if not callable(method):
        return None
    return await maybe_await(method(*args))


def runtimePathGraphId(paths: RuntimePaths) -> str:
    raw = "\0".join(str(getattr(paths, field)) for field in paths.__dataclass_fields__)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def augmentConfigTools(config: dict[str, Any], runtimeToolNames: list[str]) -> None:
    tools = config.setdefault("tools", [])
    allowed = config.setdefault("allowedTools", [])
    defaults = set(getDefaultToolList())
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


def taskSnapshotFromState(getState: Callable[[], dict[str, Any]]) -> dict[str, Any]:
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


def daemonSnapshotPayload(status: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "phase": firstPresent(status, "phase"),
        "epoch": firstPresent(status, "epoch"),
        "pid": firstPresent(status, "pid"),
        "socket_path": firstPresent(status, "socketPath", "socket_path"),
        "supervisor_id": firstPresent(status, "supervisorId", "supervisor_id"),
        "attached_clients": firstPresent(status, "attachedClients", "attached_clients"),
        "watched_sessions": firstPresent(status, "watchedSessions", "watched_sessions"),
        "active_tasks": firstPresent(status, "activeTasks", "active_tasks"),
        "updated_at": firstPresent(status, "updatedAt", "updated_at"),
    }


def firstPresent(data: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in data:
            return data.get(key)
    return None
