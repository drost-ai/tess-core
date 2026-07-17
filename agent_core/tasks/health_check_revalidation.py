from __future__ import annotations

import asyncio
import hashlib
import json
import re
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from .._utils import maybe_await, now_iso
from ..hooks import fireToolHooks
from ..storage import getStateRoot, loadHealthCheckTask, loadSession, withRecordTransaction
from ..tools.permissions import _get_effective_permission_mode
from .health_check_migration import (
    loadHealthCheckRevalidationJournal,
    migrateLegacyHealthCheck,
    saveHealthCheckRevalidationJournal,
)
from .health_check_policy import authorizeHealthCheckSpec
from .health_check_record import (
    buildAuthorizedHealthCheckRecord,
    linkHealthChecksToSession,
    persistHealthCheckRecord,
)


GRANT_TTL_SECONDS = 5 * 60
MAX_LIVE_GRANTS = 128
_LIVE_GRANTS: dict[str, dict[str, Any]] = {}
_GRANT_CLOCK: Callable[[], datetime] = lambda: datetime.now(timezone.utc)
_REVALIDATION_TEST_HOOK: Callable[[str], Any] | None = None


async def inspectHealthCheckRevalidation(taskId: str) -> dict[str, Any]:
    record = await migrateLegacyHealthCheck(taskId)
    if not record:
        raise RuntimeError(f"Health check not found: {taskId}")
    journal = await _require_suspension_journal(taskId, record)
    return _omit_none(
        {
            "taskId": taskId,
            "sessionId": journal["sessionId"],
            "suspensionRevision": journal["suspensionRevision"],
            "specDigest": journal["specDigest"],
            "missingAuthority": list(journal["missingAuthority"]),
            "supersededBy": journal.get("supersededBy"),
        }
    )


async def authorizeHealthCheckRevalidation(
    context: dict[str, Any], taskId: str
) -> dict[str, Any]:
    record = await migrateLegacyHealthCheck(taskId)
    if not record:
        raise RuntimeError(f"Health check not found: {taskId}")
    journal = await _require_suspension_journal(taskId, record)
    if journal.get("replacement") or journal.get("supersededBy"):
        raise RuntimeError(f"Health check {taskId} already has a revalidated replacement.")
    spec = journal.get("normalizedSpec")
    if not isinstance(spec, dict) or journal.get("invalidSpecReason"):
        raise RuntimeError(
            f"Health check {taskId} cannot be revalidated: "
            f"{journal.get('invalidSpecReason') or 'normalized spec unavailable'}"
        )
    session = context.get("session") or {}
    if session.get("id") != journal["sessionId"]:
        raise RuntimeError(
            "Health-check revalidation approval belongs to a different session."
        )
    persisted_session = await loadSession(journal["sessionId"])
    if not persisted_session or persisted_session.get("id") != session.get("id"):
        raise RuntimeError("Health-check revalidation session is unavailable.")
    permission_mode = await _get_effective_permission_mode(context)
    hook = await fireToolHooks(
        {
            "event": "PreToolUse",
            "toolName": "TaskHealthCheckStart",
            "toolInput": {
                "title": spec["title"],
                "mode": spec["mode"],
                "command": spec.get("command"),
                "url": spec.get("url"),
            },
            "fire": {
                "cwd": spec["cwd"],
                "sessionId": journal["sessionId"],
                "config": context.get("config") or {},
                "permissionMode": permission_mode,
                "authority": (session.get("metadata") or {}).get(
                    "effectiveAgentAuthority"
                ),
            },
            "emit": context.get("outputJson") or (lambda *_: None),
        }
    )
    if hook.get("decision") == "deny":
        suffix = f": {hook['reason']}" if hook.get("reason") else "."
        raise RuntimeError(
            "Health-check revalidation was blocked by a PreToolUse hook" + suffix
        )
    execution_policy = await authorizeHealthCheckSpec(context, spec)
    nonce = secrets.token_urlsafe(32)
    expires = _GRANT_CLOCK() + timedelta(seconds=GRANT_TTL_SECONDS)
    grant = {
        "schemaVersion": 1,
        "nonce": nonce,
        "sessionId": journal["sessionId"],
        "suspendedTaskId": taskId,
        "suspensionRevision": journal["suspensionRevision"],
        "specDigest": journal["specDigest"],
        "mode": spec["mode"],
        "cwd": spec["cwd"],
        "authorityFingerprint": execution_policy["authorityFingerprint"],
        "expiresAt": expires.isoformat().replace("+00:00", "Z"),
    }
    _prune_live_grants()
    if len(_LIVE_GRANTS) >= MAX_LIVE_GRANTS:
        _LIVE_GRANTS.pop(next(iter(_LIVE_GRANTS)))
    _LIVE_GRANTS[nonce] = {
        "grant": _clone(grant),
        "request": {
            "sessionId": journal["sessionId"],
            "spec": _clone(spec),
            "specDigest": journal["specDigest"],
            "executionPolicy": execution_policy,
            "supersedes": taskId,
            "lineageId": taskId,
            "generation": 1,
        },
        "runtimeHome": getStateRoot(),
    }
    return grant


