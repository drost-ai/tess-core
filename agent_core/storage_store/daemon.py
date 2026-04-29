from __future__ import annotations

from .shared import *

async def saveDaemonState(record: dict[str, Any]) -> None:
    await writeJsonFile(daemonStatePath(), record)
    os.makedirs(os.path.dirname(daemonPidPath()), exist_ok=True)
    with open(daemonPidPath(), "w", encoding="utf-8") as handle:
        handle.write(f"{record.get('pid')}\n")

async def loadDaemonState() -> dict[str, Any] | None:
    return await readJsonFile(daemonStatePath(), None)

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
