from __future__ import annotations

import hashlib
import os
import re
import tempfile
from pathlib import Path
from typing import Literal
from ..runtime.identifiers import parse_artifact_id, parse_session_id
from ..runtime.paths import (
    RuntimePaths,
    RuntimePathsScope,
    configure_active_runtime_paths,
    get_active_runtime_paths,
    get_scoped_runtime_paths,
    reset_active_runtime_paths,
    resolve_runtime_paths,
)

Scope = Literal["user", "project", "local"]
DEFAULT_RUNTIME_HOME = "~/.agent-core"
class RuntimeHomeScope:
    def __init__(self, runtimeHome: str):
        self.runtimeHome = resolve_runtime_home(None, runtimeHome)
        self._scope = RuntimePathsScope(resolve_runtime_paths(candidate=self.runtimeHome), immutable=False)

    def __enter__(self) -> str:
        self._scope.__enter__()
        return self.runtimeHome

    def __exit__(self, exc_type, exc, tb) -> None:
        self._scope.__exit__(exc_type, exc, tb)

    async def __aenter__(self) -> str:
        return self.__enter__()

    async def __aexit__(self, exc_type, exc, tb) -> None:
        self.__exit__(exc_type, exc, tb)


def scoped_runtime_home(runtimeHome: str) -> RuntimeHomeScope:
    return RuntimeHomeScope(runtimeHome)


def get_context_runtime_home() -> str | None:
    scoped = get_scoped_runtime_paths()
    return scoped.home if scoped is not None else None


def expand_home(rawPath: str) -> str:
    if rawPath == "~":
        return str(Path.home())
    if rawPath.startswith("~/"):
        return str(Path.home() / rawPath[2:])
    return rawPath

def resolve_runtime_home(cwd: str | None = None, candidate: str | None = None) -> str:
    return resolve_runtime_paths(cwd, candidate).home

def configure_runtime_home(runtimeHome: str) -> str:
    return configure_active_runtime_paths(runtimeHome).home

def reset_configured_runtime_home() -> None:
    reset_active_runtime_paths()

def get_state_root() -> str:
    return get_active_runtime_paths().home

def find_git_root(cwd: str) -> str | None:
    """Resolve the nearest worktree root without spawning ``git``.

    Git represents ordinary repositories with a ``.git`` directory and linked
    worktrees/submodules with a ``.git`` file. Walking parents is both more
    responsive on the agent hot path and observes newly-created repositories
    without a process-lifetime negative cache.
    """
    try:
        current = Path(cwd).resolve(strict=False)
        if not current.is_dir():
            current = current.parent
        for candidate in (current, *current.parents):
            if (candidate / ".git").exists():
                return str(candidate)
    except (OSError, RuntimeError):
        return None
    return None

def get_project_root(cwd: str) -> str:
    return find_git_root(cwd) or cwd

def get_project_state_dir(cwd: str) -> str:
    return os.path.join(get_project_root(cwd), ".tess", "project")

def get_local_state_dir(cwd: str) -> str:
    return os.path.join(cwd, ".tess", "local")

def get_project_memory_path(cwd: str) -> str:
    return os.path.join(get_project_state_dir(cwd), "memory.md")

def get_project_memory_records_path(cwd: str) -> str:
    return os.path.join(get_project_state_dir(cwd), "memory-records.json")

def get_daemon_dir() -> str:
    return os.path.join(get_state_root(), "daemon")

def get_daemon_instances_dir() -> str:
    return os.path.join(get_daemon_dir(), "instances")

_DAEMON_INSTANCE_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-][A-Za-z0-9._-]{0,63}$")

def parse_daemon_instance_id(instanceId: str) -> str:
    if not isinstance(instanceId, str) or not _DAEMON_INSTANCE_ID_PATTERN.fullmatch(instanceId):
        raise ValueError(f"Invalid daemon instance id: {instanceId!r}")
    return instanceId

def daemon_instance_dir(instanceId: str) -> str:
    return os.path.join(get_daemon_instances_dir(), parse_daemon_instance_id(instanceId))

MAX_DAEMON_SOCKET_PATH_BYTES = 100