async def revalidateHealthCheck(
    args: dict[str, Any] | None = None, **kwargs: Any
) -> dict[str, Any]:
    values = {**(args or {}), **kwargs}
    task_id = values.get("taskId")
    if not isinstance(task_id, str):
        raise ValueError("taskId must be a string")
    await migrateLegacyHealthCheck(task_id)
    async with withRecordTransaction("health-revalidation", task_id):
        source = await loadHealthCheckTask(task_id)
        if not source:
            raise RuntimeError(f"Health check not found: {task_id}")
        journal = await _require_suspension_journal(task_id, source)
        _validate_expected_state(values, journal)
        grant = values.get("approvalGrant")
        if not isinstance(grant, dict):
            raise RuntimeError("Health-check revalidation approval grant is malformed.")
        nonce_digest = _digest_nonce(str(grant.get("nonce") or ""))
        if journal.get("replacement"):
            _validate_prepared_grant(grant, journal, nonce_digest)
            await _resume_revalidation(journal)
            return _replacement_result(journal)
        live = _require_live_grant(grant, journal)
        replacement_task_id = str(uuid.uuid4())
        journal.update(
            {
                "supersedes": task_id,
                "supersededBy": replacement_task_id,
                "grantNonceDigest": nonce_digest,
                "replacement": {
                    "taskId": replacement_task_id,
                    "phase": "prepared",
                    "executionPolicy": _clone(live["request"]["executionPolicy"]),
                    "createdAt": now_iso(),
                },
            }
        )
        await saveHealthCheckRevalidationJournal(journal)
        _LIVE_GRANTS.pop(grant["nonce"], None)
        await _run_test_hook("prepared")
        await _resume_revalidation(journal)
        return _replacement_result(journal)


async def _resume_revalidation(journal: dict[str, Any]) -> None:
    replacement = journal.get("replacement")
    spec = journal.get("normalizedSpec")
    if not isinstance(replacement, dict) or not isinstance(spec, dict) or not journal.get("supersededBy"):
        raise RuntimeError("Health-check revalidation journal is incomplete.")
    request = {
        "sessionId": journal["sessionId"],
        "spec": spec,
        "specDigest": journal["specDigest"],
        "executionPolicy": replacement["executionPolicy"],
        "supersedes": journal["suspendedTaskId"],
        "lineageId": journal["suspendedTaskId"],
        "generation": 1,
    }
    if replacement["phase"] == "prepared":
        existing = await loadHealthCheckTask(replacement["taskId"])
        if existing:
            _validate_replacement(existing, journal, False)
        else:
            await persistHealthCheckRecord(
                buildAuthorizedHealthCheckRecord(
                    request=request,
                    taskId=replacement["taskId"],
                    status="suspended_unverified",
                    generation=0,
                    suspendedReason=(
                        "Approved revalidation is awaiting its durable session link."
                    ),
                )
            )
        replacement["phase"] = "replacement_written"
        await saveHealthCheckRevalidationJournal(journal)
        await _run_test_hook("replacement_written")
    if replacement["phase"] == "replacement_written":
        await linkHealthChecksToSession(
            journal["sessionId"],
            [journal["suspendedTaskId"], replacement["taskId"]],
        )
        replacement["phase"] = "session_linked"
        await saveHealthCheckRevalidationJournal(journal)
        await _run_test_hook("session_linked")
    if replacement["phase"] == "session_linked":
        record = await loadHealthCheckTask(replacement["taskId"])
        if not record:
            raise RuntimeError(
                "Health-check replacement disappeared before activation."
            )
        _validate_replacement(record, journal, True)
        if record.get("generation") == 0 and record.get("status") == "suspended_unverified":
            activated = {
                **record,
                "status": "active",
                "generation": 1,
                "revision": int(record.get("revision") or 0) + 1,
                "updatedAt": now_iso(),
            }
            activated.pop("suspendedReason", None)
            await persistHealthCheckRecord(activated)
            await _run_test_hook("activated")
        replacement["phase"] = "committed"
        await saveHealthCheckRevalidationJournal(journal)
        await _run_test_hook("committed")
    if replacement["phase"] == "committed":
        record = await loadHealthCheckTask(replacement["taskId"])
        if not record:
            raise RuntimeError(
                "Committed health-check replacement is unavailable."
            )
        _validate_replacement(record, journal, True)


