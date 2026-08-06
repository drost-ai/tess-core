from __future__ import annotations

from ..contracts.records import (
    ChildAgentRecord,
    MonitorRecord,
    PlanApprovalRecord,
    TaskMeta,
    TaskRecord,
)
from ..runtime.identifiers import (
    RuntimeIdentifierError,
    parseAgentId,
    parseApprovalRequestId,
    parseMonitorId,
    parseTaskId,
    tryParseAgentId,
    tryParseApprovalRequestId,
    tryParseMonitorId,
    tryParseTaskId,
)
from ..session.persisted_record_scanner import readPersistedJsonRecord, scanPersistedJsonRecords
from ..session.persisted_record_validation import (
    isChildAgentRecord,
    isHealthCheckTaskRecord,
    isMonitorRecord,
    isPlanApprovalRecord,
    isTaskMetaRecord,
    isTaskRecord,
)

import os
import random
import uuid
from typing import Any

from .json_io import fileLock, readJsonFile, writeJsonFile
from .paths import getPlansDir, getStateRoot

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
    "awaiting_plan_approval": 2,
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
    path = childAgentPath(record["agentId"])
    async with fileLock(path):
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
    path = childAgentPath(agentId)
    async with fileLock(path):
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

def planApprovalPath(requestId: str) -> str:
    return os.path.join(
        getStateRoot(),
        "plan-approvals",
        f"{parseApprovalRequestId(requestId)}.json",
    )

async def savePlanApproval(record: PlanApprovalRecord) -> None:
    await _save_by_path(planApprovalPath(record["requestId"]), record)

async def loadPlanApproval(requestId: str) -> PlanApprovalRecord | None:
    request_id = parseApprovalRequestId(requestId)
    result = await readPersistedJsonRecord(
        family="plan_approval",
        id=request_id,
        filePath=planApprovalPath(request_id),
        validate=isPlanApprovalRecord,
    )
    return result.get("record")

async def listPlanApprovals() -> list[PlanApprovalRecord]:
    directory = os.path.join(getStateRoot(), "plan-approvals")
    scan = await scanPersistedJsonRecords(
        family="plan_approval",
        directory=directory,
        select=lambda entry: _json_candidate(directory, entry, tryParseApprovalRequestId),
        validate=isPlanApprovalRecord,
    )
    return sorted(scan["records"], key=lambda item: item.get("createdAt", ""), reverse=True)

PLAN_ADJECTIVES = ["amber", "brisk", "clear", "copper", "ember", "gentle", "midnight", "quiet", "rapid", "silver", "solid", "steady", "sunlit", "tidy", "velvet"]
PLAN_NOUNS = ["anchor", "beacon", "bridge", "compass", "falcon", "harbor", "lantern", "meadow", "signal", "sparrow", "summit", "thunder", "trail", "valley", "willow"]
async def writePlanFile(args: dict[str, Any] | None = None, **kwargs: Any) -> str:
    args = {**(args or {}), **kwargs}
    file_path = args.get("filePath")
    if not file_path:
        for _ in range(12):
            candidate = os.path.join(getPlansDir(), f"{random.choice(PLAN_ADJECTIVES)}-{random.choice(PLAN_NOUNS)}.md")
            if not os.path.exists(candidate):
                file_path = candidate
                break
        file_path = file_path or os.path.join(getPlansDir(), f"{uuid.uuid4()}.md")
    os.makedirs(os.path.dirname(file_path), exist_ok=True)
    lines = [
        "# Assistant Plan",
        "",
        f"- request id: `{args['requestId']}`",
        f"- session id: `{args['sessionId']}`",
        f"- status: `{args['status']}`",
        f"- cwd: `{args['cwd']}`",
        f"- created at: `{args['createdAt']}`",
    ]
    if args.get("resolvedAt"):
        lines.append(f"- resolved at: `{args['resolvedAt']}`")
    if args.get("reason"):
        lines.append(f"- reason: {args['reason']}")
    lines.extend(["", "## Plan", "", (args.get("plan") or "").strip() or "_No plan text captured._"])
    if args.get("allowedPrompts"):
        lines.extend(["", "## Allowed Prompts", ""])
        for prompt in args["allowedPrompts"]:
            lines.append(f"- `{prompt.get('tool')}`: {prompt.get('prompt')}")
    lines.extend(["", "## Retention", "", "This plan is retained indefinitely for session history and auditability.", ""])
    with open(file_path, "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines))
    return file_path
async def writeTaskMeta(taskId: str, value: TaskMeta) -> None:
    path = taskMetaPath(taskId)
    async with fileLock(path):
        await writeJsonFile(path, value)


async def mutateTaskMeta(taskId: str, mutate, fallback: TaskMeta | None = None) -> TaskMeta | None:
    """Locked read-modify-write for task meta (supervisor-owned record).

    ``mutate`` receives the current meta (or ``fallback`` when missing) and
    returns the meta to persist; returning ``None`` skips the write when
    nothing was loaded, or persists the received value after in-place
    mutation.
    """
    path = taskMetaPath(taskId)
    async with fileLock(path):
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
    path = taskRecordPath(record["id"])
    async with fileLock(path):
        await writeJsonFile(path, _omit_none_fields(record))

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
    path = monitorPath(record["id"])
    async with fileLock(path):
        await writeJsonFile(path, record)

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
    path = healthCheckMetaPath(record["taskId"])
    async with fileLock(path):
        await writeJsonFile(path, record)


async def mutateHealthCheckTask(taskId: str, mutate) -> TaskMeta | None:
    """Locked read-modify-write for the health-check meta record."""
    path = healthCheckMetaPath(taskId)
    async with fileLock(path):
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
