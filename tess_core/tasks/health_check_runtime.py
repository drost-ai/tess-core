from __future__ import annotations

def _knob(name: str):
    from ..config._knobs import knob_value

    return knob_value(name)


import asyncio
import contextlib
import uuid
from datetime import datetime, timezone
from typing import Any, Callable

from .._utils import maybe_await, now_iso
from ..storage import (
    list_health_check_tasks,
    load_health_check_task,
    load_session,
    save_health_check_task,
    with_record_transaction,
    write_task_meta,
)
from .health_check_executor import append_health_check_log, execute_health_check
from .health_check_policy import (
    assert_health_check_execution_policy,
    health_check_spec_digest,
)
from .health_check_record import (
    build_authorized_health_check_record,
    link_health_checks_to_session,
    persist_health_check_record,
)




def create_health_check_controller(
    *,
    emit: Callable[[str, dict[str, Any], str | None], Any],
    invalidateSession: Callable[[str], Any] | None = None,
    track: Callable[[asyncio.Task[Any]], Any] | None = None,
) -> dict[str, Any]:
    in_flight: dict[str, dict[str, Any]] = {}
    closed = False
    close_task: asyncio.Task[Any] | None = None

    def _track(task: asyncio.Task[Any]) -> None:
        if track:
            track(task)

    async def tick() -> None:
        if closed:
            return
        now_ms = datetime.now(timezone.utc).timestamp() * 1000
        for candidate in await list_health_check_tasks():
            # Legacy (pre-v2) records are inert: never runnable, never
            # mutated. The old migration lane suspended them into a state
            # with no shipped un-suspend path; skipping is the remedy.
            record = candidate
            if not record or not _is_active(record.get("status")) or not _is_runnable(record):
                continue
            last_run_ms = _timestamp_ms(record.get("lastRunAt"))
            interval_ms = int(record.get("currentIntervalMs") or record.get("intervalMs") or 0)
            if record["taskId"] in in_flight or now_ms - last_run_ms < interval_ms:
                continue
            cancel = asyncio.Event()
            generation = int(record["generation"])
            task = asyncio.create_task(_attempt(record, generation, cancel))
            in_flight[record["taskId"]] = {
                "generation": generation,
                "cancel": cancel,
                "task": task,
            }
            task.add_done_callback(
                lambda done, task_id=record["taskId"], expected=generation: _finish(
                    task_id, expected, done
                )
            )
            _track(task)

    async def start(request: dict[str, Any]) -> dict[str, str]:
        if closed:
            raise RuntimeError("Health-check runtime is closed.")
        _validate_start_request(request)
        session_id = request.get("sessionId")
        if not isinstance(session_id, str) or not await load_session(session_id):
            raise RuntimeError(f"Session not found: {session_id}")
        task_id = str(uuid.uuid4())
        record = build_authorized_health_check_record(request=request, taskId=task_id)
        await persist_health_check_record(record)
        await link_health_checks_to_session(session_id, [task_id])
        if invalidateSession:
            invalidateSession(session_id)
        await tick()
        return {"taskId": task_id, "task_id": task_id}

    async def abort_and_drain(task_id: str) -> None:
        active = in_flight.get(task_id)
        if not active:
            return
        active["cancel"].set()
        try:
            await asyncio.wait_for(
                asyncio.shield(active["task"]),
                timeout=(int(_knob("tasks.health_drain_timeout_ms")) / 1000),
            )
        except asyncio.TimeoutError as exc:
            raise RuntimeError(
                f"drain health check {task_id} exceeded "
                f"{(int(_knob("tasks.health_drain_timeout_ms")) / 1000) * 1000}ms"
            ) from exc

    async def _close() -> None:
        nonlocal closed
        closed = True
        active = list(in_flight.values())
        for entry in active:
            entry["cancel"].set()
        if active:
            try:
                await asyncio.wait_for(
                    asyncio.gather(
                        *(entry["task"] for entry in active), return_exceptions=True
                    ),
                    timeout=(int(_knob("tasks.health_drain_timeout_ms")) / 1000),
                )
            except asyncio.TimeoutError as exc:
                raise RuntimeError(
                    "close health-check runtime exceeded "
                    f"{(int(_knob("tasks.health_drain_timeout_ms")) / 1000) * 1000}ms"
                ) from exc
        in_flight.clear()

    async def close() -> None:
        nonlocal close_task
        if close_task is None:
            close_task = asyncio.create_task(_close())
        await asyncio.shield(close_task)

    async def _attempt(
        record: dict[str, Any], generation: int, cancel: asyncio.Event
    ) -> None:
        started_at = now_iso()
        if not await _mark_attempt_started(record["taskId"], generation, started_at):
            return
        try:
            result = await execute_health_check(
                record=record,
                signal=cancel,
                onProcessIdentity=lambda identity: _persist_active_identity(
                    record["taskId"], generation, identity
                ),
            )
        except Exception as exc:
            if closed or cancel.is_set():
                return
            result = _failed_result(exc)
        if closed or cancel.is_set():
            return
        with contextlib.suppress(Exception):
            await append_health_check_log(record, started_at, result)
        committed = await _commit_attempt(
            record["taskId"], generation, started_at, result
        )
        if not committed:
            await maybe_await(
                emit(
                    "health_check.stale_result_dropped",
                    {"task_id": record["taskId"], "generation": generation},
                    record.get("sessionId"),
                )
            )
            return
        if committed["status"] == "failed":
            # The threshold crossing is the one event a health check exists
            # for; it used to be the only transition that emitted nothing.
            await maybe_await(
                emit(
                    "health_check.failed",
                    {
                        "task_id": committed["taskId"],
                        "title": committed["title"],
                        "failures": committed.get("consecutiveFailures", 0),
                        "threshold": committed.get("failureThreshold"),
                        "error": committed.get("lastError"),
                        "generation": generation,
                    },
                    committed.get("sessionId"),
                )
            )
        elif committed["status"] == "unhealthy":
            await maybe_await(
                emit(
                    "health_check.unhealthy",
                    {
                        "task_id": committed["taskId"],
                        "title": committed["title"],
                        "failures": committed.get("consecutiveFailures", 0),
                        "error": committed.get("lastError"),
                        "generation": generation,
                    },
                    committed.get("sessionId"),
                )
            )
        elif record.get("status") != "active" and committed["status"] == "active":
            await maybe_await(
                emit(
                    "health_check.recovered",
                    {
                        "task_id": committed["taskId"],
                        "title": committed["title"],
                        "generation": generation,
                    },
                    committed.get("sessionId"),
                )
            )

    def _finish(task_id: str, generation: int, _task: asyncio.Task[Any]) -> None:
        current = in_flight.get(task_id)
        if current and current["generation"] == generation:
            in_flight.pop(task_id, None)

    return {
        "tick": tick,
        "start": start,
        "abortAndDrain": abort_and_drain,
        "close": close,
        "activeCount": lambda: len(in_flight),
    }