def daemon_socket_path(instanceId: str) -> str:
    preferred = os.path.join(daemon_instance_dir(instanceId), "daemon.sock")
    if len(preferred.encode("utf-8")) <= MAX_DAEMON_SOCKET_PATH_BYTES:
        return preferred
    scope_hash = hashlib.sha256(
        f"{get_state_root()}\n{parse_daemon_instance_id(instanceId)}".encode("utf-8")
    ).hexdigest()[:12]
    return os.path.join(tempfile.gettempdir(), f"agent-core-{scope_hash}.sock")

def daemon_pid_path(instanceId: str) -> str:
    return os.path.join(daemon_instance_dir(instanceId), "pid")

def daemon_state_path(instanceId: str) -> str:
    return os.path.join(daemon_instance_dir(instanceId), "state.json")

def daemon_events_path(instanceId: str) -> str:
    return os.path.join(daemon_instance_dir(instanceId), "events.jsonl")

def daemon_spawn_ledger_path(instanceId: str) -> str:
    return os.path.join(daemon_instance_dir(instanceId), "spawn-ledger.jsonl")

def daemon_logs_dir(instanceId: str) -> str:
    return os.path.join(daemon_instance_dir(instanceId), "logs")

def get_compactions_dir() -> str:
    return os.path.join(get_state_root(), "compactions")

def get_tool_artifacts_dir() -> str:
    return os.path.join(get_state_root(), "tool-artifacts")

def get_session_tool_artifacts_dir(sessionId: str) -> str:
    return os.path.join(get_tool_artifacts_dir(), parse_session_id(sessionId))

def tool_artifact_meta_path(sessionId: str, artifactId: str) -> str:
    return os.path.join(
        get_session_tool_artifacts_dir(sessionId),
        f"{parse_artifact_id(artifactId)}.meta.json",
    )

def tool_artifact_data_path(sessionId: str, artifactId: str, extension: str) -> str:
    ext = extension if extension.startswith(".") else f".{extension}"
    if not re.fullmatch(r"\.[A-Za-z0-9]{1,16}(?:\.[A-Za-z0-9]{1,8})?", ext):
        raise ValueError("Invalid artifact extension")
    return os.path.join(
        get_session_tool_artifacts_dir(sessionId),
        f"{parse_artifact_id(artifactId)}{ext}",
    )

def get_session_compactions_dir(sessionId: str) -> str:
    return os.path.join(get_compactions_dir(), parse_session_id(sessionId))

def compaction_manifest_path(sessionId: str) -> str:
    return os.path.join(get_session_compactions_dir(sessionId), "manifest.json")

def compaction_checkpoint_path(sessionId: str, generation: int) -> str:
    return os.path.join(get_session_compactions_dir(sessionId), "checkpoints", f"{generation:04d}.json")

async def ensure_base_layout(cwd: str, paths: RuntimePaths | None = None) -> None:
    graph = paths or get_active_runtime_paths()
    runtime_dirs = [
        graph.home,
        graph.daemonDir,
        graph.sessionsDir,
        graph.compactionsDir,
        graph.artifactsDir,
        graph.tasksDir,
        graph.taskRecordsDir,
        graph.monitorsDir,
        graph.agentsDir,
        graph.remoteSessionsDir,
        graph.bridgesDir,
        graph.logsDir,
        graph.daemon_logs_dir,
        graph.locksDir,
        graph.trustDir,
        graph.repositoryTrustDir,
        graph.executableSnapshotsDir,
        graph.mcpBlobsDir,
        graph.mcpTokensDir,
        graph.privateSecretsDir,
    ]
    for directory in runtime_dirs:
        os.makedirs(directory, exist_ok=True)
        if os.name != "nt":
            os.chmod(directory, 0o700)
    for directory in (get_project_state_dir(cwd), get_local_state_dir(cwd)):
        os.makedirs(directory, exist_ok=True)


def session_path(sessionId: str) -> str:
    return os.path.join(get_state_root(), "sessions", f"{parse_session_id(sessionId)}.json")

def session_archive_dir(sessionId: str) -> str:
    return os.path.join(get_state_root(), "sessions", parse_session_id(sessionId))

def session_transcript_log_path(sessionId: str) -> str:
    return os.path.join(session_archive_dir(sessionId), "full-transcript-with-tools.jsonl")
