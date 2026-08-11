from __future__ import annotations

import asyncio
import contextlib
import os
import sys
import threading
import uuid
from .._utils import maybe_await
from ..process.task_launch import createTaskLaunchReservation
from ..process._termination import terminate_popen_group
from ..storage.paths import ensure_base_layout
from ..storage.records import (
    list_child_agents,
    list_health_check_tasks,
    list_session_monitors,
    load_child_agent,
    load_health_check_task,
    load_task_record,
    read_task_meta,
    task_output_path,
)
from ..storage.session_archive import load_session
from .supervisor_parts.communication import create_task_communication_api, read_task_output
from .supervisor_parts.health import _inspect_latest_task_record
from .supervisor_parts.launch import _create_process_task_launcher
from .supervisor_parts.monitor_watch import tick_daemon_monitors as _tick_daemon_monitors
from .supervisor_parts.mailbox_watch import create_mailbox_watch_loop
from .supervisor_parts.live_work import _create_live_work_verdict_cache, _invalidate_live_work_cache
from .supervisor_parts.processes import _prepare_shell_argv, _worker_env
from .supervisor_parts.projection import (
    _annotate_retry_lineage,
    _collaboration_snapshot,
    _event_matches_session,
    _monitor_snapshot,
    _project_session_task_records,
    _subscription_session_id,
    _task_snapshot,
)
from .supervisor_parts.reconciliation import _reconcile_persisted_tasks
from .supervisor_parts.record_issues import _report_persisted_record_issues
from .supervisor_parts.records import _build_task_record
from .supervisor_parts.shared import SupervisorRegistries
from .supervisor_parts.stop import _create_task_stopper
from .supervisor_parts.subagent_launch import _create_subagent_task_starter
from .supervisor_parts.utils import _dedupe, _require_string
from typing import Any, Callable
from ..contracts.records import launch_task_id
from .health_check_executor import record_to_spec
from .health_check_runtime import create_health_check_controller, drain_orphaned_health_probes
from .task_stop_coordination import create_task_stop_coordinator


