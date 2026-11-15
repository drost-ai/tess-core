from __future__ import annotations
from .._utils import drop_none_deep

from ..contracts.records import (
    ChildAgentRecord,
    MonitorRecord,
    TaskMeta,
    TaskRecord,
)
from ..runtime.identifiers import (
    RuntimeIdentifierError,
    parse_agent_id,
    parse_monitor_id,
    parse_task_id,
    try_parse_agent_id,
    try_parse_monitor_id,
    try_parse_task_id,
)
from .persisted_record_scanner import read_persisted_json_record, scan_persisted_json_records
from .persisted_record_validation import (
    is_child_agent_record,
    is_health_check_task_record,
    is_monitor_record,
    is_task_meta_record,
    is_task_record,
)

import os
from typing import Any

from .json_io import read_json_file, with_record_transaction, write_json_file
from .paths import get_state_root

# Every record mutation acquires its family's record transaction, keyed by
# record id. Task record, task meta, and health-check meta are one lifecycle
# unit sharing the "task-lifecycle" domain: a holder of that transaction
# (terminal_store) excludes ALL writers of all three files, not only other
# transaction holders. The transaction is reentrant within a task context, so
# these writers compose with an enclosing transaction instead of deadlocking.

def task_dir(taskId: str) -> str:
    return os.path.join(get_state_root(), "tasks", parse_task_id(taskId))

def task_record_path(taskId: str) -> str:
    return os.path.join(get_state_root(), "task-records", f"{parse_task_id(taskId)}.json")

def task_meta_path(taskId: str) -> str:
    return os.path.join(task_dir(taskId), "meta.json")

def task_output_path(taskId: str) -> str:
    return os.path.join(task_dir(taskId), "output.log")

def health_check_meta_path(taskId: str) -> str:
    return os.path.join(task_dir(taskId), "health-check.json")

def monitor_path(monitorId: str) -> str:
    return os.path.join(get_state_root(), "monitors", f"{parse_monitor_id(monitorId)}.json")

def child_agent_path(agentId: str) -> str:
    return os.path.join(get_state_root(), "agents", f"{parse_agent_id(agentId)}.json")

async def _load_by_path(path: str) -> dict[str, Any] | None:
    return await read_json_file(path, None)

def omit_none_fields(value: Any) -> Any:
    return drop_none_deep(value)


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

def _json_candidate(directory: str, entry: os.DirEntry[str], parse_id) -> dict[str, str] | None:
    if not entry.name.endswith(".json"):
        return None
    record_id = parse_id(entry.name[:-5])
    if record_id is None:
        return None
    return {"id": record_id, "filePath": os.path.join(directory, entry.name)}

async def save_child_agent(record: ChildAgentRecord) -> None:
    """Save a sub-agent record, merged with what is already stored."""
    agent_id = parse_agent_id(record["agentId"])
    path = child_agent_path(agent_id)
    async with with_record_transaction("agent-record", agent_id):
        current = await _load_by_path(path)
        await write_json_file(path, omit_none_fields(_merge_child_agent_for_full_save(current, record)))


async def mutate_child_agent(agentId: str, mutate) -> ChildAgentRecord | None:
    """Locked read-modify-write for the multi-writer child-agent record.

    ``mutate`` receives the current record (or ``None`` when missing) and
    returns the record to persist; returning ``None`` skips the write when no
    record exists, or persists the received record after in-place mutation.
    The persisted value passes through the same ``None``-field omission as
    ``save_child_agent``.
    """
    agent_id = parse_agent_id(agentId)
    path = child_agent_path(agent_id)
    async with with_record_transaction("agent-record", agent_id):
        current = await _load_by_path(path)
        updated = mutate(current)
        if updated is None:
            updated = current
        if updated is None:
            return None
        await write_json_file(path, omit_none_fields(updated))
        return updated

async def load_child_agent(agentId: str) -> ChildAgentRecord | None:
    agent_id = parse_agent_id(agentId)
    result = await read_persisted_json_record(
        family="agent",
        id=agent_id,
        filePath=child_agent_path(agent_id),
        validate=is_child_agent_record,
    )
    return result.get("record")

async def list_child_agents() -> list[ChildAgentRecord]:
    """Every valid sub-agent record, most recently started first."""
    directory = os.path.join(get_state_root(), "agents")
    scan = await scan_persisted_json_records(
        family="agent",
        directory=directory,
        select=lambda entry: _json_candidate(directory, entry, try_parse_agent_id),
        validate=is_child_agent_record,
    )
    return sorted(scan["records"], key=lambda item: item.get("startedAt", ""), reverse=True)

async def write_task_meta(taskId: str, value: TaskMeta) -> None:
    task_id = parse_task_id(taskId)
    async with with_record_transaction("task-lifecycle", task_id):
        await write_json_file(task_meta_path(task_id), value)


