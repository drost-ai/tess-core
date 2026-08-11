from __future__ import annotations

import contextvars
import inspect
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable


DEFAULT_RUNTIME_HOME = "~/.agent-core"


@dataclass(frozen=True, slots=True)
class RuntimePaths:
    home: str
    sessionsDir: str
    tasksDir: str
    taskRecordsDir: str
    monitorsDir: str
    agentsDir: str
    bridgesDir: str
    remoteSessionsDir: str
    compactionsDir: str
    artifactsDir: str
    trustDir: str
    repositoryTrustDir: str
    executableSnapshotsDir: str
    daemonDir: str
    daemon_logs_dir: str
    locksDir: str
    logsDir: str
    mcpBlobsDir: str
    mcpTokensDir: str
    privateSecretsDir: str


@dataclass(slots=True)
class _RuntimePathScope:
    paths: RuntimePaths
    immutable: bool


_active_runtime_paths: contextvars.ContextVar[_RuntimePathScope | None] = contextvars.ContextVar(
    "agent_core_runtime_paths",
    default=None,
)
_configured_runtime_paths: RuntimePaths | None = None


def expandRuntimeHome(rawPath: str) -> str:
    if rawPath == "~":
        return str(Path.home())
    if rawPath.startswith("~/"):
        return str(Path.home() / rawPath[2:])
    return rawPath


def resolveRuntimePaths(cwd: str | None = None, candidate: str | None = None) -> RuntimePaths:
    raw = candidate or os.environ.get("AGENT_CORE_HOME") or DEFAULT_RUNTIME_HOME
    expanded = expandRuntimeHome(raw)
    home = os.path.normpath(expanded if os.path.isabs(expanded) else os.path.join(cwd or os.getcwd(), expanded))
    trust = os.path.join(home, "trust")
    daemon = os.path.join(home, "daemon")
    return RuntimePaths(
        home=home,
        sessionsDir=os.path.join(home, "sessions"),
        tasksDir=os.path.join(home, "tasks"),
        taskRecordsDir=os.path.join(home, "task-records"),
        monitorsDir=os.path.join(home, "monitors"),
        agentsDir=os.path.join(home, "agents"),
        bridgesDir=os.path.join(home, "bridges"),
        remoteSessionsDir=os.path.join(home, "remote-sessions"),
        compactionsDir=os.path.join(home, "compactions"),
        artifactsDir=os.path.join(home, "tool-artifacts"),
        trustDir=trust,
        repositoryTrustDir=os.path.join(trust, "repositories"),
        executableSnapshotsDir=os.path.join(trust, "snapshots"),
        daemonDir=daemon,
        daemon_logs_dir=os.path.join(daemon, "logs"),
        locksDir=os.path.join(home, "locks"),
        logsDir=os.path.join(home, "logs"),
        mcpBlobsDir=os.path.join(home, "mcp-blobs"),
        mcpTokensDir=os.path.join(home, "mcp-tokens"),
        privateSecretsDir=os.path.join(home, "private-secrets"),
    )


def configureActiveRuntimePaths(runtimeHome: str) -> RuntimePaths:
    global _configured_runtime_paths
    next_paths = resolveRuntimePaths(candidate=runtimeHome)
    scoped = _active_runtime_paths.get()
    if scoped is None:
        _configured_runtime_paths = next_paths
        return next_paths
    if scoped.immutable and scoped.paths.home != next_paths.home:
        raise RuntimeError(
            f"Runtime path scope is immutable ({scoped.paths.home}); refusing switch to {next_paths.home}."
        )
    if not scoped.immutable:
        scoped.paths = next_paths
    return scoped.paths


def getActiveRuntimePaths() -> RuntimePaths:
    scoped = _active_runtime_paths.get()
    return scoped.paths if scoped is not None else _configured_runtime_paths or resolveRuntimePaths()


def getScopedRuntimePaths() -> RuntimePaths | None:
    scoped = _active_runtime_paths.get()
    return scoped.paths if scoped is not None else None


def resetActiveRuntimePaths() -> None:
    global _configured_runtime_paths
    _configured_runtime_paths = None


class RuntimePathsScope:
    def __init__(self, paths: RuntimePaths, *, immutable: bool = True):
        self.paths = paths
        self.immutable = immutable
        self._token: contextvars.Token[_RuntimePathScope | None] | None = None

    def __enter__(self) -> RuntimePaths:
        if self._token is not None:
            raise RuntimeError("Runtime path scope is already active.")
        scope = _RuntimePathScope(self.paths, self.immutable)
        self._token = _active_runtime_paths.set(scope)
        return scope.paths

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        if self._token is not None:
            _active_runtime_paths.reset(self._token)
            self._token = None

    async def __aenter__(self) -> RuntimePaths:
        return self.__enter__()

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.__exit__(exc_type, exc, tb)


def runWithRuntimePaths(
    paths: RuntimePaths,
    fn: Callable[[], Any] | None = None,
    options: dict[str, Any] | None = None,
) -> Any:
    scope = RuntimePathsScope(paths, immutable=(options or {}).get("immutable", True) is not False)
    if fn is None:
        return scope
    scope.__enter__()
    try:
        result = fn()
    except BaseException:
        scope.__exit__(None, None, None)
        raise
    if inspect.isawaitable(result):
        # The callable has run under the requested scope, but its awaitable may
        # be scheduled in a newly-created asyncio Task (notably by
        # ``asyncio.run`` in the background runtimes).  ContextVar tokens may
        # only be reset in the Context that created them, so close this scope
        # before returning and establish a fresh one inside the awaiting Task.
        scope.__exit__(None, None, None)

        async def await_scoped() -> Any:
            async with RuntimePathsScope(
                paths,
                immutable=(options or {}).get("immutable", True) is not False,
            ):
                return await result

        return await_scoped()
    scope.__exit__(None, None, None)
    return result


