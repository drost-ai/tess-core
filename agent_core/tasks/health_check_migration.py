from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
from typing import Any

from .._utils import now_iso
from ..storage import (
    healthCheckMetaPath,
    loadHealthCheckTask,
    loadTaskRecord,
    readJsonFile,
    saveHealthCheckTask,
    saveTaskRecord,
    writeJsonFile,
    writeTaskMeta,
    withRecordTransaction,
)
from .health_check_policy import (
    assertHealthCheckExecutionPolicy,
    healthCheckSpecDigest,
    normalizeHealthCheckSpec,
)


MAX_LEGACY_HEALTH_RECORD_BYTES = 1024 * 1024


def healthCheckRevalidationJournalPath(taskId: str) -> str:
    return os.path.join(os.path.dirname(healthCheckMetaPath(taskId)), "health-check-revalidation.json")


async def loadHealthCheckRevalidationJournal(taskId: str) -> dict[str, Any] | None:
    return await readJsonFile(healthCheckRevalidationJournalPath(taskId), None)


async def saveHealthCheckRevalidationJournal(journal: dict[str, Any]) -> None:
    await writeJsonFile(
        healthCheckRevalidationJournalPath(journal["suspendedTaskId"]),
        _omit_none(journal),
    )


async def migrateLegacyHealthCheck(taskId: str) -> dict[str, Any] | None:
    async with withRecordTransaction("task-lifecycle", taskId):
        record = await loadHealthCheckTask(taskId)
        if not record:
            return None
        if record.get("status") in {"stopped", "failed", "suspended_unverified", "suspended_for_rollback"}:
            return record
        if await _is_runnable_v2(record):
            return record
        target = Path(healthCheckMetaPath(taskId))
        stats = target.stat()
        if not target.is_file() or stats.st_size > MAX_LEGACY_HEALTH_RECORD_BYTES:
            raise RuntimeError("Legacy health-check record exceeds its migration byte limit.")
        raw = target.read_bytes()
        source_digest = hashlib.sha256(raw).hexdigest()
        normalized = None
        invalid_reason = None
        try:
            normalized = await normalizeHealthCheckSpec(
                record,
                str(record.get("cwd") or "."),
                {"legacy": True},
            )
        except Exception as exc:
            invalid_reason = _bounded_reason(exc)
        spec_digest = (
            healthCheckSpecDigest(normalized)
            if normalized
            else hashlib.sha256(
                json.dumps(
                    {
                        "mode": record.get("mode"),
                        "command": record.get("command"),
                        "url": record.get("url"),
                        "title": record.get("title"),
                    },
                    separators=(",", ":"),
                ).encode()
            ).hexdigest()
        )
        revision = max(1, int(record.get("suspensionRevision") or record.get("revision") or 0) + 1)
        policy_valid = bool(normalized and _policy_valid(normalized, record))
        missing = _missing_authority(record, invalid_reason, policy_valid, spec_digest)
        journal = {
            "schemaVersion": 1,
            "suspendedTaskId": taskId,
            "sessionId": record["sessionId"],
            "suspensionRevision": revision,
            "specDigest": spec_digest,
            "sourceDigest": source_digest,
            "missingAuthority": missing,
            "rawLegacyRecord": record,
            "rawLegacyBytesBase64": base64.b64encode(raw).decode("ascii"),
            "normalizedSpec": normalized,
            "invalidSpecReason": invalid_reason,
            "createdAt": now_iso(),
        }
        await saveHealthCheckRevalidationJournal(journal)
        now = now_iso()
        suspended = _omit_none(
            {
                **record,
                **(normalized or {}),
                "status": "suspended_unverified",
                "schemaVersion": 2,
                "revision": revision,
                "generation": 0,
                "specDigest": spec_digest,
                "executionPolicy": None,
                "activeProcessIdentity": None,
                "suspendedReason": (
                    f"Legacy health check is inert and its spec is invalid: {invalid_reason}"
                    if invalid_reason
                    else "Legacy health check is inert until its execution authority is explicitly revalidated."
                ),
                "suspensionRevision": revision,
                "updatedAt": now,
            }
        )
        await saveHealthCheckTask(suspended)
        await writeTaskMeta(taskId, {**suspended, "type": "health_check"})
        task_record = await loadTaskRecord(taskId)
        if task_record:
            await saveTaskRecord(
                {
                    **task_record,
                    "status": "stopped",
                    "summary": suspended["suspendedReason"],
                    "detailStatus": "suspended_unverified",
                    "updatedAt": now,
                    "completedAt": now,
                }
            )
        return suspended


async def _is_runnable_v2(record: dict[str, Any]) -> bool:
    if (
        record.get("schemaVersion") != 2
        or isinstance(record.get("generation"), bool)
        or not isinstance(record.get("generation"), int)
        or record["generation"] < 1
        or not isinstance(record.get("specDigest"), str)
        or len(record["specDigest"]) != 64
        or not isinstance(record.get("executionPolicy"), dict)
    ):
        return False
    try:
        spec = await normalizeHealthCheckSpec(record, str(record.get("cwd") or "."))
        return healthCheckSpecDigest(spec) == record["specDigest"] and _policy_valid(spec, record)
    except Exception:
        return False


def _policy_valid(spec: dict[str, Any], record: dict[str, Any]) -> bool:
    try:
        assertHealthCheckExecutionPolicy(spec, record["executionPolicy"])
        return True
    except Exception:
        return False


def _missing_authority(
    record: dict[str, Any],
    invalid_reason: str | None,
    policy_valid: bool,
    normalized_digest: str,
) -> list[str]:
    values = [
        None if record.get("schemaVersion") == 2 else "schemaVersion",
        None if isinstance(record.get("generation"), int) and record["generation"] >= 1 else "generation",
        None if policy_valid else "executionPolicy",
        None if record.get("specDigest") == normalized_digest else "specDigest",
        None if record.get("cwd") else "cwd",
        "ownedProcessIdentity",
        "validSpec" if invalid_reason else None,
    ]
    return [value for value in values if value]


def _bounded_reason(error: Any) -> str:
    value = str(error)
    return value if len(value) <= 500 else f"{value[:497]}..."


def _omit_none(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _omit_none(item) for key, item in value.items() if item is not None}
    if isinstance(value, list):
        return [_omit_none(item) for item in value]
    return value