async def _require_suspension_journal(
    task_id: str, record: dict[str, Any]
) -> dict[str, Any]:
    if record.get("status") != "suspended_unverified" or record.get("generation") != 0:
        raise RuntimeError(
            f"Health check {task_id} is not awaiting legacy revalidation."
        )
    journal = await loadHealthCheckRevalidationJournal(task_id)
    if (
        not journal
        or journal.get("suspendedTaskId") != task_id
        or journal.get("sessionId") != record.get("sessionId")
        or journal.get("suspensionRevision") != record.get("suspensionRevision")
        or journal.get("specDigest") != record.get("specDigest")
    ):
        raise RuntimeError(
            "Health-check suspension journal is missing or inconsistent."
        )
    return journal


def _require_live_grant(
    grant: dict[str, Any], journal: dict[str, Any]
) -> dict[str, Any]:
    _validate_grant_shape(grant)
    if _parse_time(grant["expiresAt"]) <= _GRANT_CLOCK():
        _LIVE_GRANTS.pop(grant["nonce"], None)
        raise RuntimeError("Health-check revalidation approval grant expired.")
    live = _LIVE_GRANTS.get(grant["nonce"])
    if not live or live["grant"] != grant:
        raise RuntimeError(
            "Health-check revalidation approval grant is invalid or already consumed."
        )
    if live["runtimeHome"] != getStateRoot():
        raise RuntimeError(
            "Health-check revalidation approval grant belongs to a different runtime home."
        )
    _validate_grant_bindings(grant, journal)
    if (
        live["request"]["executionPolicy"]["authorityFingerprint"]
        != grant["authorityFingerprint"]
    ):
        raise RuntimeError(
            "Health-check revalidation authority fingerprint changed."
        )
    return live


def _validate_prepared_grant(
    grant: dict[str, Any], journal: dict[str, Any], nonce_digest: str
) -> None:
    _validate_grant_shape(grant)
    _validate_grant_bindings(grant, journal)
    if journal.get("grantNonceDigest") != nonce_digest:
        raise RuntimeError(
            "A different approval grant already owns this revalidation transaction."
        )
    policy = (journal.get("replacement") or {}).get("executionPolicy") or {}
    if policy.get("authorityFingerprint") != grant["authorityFingerprint"]:
        raise RuntimeError(
            "Health-check revalidation authority fingerprint changed."
        )


def _validate_expected_state(values: dict[str, Any], journal: dict[str, Any]) -> None:
    if (
        journal["suspensionRevision"] != values.get("expectedSuspensionRevision")
        or journal["specDigest"] != values.get("expectedSpecDigest")
    ):
        raise RuntimeError(
            "Health-check suspension revision or spec digest changed before revalidation."
        )


def _validate_grant_bindings(
    grant: dict[str, Any], journal: dict[str, Any]
) -> None:
    spec = journal.get("normalizedSpec") or {}
    expected = (
        journal["sessionId"],
        journal["suspendedTaskId"],
        journal["suspensionRevision"],
        journal["specDigest"],
        spec.get("mode"),
        spec.get("cwd"),
    )
    actual = (
        grant.get("sessionId"),
        grant.get("suspendedTaskId"),
        grant.get("suspensionRevision"),
        grant.get("specDigest"),
        grant.get("mode"),
        grant.get("cwd"),
    )
    if actual != expected:
        raise RuntimeError(
            "Health-check revalidation approval grant does not match the suspended record."
        )


