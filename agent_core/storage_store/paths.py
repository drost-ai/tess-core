from __future__ import annotations

import hashlib
import tempfile

from .shared import *
from ..runtime_identifiers import parseArtifactId, parseSessionId
from ..runtime_paths import (
    RuntimePaths,
    RuntimePathsScope,
    configureActiveRuntimePaths,
    getActiveRuntimePaths,
    getScopedRuntimePaths,
    resetActiveRuntimePaths,
    resolveRuntimePaths,
)

Scope = Literal["user", "project", "local"]
DEFAULT_RUNTIME_HOME = "~/.agent-core"
class RuntimeHomeScope:
    def __init__(self, runtimeHome: str):
        self.runtimeHome = resolveRuntimeHome(None, runtimeHome)
        self._scope = RuntimePathsScope(resolveRuntimePaths(candidate=self.runtimeHome), immutable=False)

    def __enter__(self) -> str:
        self._scope.__enter__()
        return self.runtimeHome

    def __exit__(self, exc_type, exc, tb) -> None:
        self._scope.__exit__(exc_type, exc, tb)

    async def __aenter__(self) -> str:
        return self.__enter__()

    async def __aexit__(self, exc_type, exc, tb) -> None:
        self.__exit__(exc_type, exc, tb)


def scopedRuntimeHome(runtimeHome: str) -> RuntimeHomeScope:
    return RuntimeHomeScope(runtimeHome)


def getContextRuntimeHome() -> str | None:
    scoped = getScopedRuntimePaths()
    return scoped.home if scoped is not None else None


def expandHome(rawPath: str) -> str:
    if rawPath == "~":
        return str(Path.home())
    if rawPath.startswith("~/"):
        return str(Path.home() / rawPath[2:])
    return rawPath

def resolveRuntimeHome(cwd: str | None = None, candidate: str | None = None) -> str:
    return resolveRuntimePaths(cwd, candidate).home

def configureRuntimeHome(runtimeHome: str) -> str:
    return configureActiveRuntimePaths(runtimeHome).home

def resetConfiguredRuntimeHome() -> None:
    resetActiveRuntimePaths()

def getStateRoot() -> str:
    return getActiveRuntimePaths().home

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
    return os.path.join(getToolArtifactsDir(), parseSessionId(sessionId))

def toolArtifactMetaPath(sessionId: str, artifactId: str) -> str:
    return os.path.join(
        getSessionToolArtifactsDir(sessionId),
        f"{parseArtifactId(artifactId)}.meta.json",
    )

def toolArtifactDataPath(sessionId: str, artifactId: str, extension: str) -> str:
    ext = extension if extension.startswith(".") else f".{extension}"
    if not re.fullmatch(r"\.[A-Za-z0-9]{1,16}", ext):
        raise ValueError("Invalid artifact extension")
    return os.path.join(
        getSessionToolArtifactsDir(sessionId),
        f"{parseArtifactId(artifactId)}{ext}",
    )

def getSessionCompactionsDir(sessionId: str) -> str:
    return os.path.join(getCompactionsDir(), parseSessionId(sessionId))

def compactionManifestPath(sessionId: str) -> str:
    return os.path.join(getSessionCompactionsDir(sessionId), "manifest.json")

def compactionCheckpointPath(sessionId: str, generation: int) -> str:
    return os.path.join(getSessionCompactionsDir(sessionId), "checkpoints", f"{generation:04d}.json")

async def ensureBaseLayout(cwd: str, paths: RuntimePaths | None = None) -> None:
    graph = paths or getActiveRuntimePaths()
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
        graph.planApprovalsDir,
        graph.plansDir,
        graph.remoteSessionsDir,
        graph.bridgesDir,
        graph.logsDir,
        graph.daemonLogsDir,
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
    for directory in (getProjectStateDir(cwd), getLocalStateDir(cwd)):
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
