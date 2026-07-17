from __future__ import annotations

from .shared import *
from ..persisted_record_scanner import readPersistedJsonRecord
from ..persisted_record_validation import isDaemonStateRecord

DAEMON_EVENTS_TAIL_BYTES = 64 * 1024
DAEMON_EVENTS_ROTATE_BYTES = 32 * 1024 * 1024

async def saveDaemonState(record: dict[str, Any]) -> None:
    await writeJsonFile(daemonStatePath(), record)
    os.makedirs(os.path.dirname(daemonPidPath()), exist_ok=True)
    with open(daemonPidPath(), "w", encoding="utf-8") as handle:
        handle.write(f"{record.get('pid')}\n")

async def loadDaemonState() -> dict[str, Any] | None:
    result = await readPersistedJsonRecord(
        family="daemon_state",
        filePath=daemonStatePath(),
        validate=isDaemonStateRecord,
    )
    return result.get("record")

async def clearDaemonState() -> None:
    for path in (daemonPidPath(), daemonStatePath(), daemonSocketPath()):
        try:
            os.remove(path)
        except FileNotFoundError:
            pass

async def appendDaemonEvent(record: dict[str, Any]) -> None:
    await appendJsonLine(daemonEventsPath(), record)

async def loadDaemonEvents() -> list[dict[str, Any]]:
    try:
        with open(daemonEventsPath(), "r", encoding="utf-8") as handle:
            return [json.loads(line) for line in handle if line.strip()]
    except Exception:
        return []

async def readLastDaemonEventCursor() -> int:
    live = await _read_last_daemon_cursor_from_tail(daemonEventsPath())
    if live > 0:
        return live
    return await _read_last_daemon_cursor_from_tail(f"{daemonEventsPath()}.1")

async def rotateDaemonEventsIfOversized(capBytes: int = DAEMON_EVENTS_ROTATE_BYTES) -> bool:
    file_path = daemonEventsPath()
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
        read_bytes = min(size, DAEMON_EVENTS_TAIL_BYTES)
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
