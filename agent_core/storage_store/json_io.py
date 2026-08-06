from __future__ import annotations

import contextlib as _contextlib
import contextvars as _contextvars
import hashlib as _hashlib
import time as _time
import weakref as _weakref
from typing import Callable

from ..persistence_metrics import (
    persistenceObserversActive as _persistence_observers_active,
    recordPersistenceIo as _record_persistence_io,
)
from .io_executor import runStorageIo as _run_storage_io

try:
    import fcntl as _fcntl
except ImportError:  # pragma: no cover - non-POSIX platforms
    _fcntl = None

from .shared import *

# Record ownership matrix (package 21, workstream D). Locking is applied only
# where multiple processes genuinely mutate the same file; single-writer
# records stay unlocked-fast with owner discipline:
#
#   Session JSON        multi-writer (parent loop, supervisor task-id updates,
#                       child prep)            -> locked save + mutateSessionRecord
#   Child-agent record  multi-writer (parent create, child-runner finalize,
#                       supervisor reconcile)  -> locked save + mutateChildAgent
#   Task meta           supervisor-owned; child finalizer and local Bash
#                       placeholder also write -> locked save + mutateTaskMeta
#   Health-check meta   supervisor-owned       -> locked save + mutateHealthCheckTask
#   Monitor record      parent-owned           -> locked save (cheap insurance)
#   Settings/auto-mode/project memory/MCP/plugin records
#                       low-contention multi-writer -> locked helper available
#   Daemon state/events cursor-based + append-only     -> unchanged by design
#
# Default bound for acquiring a record lock. A timeout fails closed: a
# successful mutation must never have raced through an unlocked fallback.
STORAGE_LOCK_TIMEOUT_MS = 5000

_LOCK_OBSERVERS: list[Callable[[dict[str, Any]], Any]] = []

# In-process locks are scoped per running event loop: tests and embedders run
# many short-lived loops, and an asyncio.Lock cannot cross loops. Cross-loop
# (and cross-process) exclusion is the OS flock's job.
_IN_PROCESS_LOCKS: "_weakref.WeakKeyDictionary[Any, dict[str, Any]]" = _weakref.WeakKeyDictionary()
_HELD_TRANSACTION_LOCKS: _contextvars.ContextVar[frozenset[str]] = _contextvars.ContextVar(
    "agent_core_held_transaction_locks",
    default=frozenset(),
)


class StorageTransactionError(RuntimeError):
    def __init__(self, code: str, message: str, lockPath: str):
        super().__init__(message)
        self.code = code
        self.lockPath = lockPath


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


def _open_lock_handle(path: str) -> Any:
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    return open(f"{path}.lock", "a+", encoding="utf-8")


def _release_lock_handle(handle: Any) -> None:
    try:
        if _fcntl is not None:
            _fcntl.flock(handle.fileno(), _fcntl.LOCK_UN)
    finally:
        handle.close()