async def _mark_attempt_started(task_id: str, generation: int, started_at: str) -> bool:
    async with with_record_transaction("task-lifecycle", task_id):
        current = await load_health_check_task(task_id)
        if not current or current.get("generation") != generation or not _is_active(current.get("status")):
            return False
        next_record = {
            **current,
            "lastRunAt": started_at,
            "updatedAt": started_at,
            "revision": int(current.get("revision") or 0) + 1,
        }
        await save_health_check_task(next_record)
        await write_task_meta(task_id, {**next_record, "type": "health_check"})
        return True


async def _persist_active_identity(
    task_id: str, generation: int, identity: dict[str, Any]
) -> None:
    async with with_record_transaction("task-lifecycle", task_id):
        current = await load_health_check_task(task_id)
        if not current or current.get("generation") != generation or not _is_active(current.get("status")):
            return
        now = now_iso()
        next_record = {
            **current,
            "activeProcessIdentity": identity,
            "revision": int(current.get("revision") or 0) + 1,
            "updatedAt": now,
        }
        await save_health_check_task(next_record)
        await write_task_meta(task_id, {**next_record, "type": "health_check"})


async def _commit_attempt(
    task_id: str,
    generation: int,
    started_at: str,
    result: dict[str, Any],
) -> dict[str, Any] | None:
    async with with_record_transaction("task-lifecycle", task_id):
        current = await load_health_check_task(task_id)
        if not current or current.get("generation") != generation or not _is_active(current.get("status")):
            return None
        failures = 0 if result["okay"] else int(current.get("consecutiveFailures") or 0) + 1
        threshold = int(current.get("failureThreshold") or 2)
        status = "active" if result["okay"] else "failed" if failures >= threshold else "unhealthy"
        now = now_iso()
        next_record = {
            **current,
            "status": status,
            "updatedAt": now,
            "revision": int(current.get("revision") or 0) + 1,
            "lastRunAt": started_at,
            "lastStatusCode": result.get("lastStatusCode"),
            "lastExitCode": result.get("lastExitCode"),
            "lastSignal": result.get("lastSignal"),
            "lastTimedOut": bool(result.get("lastTimedOut")),
            "lastTerminated": bool(result.get("lastTerminated")),
            "lastResponseSnippet": result.get("responseSnippet") or result.get("outputExcerpt"),
            "lastSuccessAt": started_at if result["okay"] else current.get("lastSuccessAt"),
            "lastFailureAt": current.get("lastFailureAt") if result["okay"] else started_at,
            "lastError": None if result["okay"] else result.get("lastError"),
            "consecutiveFailures": failures,
            "currentIntervalMs": (
                int(current["intervalMs"])
                if result["okay"]
                else _next_backoff_interval_ms(current, failures)
            ),
            "lastProcessIdentity": result.get("processIdentity") or current.get("lastProcessIdentity"),
            "lastOutputArtifacts": result.get("outputArtifacts"),
            "lastMeasuredBytes": result.get("measuredBytes", 0),
            "lastOutputTruncated": bool(result.get("outputTruncated")),
        }
        next_record.pop("activeProcessIdentity", None)
        await persist_health_check_record(next_record)
        return next_record


