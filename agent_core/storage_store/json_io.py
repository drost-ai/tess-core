from __future__ import annotations

import contextlib as _contextlib
import time as _time
import weakref as _weakref
from typing import Callable

try:
    import fcntl as _fcntl
except ImportError:  # pragma: no cover - non-POSIX platforms
    _fcntl = None

from .shared import *

# Default bound for acquiring a record lock before falling back to a
# best-effort unlocked write (liveness over strict mutual exclusion).
STORAGE_LOCK_TIMEOUT_MS = 5000

_LOCK_OBSERVERS: list[Callable[[dict[str, Any]], Any]] = []

# In-process locks are scoped per running event loop: tests and embedders run
# many short-lived loops, and an asyncio.Lock cannot cross loops. Cross-loop
# (and cross-process) exclusion is the OS flock's job.
_IN_PROCESS_LOCKS: "_weakref.WeakKeyDictionary[Any, dict[str, Any]]" = _weakref.WeakKeyDictionary()


def registerStorageLockObserver(listener: Callable[[dict[str, Any]], Any]):
    """Observe storage lock timeouts; returns an unsubscribe callable.

    The runtime bootstrap forwards these to the ``storage.lock_timeout``
    runtime event so a silent fallback to unlocked writes is observable.
    """
    _LOCK_OBSERVERS.append(listener)

    def unsubscribe() -> None:
        if listener in _LOCK_OBSERVERS:
            _LOCK_OBSERVERS.remove(listener)

    return unsubscribe


def _notify_lock_observers(payload: dict[str, Any]) -> None:
    for listener in list(_LOCK_OBSERVERS):
        try:
            listener(payload)
        except Exception:
            pass


def _in_process_lock(path: str) -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    locks = _IN_PROCESS_LOCKS.get(loop)
    if locks is None:
        locks = {}
        _IN_PROCESS_LOCKS[loop] = locks
    lock = locks.get(path)
    if lock is None:
        lock = asyncio.Lock()
        locks[path] = lock
    return lock


def _try_flock(handle: Any) -> bool:
    try:
        _fcntl.flock(handle.fileno(), _fcntl.LOCK_EX | _fcntl.LOCK_NB)
        return True
    except OSError:
        return False


@_contextlib.asynccontextmanager
async def fileLock(filePath: str, *, timeoutMs: int = STORAGE_LOCK_TIMEOUT_MS):
    """Advisory lock for read-modify-write sequences on a JSON record file.

    Two layers: a per-path asyncio.Lock serializes coroutines in this process,
    then an OS ``flock`` on a ``<filePath>.lock`` sidecar serializes against
    other processes (the daemon's background children). Acquisition is bounded
    by ``timeoutMs``; on timeout the caller proceeds with a best-effort
    unlocked write and observers are notified — liveness is preferred to
    wedging the agent. The flock is taken non-blocking in a thread so the
    event loop is never blocked, and it releases automatically on process
    death (no stale-lock cleanup needed).
    """
    path = os.path.abspath(filePath)
    deadline = _time.monotonic() + max(0, int(timeoutMs)) / 1000.0
    in_process = _in_process_lock(path)
    in_process_acquired = False
    os_handle = None
    try:
        remaining = max(0.001, deadline - _time.monotonic())
        try:
            await asyncio.wait_for(in_process.acquire(), timeout=remaining)
            in_process_acquired = True
        except asyncio.TimeoutError:
            _notify_lock_observers(
                {"path": path, "layer": "in_process", "timeoutMs": int(timeoutMs), "action": "fallback_unlocked"}
            )
        if _fcntl is not None:
            directory = os.path.dirname(path)
            if directory:
                os.makedirs(directory, exist_ok=True)
            handle = open(f"{path}.lock", "a+", encoding="utf-8")
            acquired_os = False
            while True:
                if await asyncio.to_thread(_try_flock, handle):
                    acquired_os = True
                    break
                if _time.monotonic() >= deadline:
                    break
                await asyncio.sleep(0.01)
            if acquired_os:
                os_handle = handle
            else:
                handle.close()
                _notify_lock_observers(
                    {"path": path, "layer": "os", "timeoutMs": int(timeoutMs), "action": "fallback_unlocked"}
                )
        yield
    finally:
        if os_handle is not None:
            with _contextlib.suppress(Exception):
                _fcntl.flock(os_handle.fileno(), _fcntl.LOCK_UN)
            with _contextlib.suppress(Exception):
                os_handle.close()
        if in_process_acquired:
            in_process.release()


async def mutateJsonFile(filePath: str, mutate: Callable[[Any], Any], fallback: Any = None) -> Any:
    """Locked read-modify-write: load, apply ``mutate``, persist, return.

    ``mutate`` must be pure-in-effect: it receives the current value (or
    ``fallback`` when the file is missing) and returns the value to persist.
    Returning ``None`` means "I mutated in place" and persists the received
    value. The write inside the lock is the same atomic temp+``os.replace``
    as ``writeJsonFile``, so on-disk bytes are unchanged by locking.
    """
    async with fileLock(filePath):
        current = await readJsonFile(filePath, fallback)
        updated = mutate(current)
        if updated is None:
            updated = current
        await writeJsonFile(filePath, updated)
        return updated


async def readJsonFile(filePath: str, fallback: Any) -> Any:
    try:
        with open(filePath, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError:
        return fallback

async def writeJsonFile(filePath: str, value: Any) -> None:
    os.makedirs(os.path.dirname(filePath), exist_ok=True)
    tmp = f"{filePath}.{os.getpid()}.{uuid.uuid4()}.tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(value, handle, indent=2, ensure_ascii=False)
        handle.write("\n")
    os.replace(tmp, filePath)

async def appendJsonLine(filePath: str, value: Any) -> None:
    os.makedirs(os.path.dirname(filePath), exist_ok=True)
    with open(filePath, "a", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False)
        handle.write("\n")

def parseJsonString(value: str | None) -> Any:
    if not value:
        return None
    return json.loads(value)

def fileOrJson(value: str | None) -> Any:
    if not value:
        return None
    trimmed = value.strip()
    if trimmed.startswith("{") or trimmed.startswith("["):
        return json.loads(trimmed)
    with open(trimmed, "r", encoding="utf-8") as handle:
        return json.load(handle)

def resolveMaybeRelative(baseDir: str, candidate: str) -> str:
    expanded = expandHome(candidate)
    return os.path.abspath(expanded if os.path.isabs(expanded) else os.path.join(baseDir, expanded))

async def loadAuth() -> dict[str, Any] | None:
    return await readJsonFile(os.path.join(getStateRoot(), "auth.json"), None)

async def loadCodexAuthToken(authPath: str) -> str | None:
    try:
        with open(authPath, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
        api_key = str(payload.get("OPENAI_API_KEY") or "").strip()
        if api_key:
            return api_key
        token = str((payload.get("tokens") or {}).get("access_token") or "").strip()
        return token or None
    except Exception:
        return None

async def saveAuth(auth: dict[str, Any]) -> None:
    await writeJsonFile(os.path.join(getStateRoot(), "auth.json"), auth)

async def clearAuth() -> None:
    try:
        os.remove(os.path.join(getStateRoot(), "auth.json"))
    except FileNotFoundError:
        pass

read_json_file = readJsonFile
write_json_file = writeJsonFile
append_json_line = appendJsonLine
mutate_json_file = mutateJsonFile
file_lock = fileLock
resolve_maybe_relative = resolveMaybeRelative
load_auth = loadAuth
load_codex_auth_token = loadCodexAuthToken
