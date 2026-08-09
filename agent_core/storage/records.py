from __future__ import annotations

from ..contracts.records import (
    ChildAgentRecord,
    MonitorRecord,
    TaskMeta,
    TaskRecord,
)
from ..runtime.identifiers import (
    RuntimeIdentifierError,
    parseAgentId,
    parseMonitorId,
    parseTaskId,
    tryParseAgentId,
    tryParseMonitorId,
    tryParseTaskId,
)
from ..session.persisted_record_scanner import readPersistedJsonRecord, scanPersistedJsonRecords
from ..session.persisted_record_validation import (
    isChildAgentRecord,
    isHealthCheckTaskRecord,
    isMonitorRecord,
    isTaskMetaRecord,
    isTaskRecord,
)

import os
from typing import Any

from .json_io import readJsonFile, withRecordTransaction, writeJsonFile
from .paths import getStateRoot

# Every record mutation acquires its family's record transaction, keyed by
# record id. Task record, task meta, and health-check meta are one lifecycle
# unit sharing the "task-lifecycle" domain: a holder of that transaction
# (terminal_store) excludes ALL writers of all three files, not only other
# transaction holders. The transaction is reentrant within a task context, so
# these writers compose with an enclosing transaction instead of deadlocking.

def taskDir(taskId: str) -> str:
    return os.path.join(getStateRoot(), "tasks", parseTaskId(taskId))

def taskRecordPath(taskId: str) -> str:
    return os.path.join(getStateRoot(), "task-records", f"{parseTaskId(taskId)}.json")

def taskMetaPath(taskId: str) -> str:
    return os.path.join(taskDir(taskId), "meta.json")

def taskOutputPath(taskId: str) -> str:
    return os.path.join(taskDir(taskId), "output.log")

def healthCheckMetaPath(taskId: str) -> str:
    return os.path.join(taskDir(taskId), "health-check.json")

def monitorPath(monitorId: str) -> str:
    return os.path.join(getStateRoot(), "monitors", f"{parseMonitorId(monitorId)}.json")

def childAgentPath(agentId: str) -> str:
    return os.path.join(getStateRoot(), "agents", f"{parseAgentId(agentId)}.json")

async def _save_by_path(path: str, record: dict[str, Any]) -> None:
    await writeJsonFile(path, record)

async def _load_by_path(path: str) -> dict[str, Any] | None:
    return await readJsonFile(path, None)

def _omit_none_fields(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _omit_none_fields(item) for key, item in value.items() if item is not None}
    if isinstance(value, list):
        return [_omit_none_fields(item) for item in value]
    return value


_CHILD_AGENT_STATUS_RANK = {
    "queued": 1,
    "running": 1,
    "backgrounded": 1,
    "awaiting_input": 2,
    "completed": 3,
    "failed": 3,
    "cancelled": 3,
    "stopped": 3,
    "truncated": 3,
}

_CHILD_AGENT_COMPLETION_FIELDS = {
    "completedAt",
    "completionReason",
    "error",
    "lastSummary",
    "lastUpdatedAt",
    "latestResultAt",
    "recoveryState",
    "resultSummary",
    "retryable",
    "status",
    "stoppable",
    "stopReason",
    "waitingReason",
}

_CHILD_AGENT_LAUNCH_FIELDS = {
    "attachableSessionId",
    "owner",
    "pid",
    "runInBackground",
    "taskId",
}


