from __future__ import annotations
from .._utils import note_swallowed

import contextlib as _contextlib
import contextvars as _contextvars
import hashlib as _hashlib
import random as _random
import time as _time
import weakref as _weakref
from typing import Callable

from .persistence_metrics import (
    persistence_observers_active as _persistence_observers_active,
    record_persistence_io as _record_persistence_io,
)
from .io_executor import run_storage_io as _run_storage_io
import asyncio
import json
import os
import re
import uuid
from typing import Any

from .paths import expand_home, get_state_root

try:
    import fcntl as _fcntl
except ImportError:  # pragma: no cover - non-POSIX platforms
    _fcntl = None


# Record ownership matrix. Multi-writer record families serialize through a
# NAMED record-transaction domain keyed by record id (see storage/records.py);
# per-data-file sidecar locks are not used for records, because a lock that
# holders of the family transaction do not take excludes nothing:
#
#   Task record + task meta + health-check meta
#                       one lifecycle unit -> shared "task-lifecycle" domain
#   Child-agent record  multi-writer       -> "agent-record" domain
#   Monitor record      parent-owned       -> "monitor-record" domain
#   Session JSON        multi-writer       -> locked save + mutate_session_record
#   Settings/auto-mode/project memory/MCP/plugin records
#                       low-contention multi-writer -> locked helper available
#   Daemon state/events cursor-based + append-only  -> unchanged by design
#
# Default bound for acquiring a record lock. A timeout fails closed: a
# successful mutation must never have raced through an unlocked fallback.
def _storage_lock_timeout_ms() -> int:
    # Call-time import: config resolution imports storage at load.
    from ..config._knobs import knob_value

    return int(knob_value("storage.lock_timeout_ms"))

_LOCK_OBSERVERS: list[Callable[[dict[str, Any]], Any]] = []

# In-process locks are scoped per running event loop: tests and embedders run
# many short-lived loops, and an asyncio.Lock cannot cross loops. Cross-loop
# (and cross-process) exclusion is the OS flock's job.
_IN_PROCESS_LOCKS: "_weakref.WeakKeyDictionary[Any, dict[str, Any]]" = _weakref.WeakKeyDictionary()
_HELD_TRANSACTION_LOCKS: _contextvars.ContextVar[frozenset[tuple[str, int]]] = _contextvars.ContextVar(
    "agent_core_held_transaction_locks",
    default=frozenset(),
)


class StorageTransactionError(RuntimeError):
    def __init__(self, code: str, message: str, lockPath: str):
        super().__init__(message)
        self.code = code
        self.lockPath = lockPath


def register_storage_lock_observer(listener: Callable[[dict[str, Any]], Any]):
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


def in_process_lock(path: str) -> asyncio.Lock:
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
async def file_lock(filePath: str, *, timeoutMs: int | None = None):
    """Advisory lock for read-modify-write sequences on a JSON record file.

    Two layers: a per-path asyncio.Lock serializes coroutines in this process,
    then an OS ``flock`` on a ``<filePath>.lock`` sidecar serializes against
    other processes (the daemon's background children). Acquisition is bounded
    by ``timeoutMs``; a timeout is observable and raises
    ``StorageTransactionError`` without entering the caller's critical
    section. The flock is probed non-blocking (a microsecond syscall, safe
    inline on the loop) with jittered backoff between probes, and it releases
    automatically on process death.
    """
    path = os.path.abspath(filePath)
    if timeoutMs is None:
        timeoutMs = _storage_lock_timeout_ms()
    deadline = _time.monotonic() + max(0, int(timeoutMs)) / 1000.0
    in_process = in_process_lock(path)
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
            # Probe inline, not through the storage executor: its few threads
            # are busy doing the very writes the waiters queue behind, so
            # executor-routed probes amplify contention into a convoy. Full
            # jitter desynchronizes waiters so acquisition is not a
            # fixed-cadence lottery that starves unlucky processes.
            delay = 0.001
            while True:
                if _try_flock(handle):
                    acquired_os = True
                    break
                now = _time.monotonic()
                if now >= deadline:
                    break
                await asyncio.sleep(min(_random.uniform(0.0005, delay), deadline - now))
                delay = min(delay * 1.7, 0.04)
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


async def mutate_json_file(filePath: str, mutate: Callable[[Any], Any], fallback: Any = None) -> Any:
    """Locked read-modify-write: load, apply ``mutate``, persist, return.

    ``mutate`` must be pure-in-effect: it receives the current value (or
    ``fallback`` when the file is missing) and returns the value to persist.
    Returning ``None`` means "I mutated in place" and persists the received
    value. The write inside the lock is the same atomic temp+``os.replace``
    as ``write_json_file``, so on-disk bytes are unchanged by locking.
    """
    async with file_lock(filePath):
        current = await read_json_file(filePath, fallback)
        updated = mutate(current)
        if updated is None:
            updated = current
        await write_json_file(filePath, updated)
        return updated


def transaction_lock_path(namespace: str, key: str) -> str:
    if not re.fullmatch(r"[a-z][a-z0-9._-]{0,63}", namespace):
        raise ValueError(f"Invalid transaction namespace: {namespace}")
    if not isinstance(key, str) or not key or len(key.encode("utf-8")) > 1024:
        raise ValueError("Transaction key must contain 1-1024 UTF-8 bytes.")
    digest = _hashlib.sha256(key.encode("utf-8")).hexdigest()
    return os.path.join(get_state_root(), "locks", f"{namespace}-{digest}")


