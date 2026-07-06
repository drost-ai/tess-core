from __future__ import annotations

import contextvars
import hashlib
import tempfile

from .shared import *

Scope = Literal["user", "project", "local"]
DEFAULT_RUNTIME_HOME = "~/.agent-core"
_configured_runtime_home: str | None = None
_active_runtime_home: contextvars.ContextVar[str | None] = contextvars.ContextVar("agent_core_runtime_home", default=None)


class RuntimeHomeScope:
    def __init__(self, runtimeHome: str):
        self.runtimeHome = resolveRuntimeHome(None, runtimeHome)
        self._token: contextvars.Token[str | None] | None = None

    def __enter__(self) -> str:
        self._token = _active_runtime_home.set(self.runtimeHome)
        return self.runtimeHome

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._token is not None:
            _active_runtime_home.reset(self._token)
            self._token = None

    async def __aenter__(self) -> str:
        return self.__enter__()

    async def __aexit__(self, exc_type, exc, tb) -> None:
        self.__exit__(exc_type, exc, tb)


def scopedRuntimeHome(runtimeHome: str) -> RuntimeHomeScope:
    return RuntimeHomeScope(runtimeHome)


def getContextRuntimeHome() -> str | None:
    return _active_runtime_home.get()


def expandHome(rawPath: str) -> str:
    if rawPath == "~":
        return str(Path.home())
    if rawPath.startswith("~/"):
        return str(Path.home() / rawPath[2:])
    return rawPath

def resolveRuntimeHome(cwd: str | None = None, candidate: str | None = None) -> str:
    raw = candidate or os.environ.get("AGENT_CORE_HOME") or DEFAULT_RUNTIME_HOME
    expanded = expandHome(raw)
    if os.path.isabs(expanded):
        return os.path.abspath(expanded)
    return os.path.abspath(os.path.join(cwd or os.getcwd(), expanded))

def configureRuntimeHome(runtimeHome: str) -> str:
    global _configured_runtime_home
    _configured_runtime_home = resolveRuntimeHome(None, runtimeHome)
    return _configured_runtime_home

def resetConfiguredRuntimeHome() -> None:
    global _configured_runtime_home
    _configured_runtime_home = None

def getStateRoot() -> str:
    return _active_runtime_home.get() or _configured_runtime_home or resolveRuntimeHome()

def findGitRoot(cwd: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=cwd,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=True,
        )
        value = result.stdout.strip()
        return value or None
    except Exception:
        return None

def getProjectRoot(cwd: str) -> str:
    return findGitRoot(cwd) or cwd

def getProjectStateDir(cwd: str) -> str:
    return os.path.join(getProjectRoot(cwd), ".tess", "project")

def getLocalStateDir(cwd: str) -> str:
    return os.path.join(cwd, ".tess", "local")

def getProjectMemoryPath(cwd: str) -> str:
    return os.path.join(getProjectStateDir(cwd), "memory.md")

def getProjectMemoryRecordsPath(cwd: str) -> str:
    return os.path.join(getProjectStateDir(cwd), "memory-records.json")

def getPlansDir() -> str:
    return os.path.join(getStateRoot(), "plans")

def getDaemonDir() -> str:
    return os.path.join(getStateRoot(), "daemon")

MAX_DAEMON_SOCKET_PATH_BYTES = 100

def daemonSocketPath() -> str:
    preferred = os.path.join(getDaemonDir(), "socket.sock")
    if len(preferred.encode("utf-8")) <= MAX_DAEMON_SOCKET_PATH_BYTES:
        return preferred
    home_hash = hashlib.sha256(getStateRoot().encode("utf-8")).hexdigest()[:12]
    return os.path.join(tempfile.gettempdir(), f"agent-core-{home_hash}.sock")

def daemonPidPath() -> str:
    return os.path.join(getDaemonDir(), "pid")

def daemonStatePath() -> str:
    return os.path.join(getDaemonDir(), "state.json")

def daemonEventsPath() -> str:
    return os.path.join(getDaemonDir(), "events.jsonl")

def daemonLogsDir() -> str:
    return os.path.join(getDaemonDir(), "logs")

def getCompactionsDir() -> str:
    return os.path.join(getStateRoot(), "compactions")

def getToolArtifactsDir() -> str:
    return os.path.join(getStateRoot(), "tool-artifacts")

def getSessionToolArtifactsDir(sessionId: str) -> str:
    return os.path.join(getToolArtifactsDir(), sessionId)

def toolArtifactMetaPath(sessionId: str, artifactId: str) -> str:
    return os.path.join(getSessionToolArtifactsDir(sessionId), f"{artifactId}.meta.json")

def toolArtifactDataPath(sessionId: str, artifactId: str, extension: str) -> str:
    ext = extension if extension.startswith(".") else f".{extension}"
    return os.path.join(getSessionToolArtifactsDir(sessionId), f"{artifactId}{ext}")

def getSessionCompactionsDir(sessionId: str) -> str:
    return os.path.join(getCompactionsDir(), sessionId)

def compactionManifestPath(sessionId: str) -> str:
    return os.path.join(getSessionCompactionsDir(sessionId), "manifest.json")

def compactionCheckpointPath(sessionId: str, generation: int) -> str:
    return os.path.join(getSessionCompactionsDir(sessionId), "checkpoints", f"{generation:04d}.json")

async def ensureBaseLayout(cwd: str) -> None:
    dirs = [
        getStateRoot(),
        getDaemonDir(),
        os.path.join(getStateRoot(), "sessions"),
        getCompactionsDir(),
        getToolArtifactsDir(),
        os.path.join(getStateRoot(), "tasks"),
        os.path.join(getStateRoot(), "task-records"),
        os.path.join(getStateRoot(), "monitors"),
        os.path.join(getStateRoot(), "agents"),
        os.path.join(getStateRoot(), "plan-approvals"),
        getPlansDir(),
        os.path.join(getStateRoot(), "remote-sessions"),
        os.path.join(getStateRoot(), "bridges"),
        os.path.join(getStateRoot(), "logs"),
        daemonLogsDir(),
        getProjectStateDir(cwd),
        getLocalStateDir(cwd),
    ]
    for directory in dirs:
        os.makedirs(directory, exist_ok=True)

expand_home = expandHome
resolve_runtime_home = resolveRuntimeHome
configure_runtime_home = configureRuntimeHome
reset_configured_runtime_home = resetConfiguredRuntimeHome
scoped_runtime_home = scopedRuntimeHome
get_context_runtime_home = getContextRuntimeHome
get_state_root = getStateRoot
find_git_root = findGitRoot
get_project_root = getProjectRoot
get_project_state_dir = getProjectStateDir
get_local_state_dir = getLocalStateDir
ensure_base_layout = ensureBaseLayout