async def mutate_task_meta(taskId: str, mutate, fallback: TaskMeta | None = None) -> TaskMeta | None:
    """Locked read-modify-write for task meta (supervisor-owned record).

    ``mutate`` receives the current meta (or ``fallback`` when missing) and
    returns the meta to persist; returning ``None`` skips the write when
    nothing was loaded, or persists the received value after in-place
    mutation.
    """
    task_id = parse_task_id(taskId)
    path = task_meta_path(task_id)
    async with with_record_transaction("task-lifecycle", task_id):
        current = await read_json_file(path, fallback)
        updated = mutate(current)
        if updated is None:
            updated = current
        if updated is None:
            return None
        await write_json_file(path, updated)
        return updated

async def read_task_meta(taskId: str) -> TaskMeta | None:
    task_id = parse_task_id(taskId)
    result = await read_persisted_json_record(
        family="task_meta",
        id=task_id,
        filePath=task_meta_path(task_id),
        validate=is_task_meta_record,
    )
    return result.get("record")

async def save_task_record(record: TaskRecord) -> None:
    task_id = parse_task_id(record["id"])
    async with with_record_transaction("task-lifecycle", task_id):
        await write_json_file(task_record_path(task_id), omit_none_fields(record))

async def load_task_record(taskId: str) -> TaskRecord | None:
    task_id = parse_task_id(taskId)
    result = await read_persisted_json_record(
        family="task_record",
        id=task_id,
        filePath=task_record_path(task_id),
        validate=is_task_record,
    )
    return result.get("record")

async def list_task_records() -> list[TaskRecord]:
    directory = os.path.join(get_state_root(), "task-records")
    scan = await scan_persisted_json_records(
        family="task_record",
        directory=directory,
        select=lambda entry: _json_candidate(directory, entry, try_parse_task_id),
        validate=is_task_record,
    )
    return sorted(scan["records"], key=lambda item: item.get("updatedAt", ""), reverse=True)

async def list_session_task_records(sessionId: str) -> list[TaskRecord]:
    return [task for task in await list_task_records() if task.get("sessionId") == sessionId]

async def save_monitor(record: MonitorRecord) -> None:
    """Save a monitor record."""
    monitor_id = parse_monitor_id(record["id"])
    async with with_record_transaction("monitor-record", monitor_id):
        await write_json_file(monitor_path(monitor_id), record)

async def load_monitor(monitorId: str) -> MonitorRecord | None:
    monitor_id = parse_monitor_id(monitorId)
    result = await read_persisted_json_record(
        family="monitor",
        id=monitor_id,
        filePath=monitor_path(monitor_id),
        validate=is_monitor_record,
    )
    return result.get("record")

async def list_monitors() -> list[MonitorRecord]:
    directory = os.path.join(get_state_root(), "monitors")
    scan = await scan_persisted_json_records(
        family="monitor",
        directory=directory,
        select=lambda entry: _json_candidate(directory, entry, try_parse_monitor_id),
        validate=is_monitor_record,
    )
    return sorted(scan["records"], key=lambda item: item.get("updatedAt", ""), reverse=True)

async def list_session_monitors(sessionId: str) -> list[MonitorRecord]:
    return [monitor for monitor in await list_monitors() if monitor.get("sessionId") == sessionId]

async def save_health_check_task(record: TaskMeta) -> None:
    task_id = parse_task_id(record["taskId"])
    async with with_record_transaction("task-lifecycle", task_id):
        await write_json_file(health_check_meta_path(task_id), record)


async def mutate_health_check_task(taskId: str, mutate) -> TaskMeta | None:
    """Locked read-modify-write for the health-check meta record."""
    task_id = parse_task_id(taskId)
    path = health_check_meta_path(task_id)
    async with with_record_transaction("task-lifecycle", task_id):
        current = await read_json_file(path, None)
        updated = mutate(current)
        if updated is None:
            updated = current
        if updated is None:
            return None
        await write_json_file(path, updated)
        return updated

async def load_health_check_task(taskId: str) -> TaskMeta | None:
    task_id = parse_task_id(taskId)
    result = await read_persisted_json_record(
        family="health_check",
        id=task_id,
        filePath=health_check_meta_path(task_id),
        validate=is_health_check_task_record,
    )
    return result.get("record")

async def list_health_check_tasks() -> list[TaskMeta]:
    directory = os.path.join(get_state_root(), "tasks")
    def select(entry: os.DirEntry[str]) -> dict[str, str] | None:
        task_id = try_parse_task_id(entry.name)
        if task_id is None or not entry.is_dir(follow_symlinks=False):
            return None
        return {"id": task_id, "filePath": health_check_meta_path(task_id)}

    scan = await scan_persisted_json_records(
        family="health_check",
        directory=directory,
        select=select,
        validate=is_health_check_task_record,
    )
    return sorted(scan["records"], key=lambda item: item.get("updatedAt", ""), reverse=True)
