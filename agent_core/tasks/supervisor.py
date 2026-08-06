from __future__ import annotations

import sys as _sys
import types as _types

from .supervisor_parts import *
from ..contracts.records import launch_task_id
from .health_check_executor import recordToSpec
from .health_check_runtime import createHealthCheckController
from .task_stop_coordination import createTaskStopCoordinator
from .supervisor_parts import _modules as _SUPERVISOR_PART_MODULES


async def createTaskSupervisor(args: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
    args = {**(args or {}), **kwargs}
    cwd = args.get("cwd") or os.getcwd()
    on_event: Callable[[dict[str, Any]], Any] = args.get("onEvent") or (lambda _event: None)
    await ensureBaseLayout(cwd)
    supervisor_id = str(uuid.uuid4())
    live_work_cache = _create_live_work_verdict_cache()
    subscribers: set[Callable[[dict[str, Any]], Any]] = set()
    attached_all_count = 0
    attached_sessions: dict[str, int] = {}
    reported_record_issues: set[str] = set()
    construction_watchers = set(_WATCHERS)

    async def emit(event: str, data: dict[str, Any], session_id: str | None = None) -> None:
        payload = {"event": event, "data": data, "sessionId": session_id}
        for subscriber in list(subscribers):
            await maybe_await(subscriber(payload))
        await maybe_await(on_event(payload))

    task_stop_coordinator = createTaskStopCoordinator()
    _launch_process_task = _create_process_task_launcher(
        live_work_cache,
        emit,
        task_stop_coordinator.waitForStopCommit,
    )

    def _track_health_task(task: asyncio.Task[Any]) -> None:
        _WATCHERS.add(task)
        task.add_done_callback(_WATCHERS.discard)

    health_check_controller = createHealthCheckController(
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
        if not await loadSession(session_id):
            raise RuntimeError(f"Session not found: {session_id}")
        task_id = str(uuid.uuid4())
        output_file = taskOutputPath(task_id)
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
        session = await loadSession(session_id)
        if not session:
            raise RuntimeError(f"Session not found: {session_id}")
        task_id = str(uuid.uuid4())
        output_file = taskOutputPath(task_id)
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
            "agent_core.process.background_main_session_runner",
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

    start_subagent_task = _create_subagent_task_starter(cwd, supervisor_id, _launch_process_task)
    stop_task = _create_task_stopper(
        task_stop_coordinator,
        live_work_cache,
        emit,
        health_check_controller["abortAndDrain"],
    )

    async def inspect_task(task_id: str) -> dict[str, Any] | None:
        return await _inspect_latest_task_record(task_id)

    async def list_tasks(session_id: str | None = None) -> list[dict[str, Any]]:
        from ..storage import listTaskRecords

        records = await listTaskRecords()
        if session_id:
            records = [record for record in records if record.get("sessionId") == session_id]
        return records

    async def retry_task(task_id: str) -> dict[str, Any]:
        record = await loadTaskRecord(task_id)
        meta = await readTaskMeta(task_id)
        if not record and (not meta or not meta.get("command")):
            raise RuntimeError(f"Task is not retryable: {task_id}")
        kind = (record or {}).get("kind") or (meta or {}).get("type")
        source = record or meta or {}
        if kind == "background_command":
            command = source.get("command")
            if not command:
                raise RuntimeError("Background command task is missing retry metadata.")
            retried = await start_background_command(
                {
                    "sessionId": source.get("sessionId"),
                    "command": command,
                    "cwd": source.get("cwd") or cwd,
                    "title": source.get("title"),
                }
            )
            await _annotate_retry_lineage(task_id, launch_task_id(retried), source)
            return retried
        if kind == "main_session":
            prompt = source.get("command")
            if not prompt:
                raise RuntimeError("Main-session task is missing retry metadata.")
            retried = await start_main_session_task(
                {
                    "sessionId": source.get("sessionId"),
                    "prompt": prompt,
                    "cwd": source.get("cwd") or cwd,
                    "title": source.get("title"),
                }
            )
            await _annotate_retry_lineage(task_id, launch_task_id(retried), source)
            return retried
        if kind == "health_check":
            health = await loadHealthCheckTask(task_id)
            if not health:
                raise RuntimeError(f"Health check metadata not found: {task_id}")
            if (
                health.get("schemaVersion") != 2
                or not isinstance(health.get("executionPolicy"), dict)
                or not isinstance(health.get("specDigest"), str)
                or int(health.get("generation") or 0) < 1
                or health.get("status") not in {"stopped", "failed"}
            ):
                raise RuntimeError(
                    "Health check requires explicit revalidation before retry."
                )
            retried = await health_check_controller["start"](
                {
                    "sessionId": health["sessionId"],
                    "spec": recordToSpec(health),
                    "specDigest": health["specDigest"],
                    "executionPolicy": health["executionPolicy"],
                    "lineageId": health.get("lineageId") or health["taskId"],
                    "supersedes": health["taskId"],
                    "generation": 1,
                }
            )
            await _annotate_retry_lineage(task_id, launch_task_id(retried), source)
            return retried
        if kind == "subagent":
            metadata = source.get("metadata") if isinstance(source.get("metadata"), dict) else {}
            agent_id = metadata.get("agent_id") or source.get("sourceId") or (meta or {}).get("agent_id")
            child = await loadChildAgent(agent_id) if isinstance(agent_id, str) and agent_id else None
            if not child:
                raise RuntimeError(f"Subagent metadata not found for task: {task_id}")
            child_session = await loadSession(child["sessionId"])
            if not child_session:
                raise RuntimeError(f"Subagent session not found: {child['sessionId']}")
            retried = await start_subagent_task(
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
            await _annotate_retry_lineage(task_id, launch_task_id(retried), source)
            return retried
        raise RuntimeError(f"Retry is not supported for task kind: {kind}")

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
        for task_id, proc in list(_PROCESS_REGISTRY.items()):
            if proc.poll() is None:
                with contextlib.suppress(Exception):
                    await stop_task(task_id)
        await health_check_controller["close"]()
        for watcher in list(_WATCHERS):
            if isinstance(watcher, threading.Thread) and watcher is not threading.current_thread() and watcher.is_alive():
                watcher.join(timeout=2)
            elif isinstance(watcher, asyncio.Task):
                watcher.cancel()
        await asyncio.gather(*(watcher for watcher in list(_WATCHERS) if isinstance(watcher, asyncio.Task)), return_exceptions=True)
        _PROCESS_REGISTRY.clear()

    def get_state() -> dict[str, Any]:
        return {"supervisorId": supervisor_id, "watchedSessions": len(attached_sessions), "activeTasks": sum(1 for proc in _PROCESS_REGISTRY.values() if proc.poll() is None) + health_check_controller["activeCount"](), "attachedClients": attached_all_count + sum(attached_sessions.values())}

    async def get_bootstrap(session_id: str) -> dict[str, Any]:
        records = await _project_session_task_records(session_id, live_work_cache=live_work_cache)
        return {"taskSnapshot": _task_snapshot(records), "monitorSnapshot": _monitor_snapshot(await listSessionMonitors(session_id)), "collaborationSnapshot": await _collaboration_snapshot(session_id)}

    async def health_reconcile_loop() -> None:
        while True:
            await asyncio.sleep(2)
            try:
                await health_check_controller["tick"]()
            except Exception as exc:
                await emit(
                    "supervisor.reconcile_failed",
                    {"reason": str(exc), "family": "health_check"},
                )

    try:
        await _reconcile_persisted_tasks(supervisor_id, emit)
        await listChildAgents()
        await listHealthCheckTasks()
        await health_check_controller["tick"]()
        await _report_persisted_record_issues(emit, reported_record_issues)
    except Exception:
        created_watchers = set(_WATCHERS) - construction_watchers
        for watcher in created_watchers:
            if isinstance(watcher, asyncio.Task):
                watcher.cancel()
        await asyncio.gather(
            *(watcher for watcher in created_watchers if isinstance(watcher, asyncio.Task)),
            return_exceptions=True,
        )
        for watcher in created_watchers:
            _WATCHERS.discard(watcher)
        raise

    health_reconcile = asyncio.create_task(health_reconcile_loop())
    _WATCHERS.add(health_reconcile)
    health_reconcile.add_done_callback(_WATCHERS.discard)

    return {
        "close": close,
        "addAttachment": add_attachment,
        "getBootstrap": get_bootstrap,
        "listTasks": list_tasks,
        "inspectTask": inspect_task,
        "readTaskOutput": readTaskOutput,
        **createTaskCommunicationApi(emit),
        "stopTask": stop_task,
        "retryTask": retry_task,
        "startBackgroundCommand": start_background_command,
        "startMainSessionTask": start_main_session_task,
        "startSubagentTask": start_subagent_task,
        "startHealthCheck": start_health_check,
        "subscribe": subscribe,
        "getState": get_state,
    }


_PUBLIC_EXPORTS = ['Any', 'Callable', 'Path', 'annotations', 'asyncio', 'contextlib', 'createTaskSupervisor', 'ensureBaseLayout', 'listChildAgents', 'listHealthCheckTasks', 'listSessionMonitors', 'listTaskRecords', 'loadBridge', 'loadChildAgent', 'loadHealthCheckTask', 'loadRemoteSession', 'loadSession', 'loadTaskRecord', 'maybe_await', 'now_iso', 'os', 'readTaskMeta', 'readTaskOutput', 'saveChildAgent', 'saveHealthCheckTask', 'saveSession', 'saveTaskRecord', 'signal', 'subprocess', 'sys', 'taskOutputPath', 'threading', 'uuid', 'writeTaskMeta']

__all__ = _PUBLIC_EXPORTS


def __dir__():
    return sorted(_PUBLIC_EXPORTS)


class _SupervisorModule(_types.ModuleType):
    def __setattr__(self, name, value):
        super().__setattr__(name, value)
        if name.startswith("__"):
            return
        for module in _SUPERVISOR_PART_MODULES:
            if name in vars(module):
                setattr(module, name, value)


_sys.modules[__name__].__class__ = _SupervisorModule