def _validate_start_request(request: dict[str, Any]) -> None:
    spec = request.get("spec")
    digest = request.get("specDigest")
    policy = request.get("executionPolicy")
    if not isinstance(spec, dict) or health_check_spec_digest(spec) != digest:
        raise RuntimeError("Health-check spec changed after authorization.")
    if not isinstance(policy, dict):
        error = RuntimeError("Health-check start requires explicit execution authorization.")
        setattr(error, "code", "health_check.authorization_required")
        raise error
    assert_health_check_execution_policy(spec, policy)


def _is_runnable(record: dict[str, Any]) -> bool:
    return (
        record.get("schemaVersion") == 2
        and isinstance(record.get("generation"), int)
        and not isinstance(record.get("generation"), bool)
        and record["generation"] >= 1
        and isinstance(record.get("executionPolicy"), dict)
        and isinstance(record.get("specDigest"), str)
    )


def _is_active(status: Any) -> bool:
    return status in {"active", "unhealthy"}


def _timestamp_ms(value: Any) -> float:
    if not isinstance(value, str):
        return 0
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1000
    except ValueError:
        return 0


def _next_backoff_interval_ms(record: dict[str, Any], failures: int) -> int:
    base = int(record.get("intervalMs") or 10_000)
    multiplier = float(record.get("backoffMultiplier") or 2)
    maximum = int(record.get("maxIntervalMs") or base * 4)
    return min(maximum, round(base * multiplier ** max(0, failures - 1)))


def _failed_result(error: Exception) -> dict[str, Any]:
    reason = str(error)
    if len(reason) > 500:
        reason = f"{reason[:497]}..."
    return {
        "okay": False,
        "aborted": False,
        "output": "",
        "outputExcerpt": reason,
        "outputTruncated": False,
        "lastError": reason,
        "measuredBytes": 0,
    }




async def drain_orphaned_health_probes(
    emit: Callable[[str, dict[str, Any], str | None], Any],
) -> int:
    """Boot containment for probe shells orphaned by a daemon crash.

    activeProcessIdentity was previously write-only: no reader ever consulted
    it, so a daemon that died mid-probe leaked one live probe shell per
    crash, forever. At boot no probe can legitimately be in flight, so any
    recorded identity belongs to a dead epoch - drain it (identity-verified,
    never a raw pid kill) and clear the marker."""
    from ..process._termination import drain_owned_process

    drained = 0
    for record in await list_health_check_tasks():
        identity = record.get("activeProcessIdentity")
        task_id = record.get("taskId")
        if not isinstance(identity, dict) or not isinstance(task_id, str) or not task_id:
            continue
        try:
            outcome = await drain_owned_process(identity)
        except Exception as exc:
            outcome = f"unavailable:{type(exc).__name__}"

        def _clear(current: dict[str, Any] | None) -> dict[str, Any] | None:
            if not current or not isinstance(current.get("activeProcessIdentity"), dict):
                return current
            next_record = {key: value for key, value in current.items() if key != "activeProcessIdentity"}
            next_record["revision"] = int(current.get("revision") or 0) + 1
            next_record["updatedAt"] = now_iso()
            return next_record

        from ..storage import mutate_health_check_task

        await mutate_health_check_task(task_id, _clear)
        drained += 1
        await maybe_await(
            emit(
                "health_check.probe_drained",
                {"task_id": task_id, "outcome": outcome or "drained"},
                record.get("sessionId"),
            )
        )
    return drained