@_contextlib.asynccontextmanager
async def with_record_transaction(
    namespace: str,
    key: str,
    *,
    timeoutMs: int | None = None,
):
    lock_path = transaction_lock_path(namespace, key)
    # Reentrancy is per-task: a task spawned while the parent holds the lock
    # inherits the parent's context copy, and must not treat the parent's
    # holdings as its own — that would run it unlocked alongside the parent.
    holder = (lock_path, id(asyncio.current_task()))
    held = _HELD_TRANSACTION_LOCKS.get()
    if holder in held:
        yield
        return
    async with file_lock(lock_path, timeoutMs=timeoutMs):
        token = _HELD_TRANSACTION_LOCKS.set(held | {holder})
        try:
            yield
        finally:
            _HELD_TRANSACTION_LOCKS.reset(token)


async def mutate_json_record(
    *,
    namespace: str,
    key: str,
    filePath: str,
    fallback: Any,
    mutate: Callable[[Any], Any],
    timeoutMs: int | None = None,
) -> dict[str, Any]:
    lock_path = transaction_lock_path(namespace, key)
    async with with_record_transaction(namespace, key, timeoutMs=timeoutMs):
        current = await read_json_file(filePath, fallback)
        result = mutate(current)
        if hasattr(result, "__await__"):
            result = await result
        if not isinstance(result, dict) or "changed" not in result or "value" not in result:
            raise TypeError("Record mutation must return {'value': ..., 'changed': bool}.")
        changed = bool(result["changed"])
        if changed:
            await write_json_file(filePath, result["value"])
        return {"value": result["value"], "changed": changed, "lockPath": f"{lock_path}.lock"}


def _read_json_file_sync(file_path: str, fallback: Any) -> Any:
    try:
        with open(file_path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError:
        return fallback


async def read_json_file(filePath: str, fallback: Any) -> Any:
    return await _run_storage_io(_read_json_file_sync, filePath, fallback)


def _read_json_file_tolerant_sync(file_path: str, fallback: Any) -> Any:
    try:
        return _read_json_file_sync(file_path, fallback)
    except (OSError, ValueError):
        return fallback


async def read_json_file_tolerant(filePath: str, fallback: Any) -> Any:
    """Degrade a torn or unreadable record to ``fallback`` instead of raising —
    only for records recovery can rebuild (e.g. v2 heads from body + archive)."""
    return await _run_storage_io(_read_json_file_tolerant_sync, filePath, fallback)


def _fsync_directory_sync(directory: str) -> None:
    try:
        fd = os.open(directory or ".", os.O_RDONLY)
    except OSError:  # pragma: no cover - platforms without directory fds
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_json_file_sync(file_path: str, value: Any) -> None:
    measure = _persistence_observers_active()
    started_ns = _time.perf_counter_ns() if measure else 0
    os.makedirs(os.path.dirname(file_path), exist_ok=True)
    tmp = f"{file_path}.{os.getpid()}.{uuid.uuid4()}.tmp"
    # Heads and records must never read back torn after a crash: fsync the
    # payload before the atomic rename and the directory entry after it.
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(value, handle, indent=2, ensure_ascii=False)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    byte_count = os.path.getsize(tmp) if measure else 0
    os.replace(tmp, file_path)
    _fsync_directory_sync(os.path.dirname(file_path))
    if measure:
        _record_persistence_io("write", file_path, byte_count, _time.perf_counter_ns() - started_ns)


async def write_json_file(filePath: str, value: Any) -> None:
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


async def append_json_line(filePath: str, value: Any) -> None:
    path = os.path.abspath(filePath)
    async with with_record_transaction("jsonl", path):
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


async def read_json_lines(filePath: str) -> list[dict[str, Any]]:
    """Tolerant lock-free JSONL read.

    Appends emit one line per write and rewrites replace atomically, so the
    worst case for an unlocked reader is a torn tail line, skipped by the
    tolerant parser until the next read observes it complete. Polling readers
    must not contend with writers for the record transaction;
    read-modify-write callers use :func:`mutate_json_lines`, which does.
    """
    path = os.path.abspath(filePath)
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


async def mutate_json_lines(filePath: str, mutate: Callable[[list[dict[str, Any]]], Any]) -> Any:
    """Atomically read, mutate, and rewrite one JSONL record.

    The callback returns ``{'records': ..., 'result': ..., 'changed': bool}``.
    The transaction is shared with :func:`append_json_line`, including across
    processes, so an append cannot be lost behind a concurrent rewrite.
    """

    path = os.path.abspath(filePath)
    async with with_record_transaction("jsonl", path):
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

def parse_json_string(value: str | None) -> Any:
    if not value:
        return None
    return json.loads(value)

def file_or_json(value: str | None) -> Any:
    if not value:
        return None
    trimmed = value.strip()
    if trimmed.startswith("{") or trimmed.startswith("["):
        return json.loads(trimmed)
    with open(trimmed, "r", encoding="utf-8") as handle:
        return json.load(handle)

def resolve_maybe_relative(baseDir: str, candidate: str) -> str:
    expanded = expand_home(candidate)
    return os.path.abspath(expanded if os.path.isabs(expanded) else os.path.join(baseDir, expanded))
