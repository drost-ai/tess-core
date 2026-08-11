from __future__ import annotations

import contextlib as _contextlib

import asyncio
import json
import os
import uuid
from typing import Any

from .._utils import is_process_alive as _is_process_alive
from .json_io import withRecordTransaction
from .paths import (
    daemonEventsPath,
    daemonPidPath,
    daemonSocketPath,
    daemonStatePath,
    getDaemonInstancesDir,
    getStateRoot,
    parseDaemonInstanceId,
)
from .persisted_record_scanner import readPersistedJsonRecord
from .persisted_record_validation import isDaemonStateRecord


async def saveDaemonState(instanceId: str, record: dict[str, Any]) -> None:
    await asyncio.to_thread(_write_private_json, daemonStatePath(instanceId), record)
    await asyncio.to_thread(_write_private_bytes, daemonPidPath(instanceId), f"{record.get('pid')}\n".encode("utf-8"))

async def loadDaemonState(instanceId: str) -> dict[str, Any] | None:
    result = await readPersistedJsonRecord(
        family="daemon_state",
        filePath=daemonStatePath(instanceId),
        validate=isDaemonStateRecord,
    )
    return result.get("record")

async def clearDaemonState(instanceId: str) -> None:
    for path in (daemonPidPath(instanceId), daemonStatePath(instanceId), daemonSocketPath(instanceId)):
        try:
            os.remove(path)
        except FileNotFoundError:
            pass

async def listDaemonInstanceIds() -> list[str]:
    try:
        names = os.listdir(getDaemonInstancesDir())
    except FileNotFoundError:
        return []
    instance_ids: list[str] = []
    for name in sorted(names):
        try:
            instance_ids.append(parseDaemonInstanceId(name))
        except ValueError:
            continue
    return instance_ids


async def listLiveDaemonStates() -> list[dict[str, Any]]:
    states: list[dict[str, Any]] = []
    for instance_id in await listDaemonInstanceIds():
        state = await loadDaemonState(instance_id)
        if state and isinstance(state.get("pid"), int) and _is_process_alive(state["pid"]):
            states.append(state)
    return states


async def appendDaemonEvent(instanceId: str, record: dict[str, Any]) -> None:
    async with withRecordTransaction(f"daemon-events-{parseDaemonInstanceId(instanceId)}", getStateRoot()):
        next_record = dict(record)
        if not isinstance(next_record.get("cursor"), int):
            next_record["cursor"] = await readLastDaemonEventCursor(instanceId) + 1
        payload = json.dumps(next_record, ensure_ascii=False, separators=(",", ":")) + "\n"
        await asyncio.to_thread(
            _append_private_bytes,
            daemonEventsPath(instanceId),
            payload.encode("utf-8"),
        )

async def loadDaemonEvents(instanceId: str) -> list[dict[str, Any]]:
    try:
        with open(daemonEventsPath(instanceId), "r", encoding="utf-8") as handle:
            return [json.loads(line) for line in handle if line.strip()]
    except Exception:
        return []

async def readLastDaemonEventCursor(instanceId: str) -> int:
    live = await _read_last_daemon_cursor_from_tail(daemonEventsPath(instanceId))
    if live > 0:
        return live
    return await _read_last_daemon_cursor_from_tail(f"{daemonEventsPath(instanceId)}.1")

async def rotateDaemonEventsIfOversized(instanceId: str, capBytes: int | None = None) -> bool:
    if capBytes is None:
        # Call-time import: config resolution imports storage at load.
        from ..config._knobs import knobValue

        capBytes = int(knobValue("daemon.events_rotate_bytes"))
    file_path = daemonEventsPath(instanceId)
    try:
        size = os.path.getsize(file_path)
    except OSError:
        return False
    if size <= capBytes:
        return False
    rotated = f"{file_path}.1"
    try:
        os.remove(rotated)
    except FileNotFoundError:
        pass
    os.rename(file_path, rotated)
    return True

async def _read_last_daemon_cursor_from_tail(file_path: str) -> int:
    try:
        size = os.path.getsize(file_path)
        if size <= 0:
            return 0
        from ..config._knobs import knobValue

        read_bytes = min(size, int(knobValue("daemon.events_tail_bytes")))
        with open(file_path, "rb") as handle:
            handle.seek(size - read_bytes)
            raw = handle.read(read_bytes).decode("utf-8", errors="replace")
    except Exception:
        return 0
    for line in reversed([line.strip() for line in raw.split("\n") if line.strip()]):
        try:
            record = json.loads(line)
        except Exception:
            continue
        cursor = record.get("cursor") if isinstance(record, dict) else None
        if isinstance(cursor, (int, float)):
            return int(cursor)
    return 0


def _write_private_json(file_path: str, value: Any) -> None:
    payload = (json.dumps(value, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    directory = os.path.dirname(file_path)
    os.makedirs(directory, mode=0o700, exist_ok=True)
    temp = f"{file_path}.{os.getpid()}.{uuid.uuid4()}.tmp"
    try:
        _write_private_bytes(temp, payload, exclusive=True)
        os.replace(temp, file_path)
        os.chmod(file_path, 0o600)
    finally:
        with _contextlib.suppress(FileNotFoundError):
            os.unlink(temp)


def _write_private_bytes(file_path: str, payload: bytes, *, exclusive: bool = False) -> None:
    os.makedirs(os.path.dirname(file_path), mode=0o700, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | (os.O_EXCL if exclusive else os.O_TRUNC)
    fd = os.open(file_path, flags, 0o600)
    try:
        os.fchmod(fd, 0o600)
        _write_all(fd, payload)
        os.fsync(fd)
    finally:
        os.close(fd)


def _append_private_bytes(file_path: str, payload: bytes) -> None:
    os.makedirs(os.path.dirname(file_path), mode=0o700, exist_ok=True)
    fd = os.open(file_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.fchmod(fd, 0o600)
        _write_all(fd, payload)
    finally:
        os.close(fd)


def _write_all(fd: int, payload: bytes) -> None:
    view = memoryview(payload)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise OSError("Private daemon record write made no progress.")
        view = view[written:]