@_contextlib.asynccontextmanager
async def fileLock(filePath: str, *, timeoutMs: int = STORAGE_LOCK_TIMEOUT_MS):
    """Advisory lock for read-modify-write sequences on a JSON record file.

    Two layers: a per-path asyncio.Lock serializes coroutines in this process,
    then an OS ``flock`` on a ``<filePath>.lock`` sidecar serializes against
    other processes (the daemon's background children). Acquisition is bounded
    by ``timeoutMs``; a timeout is observable and raises
    ``StorageTransactionError`` without entering the caller's critical
    section. The flock is taken non-blocking in a thread so the event loop is
    never blocked, and it releases automatically on process death.
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
            payload = {
                "path": path,
                "layer": "in_process",
                "timeoutMs": int(timeoutMs),
                "action": "failed_closed",
            }
            _notify_lock_observers(payload)
            raise StorageTransactionError(
                "storage.lock_timeout",
                f"Timed out waiting for record lock after {int(timeoutMs)}ms.",
                f"{path}.lock",
            )
        if _fcntl is not None:
            handle = await _run_storage_io(
                _open_lock_handle,
                path,
                cancelCleanup=_release_lock_handle,
            )
            os_handle = handle
            acquired_os = False
            while True:
                if await _run_storage_io(_try_flock, handle):
                    acquired_os = True
                    break
                if _time.monotonic() >= deadline:
                    break
                await asyncio.sleep(0.01)
            if not acquired_os:
                payload = {
                    "path": path,
                    "layer": "os",
                    "timeoutMs": int(timeoutMs),
                    "action": "failed_closed",
                }
                _notify_lock_observers(payload)
                raise StorageTransactionError(
                    "storage.lock_timeout",
                    f"Timed out waiting for record lock after {int(timeoutMs)}ms.",
                    f"{path}.lock",
                )
        yield
    finally:
        try:
            if os_handle is not None:
                with _contextlib.suppress(Exception):
                    await _run_storage_io(_release_lock_handle, os_handle)
        finally:
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


def transactionLockPath(namespace: str, key: str) -> str:
    if not re.fullmatch(r"[a-z][a-z0-9._-]{0,63}", namespace):
        raise ValueError(f"Invalid transaction namespace: {namespace}")
    if not isinstance(key, str) or not key or len(key.encode("utf-8")) > 1024:
        raise ValueError("Transaction key must contain 1-1024 UTF-8 bytes.")
    digest = _hashlib.sha256(key.encode("utf-8")).hexdigest()
    return os.path.join(getStateRoot(), "locks", f"{namespace}-{digest}")


@_contextlib.asynccontextmanager
async def withRecordTransaction(
    namespace: str,
    key: str,
    *,
    timeoutMs: int = STORAGE_LOCK_TIMEOUT_MS,
):
    lock_path = transactionLockPath(namespace, key)
    held = _HELD_TRANSACTION_LOCKS.get()
    if lock_path in held:
        yield
        return
    async with fileLock(lock_path, timeoutMs=timeoutMs):
        token = _HELD_TRANSACTION_LOCKS.set(held | {lock_path})
        try:
            yield
        finally:
            _HELD_TRANSACTION_LOCKS.reset(token)


async def mutateJsonRecord(
    *,
    namespace: str,
    key: str,
    filePath: str,
    fallback: Any,
    mutate: Callable[[Any], Any],
    timeoutMs: int = STORAGE_LOCK_TIMEOUT_MS,
) -> dict[str, Any]:
    lock_path = transactionLockPath(namespace, key)
    async with withRecordTransaction(namespace, key, timeoutMs=timeoutMs):
        current = await readJsonFile(filePath, fallback)
        result = mutate(current)
        if hasattr(result, "__await__"):
            result = await result
        if not isinstance(result, dict) or "changed" not in result or "value" not in result:
            raise TypeError("Record mutation must return {'value': ..., 'changed': bool}.")
        changed = bool(result["changed"])
        if changed:
            await writeJsonFile(filePath, result["value"])
        return {"value": result["value"], "changed": changed, "lockPath": f"{lock_path}.lock"}


def _read_json_file_sync(file_path: str, fallback: Any) -> Any:
    try:
        with open(file_path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError:
        return fallback


async def readJsonFile(filePath: str, fallback: Any) -> Any:
    return await _run_storage_io(_read_json_file_sync, filePath, fallback)


def _write_json_file_sync(file_path: str, value: Any) -> None:
    measure = _persistence_observers_active()
    started_ns = _time.perf_counter_ns() if measure else 0
    os.makedirs(os.path.dirname(file_path), exist_ok=True)
    tmp = f"{file_path}.{os.getpid()}.{uuid.uuid4()}.tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(value, handle, indent=2, ensure_ascii=False)
        handle.write("\n")
    byte_count = os.path.getsize(tmp) if measure else 0
    os.replace(tmp, file_path)
    if measure:
        _record_persistence_io("write", file_path, byte_count, _time.perf_counter_ns() - started_ns)


async def writeJsonFile(filePath: str, value: Any) -> None:
    await _run_storage_io(_write_json_file_sync, filePath, value)


def _append_json_line_sync(file_path: str, value: Any) -> None:
    measure = _persistence_observers_active()
    started_ns = _time.perf_counter_ns() if measure else 0
    directory = os.path.dirname(file_path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    previous_size = os.path.getsize(file_path) if measure and os.path.exists(file_path) else 0
    # json.dump may issue many small writes.  Serialize before opening the
    # append handle so one record is emitted with one write while the record
    # transaction is held.
    encoded = json.dumps(value, ensure_ascii=False) + "\n"
    with open(file_path, "a", encoding="utf-8") as handle:
        handle.write(encoded)
    if measure:
        byte_count = os.path.getsize(file_path) - previous_size
        _record_persistence_io("append", file_path, byte_count, _time.perf_counter_ns() - started_ns)


async def appendJsonLine(filePath: str, value: Any) -> None:
    path = os.path.abspath(filePath)
    async with withRecordTransaction("jsonl", path):
        await _run_storage_io(_append_json_line_sync, path, value)


def _read_json_lines_sync(file_path: str) -> list[dict[str, Any]]:
    try:
        with open(file_path, "r", encoding="utf-8") as handle:
            lines = handle.readlines()
    except FileNotFoundError:
        return []
    records: list[dict[str, Any]] = []
    for line in lines:
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            # Preserve the historical tolerant-reader contract for records
            # written by older runtimes.
            continue
        if isinstance(value, dict):
            records.append(value)
    return records


async def readJsonLines(filePath: str) -> list[dict[str, Any]]:
    path = os.path.abspath(filePath)
    async with withRecordTransaction("jsonl", path):
        return await _run_storage_io(_read_json_lines_sync, path)


def _write_json_lines_sync(file_path: str, records: list[dict[str, Any]]) -> None:
    measure = _persistence_observers_active()
    started_ns = _time.perf_counter_ns() if measure else 0
    directory = os.path.dirname(file_path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    tmp = f"{file_path}.{os.getpid()}.{uuid.uuid4()}.tmp"
    encoded = "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records)
    with open(tmp, "w", encoding="utf-8") as handle:
        handle.write(encoded)
    byte_count = os.path.getsize(tmp) if measure else 0
    os.replace(tmp, file_path)
    if measure:
        _record_persistence_io("write", file_path, byte_count, _time.perf_counter_ns() - started_ns)


async def mutateJsonLines(filePath: str, mutate: Callable[[list[dict[str, Any]]], Any]) -> Any:
    """Atomically read, mutate, and rewrite one JSONL record.

    The callback returns ``{'records': ..., 'result': ..., 'changed': bool}``.
    The transaction is shared with :func:`appendJsonLine`, including across
    processes, so an append cannot be lost behind a concurrent rewrite.
    """

    path = os.path.abspath(filePath)
    async with withRecordTransaction("jsonl", path):
        records = await _run_storage_io(_read_json_lines_sync, path)
        mutation = mutate(records)
        if hasattr(mutation, "__await__"):
            mutation = await mutation
        if not isinstance(mutation, dict) or not {"records", "result", "changed"} <= mutation.keys():
            raise TypeError("JSONL mutation must return records, result, and changed.")
        updated = mutation["records"]
        if not isinstance(updated, list) or not all(isinstance(item, dict) for item in updated):
            raise TypeError("JSONL mutation records must be a list of objects.")
        if bool(mutation["changed"]):
            await _run_storage_io(_write_json_lines_sync, path, updated)
        return mutation["result"]

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
    auth = await readJsonFile(os.path.join(getStateRoot(), "auth.json"), None)
    if not auth:
        return None
    from ..runtime.secret_provider import migrateLegacyStoredAuthSecrets

    migration = await migrateLegacyStoredAuthSecrets(auth)
    if migration["changed"]:
        await writeJsonFile(os.path.join(getStateRoot(), "auth.json"), migration["auth"])
    return migration["auth"]

async def loadCodexAuthToken(authPath: str) -> str | None:
    return await _run_storage_io(_load_codex_auth_token_sync, authPath)


def _load_codex_auth_token_sync(auth_path: str) -> str | None:
    try:
        with open(auth_path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
        api_key = str(payload.get("OPENAI_API_KEY") or "").strip()
        if api_key:
            return api_key
        token = str((payload.get("tokens") or {}).get("access_token") or "").strip()
        return token or None
    except Exception:
        return None

async def saveAuth(auth: dict[str, Any]) -> None:
    from ..runtime.secret_provider import migrateLegacyStoredAuthSecrets

    normalized = await migrateLegacyStoredAuthSecrets(auth)
    await writeJsonFile(os.path.join(getStateRoot(), "auth.json"), normalized["auth"])

async def clearAuth() -> None:
    await _run_storage_io(_clear_auth_sync, os.path.join(getStateRoot(), "auth.json"))


def _clear_auth_sync(auth_path: str) -> None:
    try:
        os.remove(auth_path)
    except FileNotFoundError:
        pass

read_json_file = readJsonFile
write_json_file = writeJsonFile
append_json_line = appendJsonLine
read_json_lines = readJsonLines
mutate_json_lines = mutateJsonLines
mutate_json_file = mutateJsonFile
mutate_json_record = mutateJsonRecord
file_lock = fileLock
with_record_transaction = withRecordTransaction
transaction_lock_path = transactionLockPath
resolve_maybe_relative = resolveMaybeRelative
load_auth = loadAuth
load_codex_auth_token = loadCodexAuthToken