async def create_task_supervisor(args: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
    args = {**(args or {}), **kwargs}
    cwd = args.get("cwd") or os.getcwd()
    on_event: Callable[[dict[str, Any]], Any] = args.get("onEvent") or (lambda _event: None)
    await ensure_base_layout(cwd)
    supervisor_id = str(uuid.uuid4())
    registries = SupervisorRegistries()
    live_work_cache = _create_live_work_verdict_cache()
    subscribers: set[Callable[[dict[str, Any]], Any]] = set()
    attached_all_count = 0
    attached_sessions: dict[str, int] = {}
    reported_record_issues: set[str] = set()

    async def emit(event: str, data: dict[str, Any], session_id: str | None = None) -> None:
        payload = {"event": event, "data": data, "sessionId": session_id}
        for subscriber in list(subscribers):
            await maybe_await(subscriber(payload))
        await maybe_await(on_event(payload))

    task_stop_coordinator = create_task_stop_coordinator()
    _launch_process_task = _create_process_task_launcher(
        live_work_cache,
        emit,
        task_stop_coordinator.waitForStopCommit,
        registries,
    )

    def _track_health_task(task: asyncio.Task[Any]) -> None:
        registries.taskWatchers.add(task)
        task.add_done_callback(registries.taskWatchers.discard)

    health_check_controller = create_health_check_controller(
        emit=emit,
        invalidateSession=lambda session_id: _invalidate_live_work_cache(
            live_work_cache, session_id
        ),
        track=_track_health_task,
    )

    async def start_background_command(task_args: dict[str, Any] | None = None, **task_kwargs: Any) -> dict[str, str]:
        task_args = {**(task_args or {}), **task_kwargs}
        session_id = _require_string(task_args.get("sessionId") or task_args.get("session_id"), "sessionId")
        command = _require_string(task_args.get("command"), "command")
        task_cwd = os.path.abspath(str(task_args.get("cwd") or cwd))
        title = task_args.get("title")
        if not await load_session(session_id):
            raise RuntimeError(f"Session not found: {session_id}")
        task_id = str(uuid.uuid4())
        output_file = task_output_path(task_id)
        os.makedirs(os.path.dirname(output_file), exist_ok=True)
        sandbox_events: list[dict[str, Any]] = []
        child_argv = _prepare_shell_argv(
            command,
            task_cwd,
            sandbox_config=task_args.get("sandbox"),
            runtime_home=task_args.get("runtimeHome"),
            output_file=output_file,
            sandbox_events=sandbox_events,
        )
        reservation = createTaskLaunchReservation(taskId=task_id, sessionId=session_id, kind="background_command")
        meta = {
            "type": "background_command",
            "taskId": task_id,
            "id": task_id,
            "sessionId": session_id,
            "command": command,
            "cwd": task_cwd,
            "startedAt": reservation["createdAt"],
            "updatedAt": reservation["createdAt"],
            "status": "starting",
            "outputFile": output_file,
            "title": title,
            "description": title,
            "detailStatus": "starting",
            "attachedDetailStatus": "launching",
            "attachedSummary": "Background command launched by daemon.",
            "owner": "daemon",
            "supervisorId": supervisor_id,
            "launchReservation": reservation,
        }
        record = {
            **_build_task_record(meta, "Background command is starting."),
            "status": "queued",
            "detailStatus": "starting",
            "launchReservation": reservation,
        }

        def _append_task_id(record_session: dict[str, Any]) -> dict[str, Any]:
            metadata = record_session.setdefault("metadata", {})
            metadata["taskIds"] = _dedupe([*(metadata.get("taskIds") or []), task_id])
            return record_session

        result = await _launch_process_task(
            task_id=task_id,
            session_id=session_id,
            command=command,
            task_cwd=task_cwd,
            output_file=output_file,
            meta=meta,
            record=record,
            child_argv=child_argv,
            env=_worker_env(),
            reserve_session_updates=[(session_id, _append_task_id)],
        )
        for sandbox_event in sandbox_events:
            await emit(sandbox_event["event"], sandbox_event["data"], session_id)
        return result

    async def start_main_session_task(task_args: dict[str, Any] | None = None, **task_kwargs: Any) -> dict[str, str]:
        task_args = {**(task_args or {}), **task_kwargs}
        session_id = _require_string(task_args.get("sessionId") or task_args.get("session_id"), "sessionId")
        prompt = _require_string(task_args.get("prompt"), "prompt")
        task_cwd = os.path.abspath(str(task_args.get("cwd") or cwd))
        title = task_args.get("title") or "Detached main-session task"
        session = await load_session(session_id)
        if not session:
            raise RuntimeError(f"Session not found: {session_id}")
        task_id = str(uuid.uuid4())
        output_file = task_output_path(task_id)
        os.makedirs(os.path.dirname(output_file), exist_ok=True)
        reservation = createTaskLaunchReservation(taskId=task_id, sessionId=session_id, kind="main_session")
        def _append_main_session_task(record_session: dict[str, Any]) -> dict[str, Any]:
            metadata = record_session.setdefault("metadata", {})
            metadata["taskIds"] = _dedupe([*(metadata.get("taskIds") or []), task_id])
            metadata["backgroundSessionIds"] = _dedupe([*(metadata.get("backgroundSessionIds") or []), session_id])
            return record_session

        command = prompt
        child_argv = [
            sys.executable,
            "-m",
            "agent_core.tasks.runners.main_session",
            "--session-id",
            session_id,
            "--task-id",
            task_id,
            "--prompt",
            prompt,
        ]
        meta = {
            "type": "main_session",
            "taskId": task_id,
            "id": task_id,
            "sessionId": session_id,
            "command": command,
            "cwd": task_cwd,
            "startedAt": reservation["createdAt"],
            "updatedAt": reservation["createdAt"],
            "status": "starting",
            "outputFile": output_file,
            "title": title,
            "description": prompt[:180],
            "attachableSessionId": session_id,
            "detailStatus": "starting",
            "attachedDetailStatus": "session_starting",
            "attachedSummary": "Detached main-session work is running in the daemon.",
            "owner": "daemon",
            "supervisorId": supervisor_id,
            "launchReservation": reservation,
        }
        record = {
            **_build_task_record(meta, "Detached main-session work is starting."),
            "status": "queued",
            "detailStatus": "starting",
            "launchReservation": reservation,
        }
        return await _launch_process_task(
            task_id=task_id,
            session_id=session_id,
            command=command,
            task_cwd=task_cwd,
            output_file=output_file,
            meta=meta,
            record=record,
            child_argv=child_argv,
            env=_worker_env({"AGENT_CORE_HOME": task_args.get("runtimeHome")}) if task_args.get("runtimeHome") else _worker_env(),
            reserve_session_updates=[(session_id, _append_main_session_task)],
        )

    start_subagent_task = _create_subagent_task_starter(cwd, supervisor_id, _launch_process_task, emit)
    stop_task = _create_task_stopper(
        task_stop_coordinator,
        live_work_cache,
        emit,
        health_check_controller["abortAndDrain"],
    )

    async def inspect_task(task_id: str) -> dict[str, Any] | None:
        return await _inspect_latest_task_record(task_id)

    async def list_tasks(session_id: str | None = None) -> list[dict[str, Any]]:
        from ..storage import list_task_records

        records = await list_task_records()
        if session_id:
            records = [record for record in records if record.get("sessionId") == session_id]
        return records

    async def _retry_background_command(task_id: str, source: dict[str, Any], meta: dict[str, Any] | None) -> dict[str, Any]:
        command = source.get("command")
        if not command:
            raise RuntimeError("Background command task is missing retry metadata.")
        return await start_background_command(
            {
                "sessionId": source.get("sessionId"),
                "command": command,
                "cwd": source.get("cwd") or cwd,
                "title": source.get("title"),
            }
        )

    async def _retry_main_session(task_id: str, source: dict[str, Any], meta: dict[str, Any] | None) -> dict[str, Any]:
        prompt = source.get("command")
        if not prompt:
            raise RuntimeError("Main-session task is missing retry metadata.")
        return await start_main_session_task(
            {
                "sessionId": source.get("sessionId"),
                "prompt": prompt,
                "cwd": source.get("cwd") or cwd,
                "title": source.get("title"),
            }
        )

    async def _retry_health_check(task_id: str, source: dict[str, Any], meta: dict[str, Any] | None) -> dict[str, Any]:
        health = await load_health_check_task(task_id)
        if not health:
            raise RuntimeError(f"Health check metadata not found: {task_id}")
        # Hand-written trust seam: retrying a health check re-runs an
        # arbitrary probe, so the persisted record must prove it was
        # authorized under the current schema before it may restart.
        if (
            health.get("schemaVersion") != 2
            or not isinstance(health.get("executionPolicy"), dict)
            or not isinstance(health.get("specDigest"), str)
            or int(health.get("generation") or 0) < 1
            or health.get("status") not in {"stopped", "failed"}
        ):
            raise RuntimeError(
                "Legacy health check cannot be retried; start a new health check."
            )
        return await health_check_controller["start"](
            {
                "sessionId": health["sessionId"],
                "spec": record_to_spec(health),
                "specDigest": health["specDigest"],
                "executionPolicy": health["executionPolicy"],
                "lineageId": health.get("lineageId") or health["taskId"],
                "supersedes": health["taskId"],
                "generation": 1,
            }
        )

    async def _retry_subagent(task_id: str, source: dict[str, Any], meta: dict[str, Any] | None) -> dict[str, Any]:
        metadata = source.get("metadata") if isinstance(source.get("metadata"), dict) else {}
        agent_id = metadata.get("agent_id") or source.get("sourceId") or (meta or {}).get("agent_id")
        child = await load_child_agent(agent_id) if isinstance(agent_id, str) and agent_id else None
        if not child:
            raise RuntimeError(f"Subagent metadata not found for task: {task_id}")
        child_session = await load_session(child["sessionId"])
        if not child_session:
            raise RuntimeError(f"Subagent session not found: {child['sessionId']}")
        return await start_subagent_task(
            {
                "parentSessionId": child.get("parentSessionId"),
                "sessionId": child.get("sessionId"),
                "agentId": child.get("agentId"),
                "cwd": child_session.get("cwd") or cwd,
                "label": child.get("name") or child.get("agentType") or child.get("agentId"),
                "parentTaskId": child.get("parentTaskId"),
                "parentToolUseId": child.get("parentToolUseId"),
                "parentTurnId": child.get("parentTurnId"),
                "launchGroupId": child.get("launchGroupId"),
                "runtimeContract": child.get("runtimeContract") or (child_session.get("metadata") or {}).get("subagentRuntimeContract"),
            }
        )

    retriers: dict[str, Callable[..., Any]] = {
        "background_command": _retry_background_command,
        "main_session": _retry_main_session,
        "health_check": _retry_health_check,
        "subagent": _retry_subagent,
    }

    async def retry_task(task_id: str) -> dict[str, Any]:
        record = await load_task_record(task_id)
        meta = await read_task_meta(task_id)
        if not record and (not meta or not meta.get("command")):
            raise RuntimeError(f"Task is not retryable: {task_id}")
        kind = (record or {}).get("kind") or (meta or {}).get("type")
        source = record or meta or {}
        retrier = retriers.get(kind or "")
        if retrier is None:
            raise RuntimeError(f"Retry is not supported for task kind: {kind}")
        retried = await retrier(task_id, source, meta)
        await _annotate_retry_lineage(task_id, launch_task_id(retried), source)
        return retried

    async def start_health_check(task_args: dict[str, Any] | None = None, **task_kwargs: Any) -> dict[str, str]:
        task_args = {**(task_args or {}), **task_kwargs}
        if not all(
            key in task_args
            for key in ("sessionId", "spec", "specDigest", "executionPolicy")
        ):
            error = RuntimeError(
                "Health-check start requires explicit execution authorization."
            )
            setattr(error, "code", "health_check.authorization_required")
            raise error
        return await health_check_controller["start"](task_args)

    def subscribe(listener: Callable[[dict[str, Any]], Any], sessionId: str | dict[str, Any] | None = None):
        session_id = _subscription_session_id(sessionId)

        async def filtered(payload: dict[str, Any]) -> None:
            if _event_matches_session(payload, session_id):
                await maybe_await(listener(payload))

        subscribers.add(filtered)

        def unsubscribe() -> None:
            subscribers.discard(filtered)

        return unsubscribe

    def add_attachment(sessionId: str | dict[str, Any] | None = None):
        nonlocal attached_all_count
        session_id = _subscription_session_id(sessionId)
        if session_id:
            attached_sessions[session_id] = attached_sessions.get(session_id, 0) + 1
        else:
            attached_all_count += 1

        def release() -> None:
            nonlocal attached_all_count
            if session_id:
                next_count = attached_sessions.get(session_id, 1) - 1
                if next_count <= 0:
                    attached_sessions.pop(session_id, None)
                else:
                    attached_sessions[session_id] = next_count
            else:
                attached_all_count = max(0, attached_all_count - 1)

        return release

    async def close() -> None:
        live = [(task_id, proc) for task_id, proc in list(registries.processes.items()) if proc.poll() is None]
        if live:
            # Report-then-kill, no prompt: the parent learns what is being
            # stopped, then every owned worker is escalated to guaranteed death.
            await maybe_await(emit("supervisor.stopping_workers", {"count": len(live), "task_ids": [task_id for task_id, _proc in live]}))
        for task_id, proc in live:
            with contextlib.suppress(Exception):
                await stop_task(task_id)
            outcome = await terminate_popen_group(proc)
            if outcome == "survived":
                await maybe_await(emit("supervisor.reconcile_failed", {"reason": f"worker for {task_id} survived SIGKILL", "family": "shutdown"}))
        await health_check_controller["close"]()
        for thread in list(registries.threadWatchers):
            if thread is not threading.current_thread() and thread.is_alive():
                thread.join(timeout=2)
        for task in list(registries.taskWatchers):
            task.cancel()
        await asyncio.gather(*list(registries.taskWatchers), return_exceptions=True)
        registries.processes.clear()

    def get_state() -> dict[str, Any]:
        return {"supervisorId": supervisor_id, "watchedSessions": len(attached_sessions), "activeTasks": sum(1 for proc in registries.processes.values() if proc.poll() is None) + health_check_controller["activeCount"](), "attachedClients": attached_all_count + sum(attached_sessions.values())}

    async def get_bootstrap(session_id: str) -> dict[str, Any]:
        records = await _project_session_task_records(session_id, live_work_cache=live_work_cache)
        return {"taskSnapshot": _task_snapshot(records), "monitorSnapshot": _monitor_snapshot(await list_session_monitors(session_id)), "collaborationSnapshot": await _collaboration_snapshot(session_id)}

    async def health_reconcile_loop() -> None:
        from ..config._knobs import knob_value

        while True:
            await asyncio.sleep(int(knob_value("daemon.health_tick_ms")) / 1000)
            try:
                await health_check_controller["tick"]()
            except Exception as exc:
                await emit(
                    "supervisor.reconcile_failed",
                    {"reason": str(exc), "family": "health_check"},
                )

    async def monitor_watch_loop() -> None:
        from ..config._knobs import knob_value

        while True:
            await asyncio.sleep(int(knob_value("tasks.monitor_tick_ms")) / 1000)
            try:
                await _tick_daemon_monitors(emit)
            except Exception as exc:
                await emit(
                    "supervisor.reconcile_failed",
                    {"reason": str(exc), "family": "monitors"},
                )

    try:
        await _reconcile_persisted_tasks(supervisor_id, emit, registries)
        await list_child_agents()
        await drain_orphaned_health_probes(emit)
        await health_check_controller["tick"]()
        await _tick_daemon_monitors(emit)
        await _report_persisted_record_issues(emit, reported_record_issues)
    except Exception:
        # The registries are per-instance, so everything in them was created
        # by this construction and is ours to unwind.
        for task in list(registries.taskWatchers):
            task.cancel()
        await asyncio.gather(*list(registries.taskWatchers), return_exceptions=True)
        registries.taskWatchers.clear()
        registries.threadWatchers.clear()
        raise

    health_reconcile = asyncio.create_task(health_reconcile_loop())
    registries.taskWatchers.add(health_reconcile)
    health_reconcile.add_done_callback(registries.taskWatchers.discard)
    monitor_watch = asyncio.create_task(monitor_watch_loop())
    registries.taskWatchers.add(monitor_watch)
    monitor_watch.add_done_callback(registries.taskWatchers.discard)
    mailbox_watch = asyncio.create_task(create_mailbox_watch_loop(emit)())
    registries.taskWatchers.add(mailbox_watch)
    mailbox_watch.add_done_callback(registries.taskWatchers.discard)

    return {
        "close": close,
        "addAttachment": add_attachment,
        "getBootstrap": get_bootstrap,
        "listTasks": list_tasks,
        "inspectTask": inspect_task,
        "read_task_output": read_task_output,
        **create_task_communication_api(emit),
        "stopTask": stop_task,
        "retryTask": retry_task,
        "startBackgroundCommand": start_background_command,
        "startMainSessionTask": start_main_session_task,
        "startSubagentTask": start_subagent_task,
        "startHealthCheck": start_health_check,
        "subscribe": subscribe,
        "getState": get_state,
        "registries": registries,
    }


