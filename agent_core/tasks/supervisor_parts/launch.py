from __future__ import annotations

import subprocess
import threading
from ..._utils import maybe_await
from ...storage import (
    loadTaskRecord,
    mutateSessionRecord,
    saveTaskRecord,
    writeTaskMeta,
)
from .shared import _PROCESS_REGISTRY, _THREAD_WATCHERS
from typing import Any, Callable
from .live_work import _invalidate_live_work_cache
from .mappers import _task_record_event_payload
from .processes import _spawn_owned_argv, _watch_process_thread
from ...process.owned_launch import activateOwnedProcessRunner, createOwnedProcessActivation
from ...process.task_launch import (
    attachTaskLaunchReservation,
    failTaskLaunchReservation,
    launchReservedProcess,
)


def _create_process_task_launcher(
    live_work_cache: dict[str, Any],
    emit: Callable[[str, dict[str, Any], str | None], Any],
    wait_for_stop_commit: Callable[[str], None],
) -> Callable[..., Any]:
    async def launch_process_task(
        *,
        task_id: str,
        session_id: str,
        command: str,
        task_cwd: str,
        output_file: str,
        meta: dict[str, Any],
        record: dict[str, Any],
        child_argv: list[str],
        env: dict[str, str],
        reserve_session_updates: list[tuple[str, Callable[[dict[str, Any]], dict[str, Any]]]],
        attach_extra: Callable[[dict[str, Any]], Any] | None = None,
    ) -> dict[str, str]:
        reservation = meta["launchReservation"]
        activation = createOwnedProcessActivation(child_argv)
        owned_argv = activation["argv"]

        async def reserve() -> None:
            await writeTaskMeta(task_id, meta)
            await saveTaskRecord(record)
            for target_session_id, update in reserve_session_updates:
                updated = await mutateSessionRecord(target_session_id, update)
                if updated is None:
                    raise RuntimeError(f"Session not found during task reservation: {target_session_id}")

        async def attach(identity: dict[str, Any]) -> None:
            attached = attachTaskLaunchReservation(reservation)
            attached_meta = {
                **meta,
                "pid": identity["pid"],
                "processIdentity": identity,
                "launchReservation": attached,
                "status": "running",
                "detailStatus": meta.get("attachedDetailStatus") or meta.get("detailStatus") or "running",
                "updatedAt": attached["attachedAt"],
            }
            attached_meta.pop("attachedDetailStatus", None)
            await writeTaskMeta(task_id, attached_meta)
            await saveTaskRecord(
                {
                    **record,
                    "status": "backgrounded",
                    "summary": meta.get("attachedSummary") or record.get("summary"),
                    "detailStatus": attached_meta["detailStatus"],
                    "processIdentity": identity,
                    "launchReservation": attached,
                    "updatedAt": attached["attachedAt"],
                }
            )
            if attach_extra is not None:
                await maybe_await(attach_extra(identity))
            activateOwnedProcessRunner(proc_holder["process"], activation["activationToken"])

        async def fail(error: Any) -> None:
            failed = failTaskLaunchReservation(reservation, error)
            reason = failed.get("failureReason") or "Task launch failed."
            await writeTaskMeta(
                task_id,
                {
                    **meta,
                    "status": "failed",
                    "detailStatus": "launch_failed",
                    "error": reason,
                    "updatedAt": failed["failedAt"],
                    "finishedAt": failed["failedAt"],
                    "launchReservation": failed,
                },
            )
            await saveTaskRecord(
                {
                    **record,
                    "status": "failed",
                    "summary": reason,
                    "detailStatus": "launch_failed",
                    "updatedAt": failed["failedAt"],
                    "completedAt": failed["failedAt"],
                    "launchReservation": failed,
                }
            )

        proc_holder: dict[str, subprocess.Popen[Any]] = {}

        def spawn() -> subprocess.Popen[Any]:
            process = _spawn_owned_argv(owned_argv, task_cwd, output_file, env=env)
            proc_holder["process"] = process
            return process

        launched = await launchReservedProcess(
            reservation=reservation,
            expectedArgv=owned_argv,
            reserve=reserve,
            spawn=spawn,
            attach=attach,
            fail=fail,
        )
        if launched.identity is None:
            raise RuntimeError(f"Task {task_id} exited before its process identity could be attached.")
        _PROCESS_REGISTRY[task_id] = launched.child
        _invalidate_live_work_cache(live_work_cache, *(target for target, _ in reserve_session_updates))
        current_record = await loadTaskRecord(task_id) or record
        await emit("task.updated", _task_record_event_payload(current_record), session_id)
        watcher = threading.Thread(
            target=_watch_process_thread,
            args=(task_id, command, task_cwd, output_file, emit, wait_for_stop_commit),
            daemon=True,
        )
        _THREAD_WATCHERS.add(watcher)
        watcher.start()
        return {
            "taskId": task_id,
            "task_id": task_id,
            "outputFile": output_file,
            "output_file": output_file,
        }

    return launch_process_task