def _validate_replacement(
    record: dict[str, Any], journal: dict[str, Any], allow_activated: bool
) -> None:
    replacement = journal["replacement"]
    generation = record.get("generation")
    generation_okay = generation == 0 or (
        allow_activated and isinstance(generation, int) and generation >= 1
    )
    policy = record.get("executionPolicy") or {}
    if not (
        record.get("taskId") == replacement["taskId"]
        and record.get("sessionId") == journal["sessionId"]
        and record.get("schemaVersion") == 2
        and record.get("specDigest") == journal["specDigest"]
        and record.get("supersedes") == journal["suspendedTaskId"]
        and record.get("lineageId") == journal["suspendedTaskId"]
        and policy.get("authorityFingerprint")
        == replacement["executionPolicy"].get("authorityFingerprint")
        and generation_okay
    ):
        raise RuntimeError(
            "Health-check replacement record does not match its revalidation journal."
        )


def _validate_grant_shape(grant: dict[str, Any]) -> None:
    valid = (
        grant.get("schemaVersion") == 1
        and isinstance(grant.get("nonce"), str)
        and bool(re.fullmatch(r"[A-Za-z0-9_-]{40,}", grant["nonce"]))
        and isinstance(grant.get("specDigest"), str)
        and bool(re.fullmatch(r"[a-f0-9]{64}", grant["specDigest"]))
        and isinstance(grant.get("authorityFingerprint"), str)
        and bool(re.fullmatch(r"[a-f0-9]{64}", grant["authorityFingerprint"]))
        and isinstance(grant.get("suspensionRevision"), int)
        and not isinstance(grant.get("suspensionRevision"), bool)
        and grant.get("mode") in {"command", "http"}
        and _parse_time_safe(grant.get("expiresAt")) is not None
    )
    if not valid:
        raise RuntimeError(
            "Health-check revalidation approval grant is malformed."
        )


def _replacement_result(journal: dict[str, Any]) -> dict[str, Any]:
    replacement = journal.get("replacement")
    if not replacement:
        raise RuntimeError(
            "Health-check revalidation replacement is unavailable."
        )
    return {
        "suspendedTaskId": journal["suspendedTaskId"],
        "replacementTaskId": replacement["taskId"],
        "lineageId": journal["suspendedTaskId"],
        "generation": 1,
    }


def _prune_live_grants() -> None:
    now = _GRANT_CLOCK()
    for nonce, value in list(_LIVE_GRANTS.items()):
        if _parse_time(value["grant"]["expiresAt"]) <= now:
            _LIVE_GRANTS.pop(nonce, None)


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _parse_time_safe(value: Any) -> datetime | None:
    try:
        return _parse_time(value) if isinstance(value, str) else None
    except ValueError:
        return None


def _digest_nonce(nonce: str) -> str:
    return hashlib.sha256(nonce.encode()).hexdigest()


def _clone(value: Any) -> Any:
    return json.loads(json.dumps(value))


def _omit_none(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _omit_none(item) for key, item in value.items() if item is not None}
    if isinstance(value, list):
        return [_omit_none(item) for item in value]
    return value


async def _run_test_hook(phase: str) -> None:
    if _REVALIDATION_TEST_HOOK:
        await maybe_await(_REVALIDATION_TEST_HOOK(phase))


def __setHealthCheckGrantClockForTests(
    clock: Callable[[], datetime] | None = None,
) -> None:
    global _GRANT_CLOCK
    _GRANT_CLOCK = clock or (lambda: datetime.now(timezone.utc))
    _LIVE_GRANTS.clear()


def __setHealthCheckRevalidationTestHookForTests(
    hook: Callable[[str], Any] | None = None,
) -> None:
    global _REVALIDATION_TEST_HOOK
    _REVALIDATION_TEST_HOOK = hook


inspect_health_check_revalidation = inspectHealthCheckRevalidation
authorize_health_check_revalidation = authorizeHealthCheckRevalidation
revalidate_health_check = revalidateHealthCheck
