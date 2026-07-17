from __future__ import annotations

from typing import Any

from .._utils import now_iso
from ..storage import (
    mutateSessionRecord,
    saveHealthCheckTask,
    saveTaskRecord,
    taskOutputPath,
    writeTaskMeta,
    withRecordTransaction,
)


def buildAuthorizedHealthCheckRecord(
    *,
    request: dict[str, Any],
    taskId: str,
    status: str = "active",
    generation: int | None = None,
    revision: int = 1,
    suspendedReason: str | None = None,
) -> dict[str, Any]:
    now = now_iso()
    spec = request["spec"]
    record = {
        "taskId": taskId,
        "sessionId": request["sessionId"],
        "createdAt": now,
        "startedAt": now,
        "updatedAt": now,
        "status": status,
        "currentIntervalMs": spec["intervalMs"],
        "outputFile": taskOutputPath(taskId),
        "schemaVersion": 2,
        "revision": revision,
        "generation": generation if generation is not None else request.get("generation", 1),
        "specDigest": request["specDigest"],
        "executionPolicy": request["executionPolicy"],
        "lineageId": request.get("lineageId"),
        "supersedes": request.get("supersedes"),
        "suspendedReason": suspendedReason,
        **spec,
    }
    return _omit_none(record)


async def persistHealthCheckRecord(record: dict[str, Any]) -> None:
    async with withRecordTransaction("task-lifecycle", record["taskId"]):
        await saveHealthCheckTask(record)
        await writeTaskMeta(record["taskId"], {**record, "type": "health_check"})
        await saveTaskRecord(_health_task_record(record))


async def linkHealthChecksToSession(sessionId: str, taskIds: list[str]) -> None:
    def link(session: dict[str, Any]) -> dict[str, Any]:
        metadata = dict(session.get("metadata") or {})
        metadata["taskIds"] = list(dict.fromkeys([*(metadata.get("taskIds") or []), *taskIds]))
        return {**session, "metadata": metadata}

    updated = await mutateSessionRecord(sessionId, link)
    if updated is None:
        raise RuntimeError(f"Session not found: {sessionId}")


def _health_task_record(record: dict[str, Any]) -> dict[str, Any]:
    status = record.get("status")
    task_status = (
        "running"
        if status == "active"
        else "degraded"
        if status == "unhealthy"
        else "stopped"
        if status in {"stopped", "suspended_unverified", "suspended_for_rollback"}
        else "failed"
    )
    terminal = task_status in {"stopped", "failed"}
    return _omit_none(
        {
            "id": record["taskId"],
            "sessionId": record["sessionId"],
            "sourceId": record["taskId"],
            "kind": "health_check",
            "status": task_status,
            "title": record["title"],
            "summary": record.get("suspendedReason")
            or record.get("lastError")
            or ("Health check is running." if not terminal else f"Health check is {task_status}."),
            "createdAt": record["createdAt"],
            "updatedAt": record["updatedAt"],
            "startedAt": record.get("startedAt") or record["createdAt"],
            "completedAt": record["updatedAt"] if terminal else None,
            "foregroundable": False,
            "restorable": True,
            "cwd": record.get("cwd"),
            "command": record.get("command") or record.get("url"),
            "outputFile": record.get("outputFile"),
            "foregroundMode": "background",
            "taskFamily": "health_check",
            "detailStatus": status,
            "retrieveOnly": True,
            "owner": "daemon",
            "metadata": {
                "mode": record.get("mode"),
                "generation": record.get("generation"),
                "revision": record.get("revision"),
                "spec_digest": record.get("specDigest"),
                "supersedes": record.get("supersedes"),
            },
        }
    )


def _omit_none(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _omit_none(item) for key, item in value.items() if item is not None}
    if isinstance(value, list):
        return [_omit_none(item) for item in value]
    return value