def _merge_child_agent_for_full_save(current: dict[str, Any] | None, incoming: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(current, dict) or current.get("agentId") != incoming.get("agentId"):
        return incoming
    current_rank = _CHILD_AGENT_STATUS_RANK.get(str(current.get("status")), 0)
    incoming_rank = _CHILD_AGENT_STATUS_RANK.get(str(incoming.get("status")), 0)
    if current_rank > incoming_rank:
        merged = {**incoming, **current}
        for key in _CHILD_AGENT_LAUNCH_FIELDS:
            if (merged.get(key) is None or merged.get(key) == "") and incoming.get(key) is not None:
                merged[key] = incoming[key]
        return merged
    merged = {**current, **incoming}
    if current_rank == incoming_rank:
        for key in _CHILD_AGENT_COMPLETION_FIELDS:
            if incoming.get(key) is None and current.get(key) is not None:
                merged[key] = current[key]
    return merged

async def _list_json_records(directory: str, loader) -> list[dict[str, Any]]:
    try:
        names = [name for name in os.listdir(directory) if name.endswith(".json")]
    except FileNotFoundError:
        return []
    records = []
    for name in names:
        try:
            value = await loader(name[:-5])
        except RuntimeIdentifierError:
            continue
        if value:
            records.append(value)
    return records


def _json_candidate(directory: str, entry: os.DirEntry[str], parse_id) -> dict[str, str] | None:
    if not entry.name.endswith(".json"):
        return None
    record_id = parse_id(entry.name[:-5])
    if record_id is None:
        return None
    return {"id": record_id, "filePath": os.path.join(directory, entry.name)}

async def saveChildAgent(record: ChildAgentRecord) -> None:
    agent_id = parseAgentId(record["agentId"])
    path = childAgentPath(agent_id)
    async with withRecordTransaction("agent-record", agent_id):
        current = await _load_by_path(path)
        await writeJsonFile(path, _omit_none_fields(_merge_child_agent_for_full_save(current, record)))


async def mutateChildAgent(agentId: str, mutate) -> ChildAgentRecord | None:
    """Locked read-modify-write for the multi-writer child-agent record.

    ``mutate`` receives the current record (or ``None`` when missing) and
    returns the record to persist; returning ``None`` skips the write when no
    record exists, or persists the received record after in-place mutation.
    The persisted value passes through the same ``None``-field omission as
    ``saveChildAgent``.
    """
    agent_id = parseAgentId(agentId)
    path = childAgentPath(agent_id)
    async with withRecordTransaction("agent-record", agent_id):
        current = await _load_by_path(path)
        updated = mutate(current)
        if updated is None:
            updated = current
        if updated is None:
            return None
        await writeJsonFile(path, _omit_none_fields(updated))
        return updated

async def loadChildAgent(agentId: str) -> ChildAgentRecord | None:
    agent_id = parseAgentId(agentId)
    result = await readPersistedJsonRecord(
        family="agent",
        id=agent_id,
        filePath=childAgentPath(agent_id),
        validate=isChildAgentRecord,
    )
    return result.get("record")

async def listChildAgents() -> list[ChildAgentRecord]:
    directory = os.path.join(getStateRoot(), "agents")
    scan = await scanPersistedJsonRecords(
        family="agent",
        directory=directory,
        select=lambda entry: _json_candidate(directory, entry, tryParseAgentId),
        validate=isChildAgentRecord,
    )
    return sorted(scan["records"], key=lambda item: item.get("startedAt", ""), reverse=True)

async def writeTaskMeta(taskId: str, value: TaskMeta) -> None:
    task_id = parseTaskId(taskId)
    async with withRecordTransaction("task-lifecycle", task_id):
        await writeJsonFile(taskMetaPath(task_id), value)


async def mutateTaskMeta(taskId: str, mutate, fallback: TaskMeta | None = None) -> TaskMeta | None:
    """Locked read-modify-write for task meta (supervisor-owned record).

    ``mutate`` receives the current meta (or ``fallback`` when missing) and
    returns the meta to persist; returning ``None`` skips the write when
    nothing was loaded, or persists the received value after in-place
    mutation.
    """
    task_id = parseTaskId(taskId)
    path = taskMetaPath(task_id)
    async with withRecordTransaction("task-lifecycle", task_id):
        current = await readJsonFile(path, fallback)
        updated = mutate(current)
        if updated is None:
            updated = current
        if updated is None:
            return None
        await writeJsonFile(path, updated)
        return updated

async def readTaskMeta(taskId: str) -> TaskMeta | None:
    task_id = parseTaskId(taskId)
    result = await readPersistedJsonRecord(
        family="task_meta",
        id=task_id,
        filePath=taskMetaPath(task_id),
        validate=isTaskMetaRecord,
    )
    return result.get("record")

async def saveTaskRecord(record: TaskRecord) -> None:
    task_id = parseTaskId(record["id"])
    async with withRecordTransaction("task-lifecycle", task_id):
        await writeJsonFile(taskRecordPath(task_id), _omit_none_fields(record))

async def loadTaskRecord(taskId: str) -> TaskRecord | None:
    task_id = parseTaskId(taskId)
    result = await readPersistedJsonRecord(
        family="task_record",
        id=task_id,
        filePath=taskRecordPath(task_id),
        validate=isTaskRecord,
    )
    return result.get("record")

async def listTaskRecords() -> list[TaskRecord]:
    directory = os.path.join(getStateRoot(), "task-records")
    scan = await scanPersistedJsonRecords(
        family="task_record",
        directory=directory,
        select=lambda entry: _json_candidate(directory, entry, tryParseTaskId),
        validate=isTaskRecord,
    )
    return sorted(scan["records"], key=lambda item: item.get("updatedAt", ""), reverse=True)

async def listSessionTaskRecords(sessionId: str) -> list[TaskRecord]:
    return [task for task in await listTaskRecords() if task.get("sessionId") == sessionId]

async def saveMonitor(record: MonitorRecord) -> None:
    monitor_id = parseMonitorId(record["id"])
    async with withRecordTransaction("monitor-record", monitor_id):
        await writeJsonFile(monitorPath(monitor_id), record)

async def loadMonitor(monitorId: str) -> MonitorRecord | None:
    monitor_id = parseMonitorId(monitorId)
    result = await readPersistedJsonRecord(
        family="monitor",
        id=monitor_id,
        filePath=monitorPath(monitor_id),
        validate=isMonitorRecord,
    )
    return result.get("record")

async def listMonitors() -> list[MonitorRecord]:
    directory = os.path.join(getStateRoot(), "monitors")
    scan = await scanPersistedJsonRecords(
        family="monitor",
        directory=directory,
        select=lambda entry: _json_candidate(directory, entry, tryParseMonitorId),
        validate=isMonitorRecord,
    )
    return sorted(scan["records"], key=lambda item: item.get("updatedAt", ""), reverse=True)

async def listSessionMonitors(sessionId: str) -> list[MonitorRecord]:
    return [monitor for monitor in await listMonitors() if monitor.get("sessionId") == sessionId]

async def saveHealthCheckTask(record: TaskMeta) -> None:
    task_id = parseTaskId(record["taskId"])
    async with withRecordTransaction("task-lifecycle", task_id):
        await writeJsonFile(healthCheckMetaPath(task_id), record)


async def mutateHealthCheckTask(taskId: str, mutate) -> TaskMeta | None:
    """Locked read-modify-write for the health-check meta record."""
    task_id = parseTaskId(taskId)
    path = healthCheckMetaPath(task_id)
    async with withRecordTransaction("task-lifecycle", task_id):
        current = await readJsonFile(path, None)
        updated = mutate(current)
        if updated is None:
            updated = current
        if updated is None:
            return None
        await writeJsonFile(path, updated)
        return updated

async def loadHealthCheckTask(taskId: str) -> TaskMeta | None:
    task_id = parseTaskId(taskId)
    result = await readPersistedJsonRecord(
        family="health_check",
        id=task_id,
        filePath=healthCheckMetaPath(task_id),
        validate=isHealthCheckTaskRecord,
    )
    return result.get("record")

async def listHealthCheckTasks() -> list[TaskMeta]:
    directory = os.path.join(getStateRoot(), "tasks")
    def select(entry: os.DirEntry[str]) -> dict[str, str] | None:
        task_id = tryParseTaskId(entry.name)
        if task_id is None or not entry.is_dir(follow_symlinks=False):
            return None
        return {"id": task_id, "filePath": healthCheckMetaPath(task_id)}

    scan = await scanPersistedJsonRecords(
        family="health_check",
        directory=directory,
        select=select,
        validate=isHealthCheckTaskRecord,
    )
    return sorted(scan["records"], key=lambda item: item.get("updatedAt", ""), reverse=True)
