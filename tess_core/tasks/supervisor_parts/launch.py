from __future__ import annotations

import subprocess
import threading
from ..._utils import maybe_await
from ...storage import (
    load_task_record,
    mutate_session_record,
    save_task_record,
    write_task_meta,
)
from .shared import SupervisorRegistries
from typing import Any, Callable
from .live_work import _invalidate_live_work_cache
from .mappers import _task_record_event_payload
from .processes import _spawn_owned_argv, _watch_process_thread
from ...process.owned_launch import activate_owned_process_runner, create_owned_process_activation
from ...process.task_launch import (
    attach_task_launch_reservation,
    fail_task_launch_reservation,
    launch_reserved_process,
)


def _create_process_task_launcher(
    live_work_cache: dict[str, Any],
    emit: Callable[[str, dict[str, Any], str | None], Any],
    wait_for_stop_commit: Callable[[str], None],
    registries: SupervisorRegistries,
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
        activation = create_owned_process_activation(child_argv)
        owned_argv = activation["argv"]

        async def reserve() -> None:
            await write_task_meta(task_id, meta)
            await save_task_record(record)
            for target_session_id, update in reserve_session_updates:
                updated = await mutate_session_record(target_session_id, update)
                if updated is None:
                    raise RuntimeError(f"Session not found during task reservation: {target_session_id}")

        async def attach(identity: dict[str, Any]) -> None:
            attached = attach_task_launch_reservation(reservation)
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
            await write_task_meta(task_id, attached_meta)
            await save_task_record(
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
            activate_owned_process_runner(proc_holder["process"], activation["activationToken"])

        async def fail(error: Any) -> None:
            failed = fail_task_launch_reservation(reservation, error)
            reason = failed.get("failureReason") or "Task launch failed."
            await write_task_meta(
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
            await save_task_record(
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

        launched = await launch_reserved_process(
            reservation=reservation,
            expectedArgv=owned_argv,
            reserve=reserve,
            spawn=spawn,
            attach=attach,
            fail=fail,
        )
        if launched.identity is None:
            raise RuntimeError(f"Task {task_id} exited before its process identity could be attached.")
        registries.processes[task_id] = launched.child
        _invalidate_live_work_cache(live_work_cache, *(target for target, _ in reserve_session_updates))
        current_record = await load_task_record(task_id) or record
        await emit("task.updated", _task_record_event_payload(current_record), session_id)
        watcher = threading.Thread(
            target=_watch_process_thread,
            args=(task_id, command, task_cwd, output_file, emit, wait_for_stop_commit, registries),
            daemon=True,
        )
        registries.threadWatchers.add(watcher)
        watcher.start()
        return {
            "taskId": task_id,
            "task_id": task_id,
            "outputFile": output_file,
            "output_file": output_file,
        }

    return launch_process_task
