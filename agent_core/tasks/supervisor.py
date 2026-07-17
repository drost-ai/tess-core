from __future__ import annotations

import sys as _sys
import types as _types

from .supervisor_parts import *
from ..contracts.records import launch_task_id
from ..subagent_capacity import check_subagent_capacity, resolve_subagent_concurrency_policy
from ..task_communication import append_task_event
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

    async def emit(event: str, data: dict[str, Any], session_id: str | None = None) -> None:
        payload = {"event": event, "data": data, "sessionId": session_id}
        for subscriber in list(subscribers):
            await maybe_await(subscriber(payload))
        await maybe_await(on_event(payload))

    async def start_background_command(task_args: dict[str, Any] | None = None, **task_kwargs: Any) -> dict[str, str]:
        task_args = {**(task_args or {}), **task_kwargs}
        session_id = _require_string(task_args.get("sessionId") or task_args.get("session_id"), "sessionId")
        command = _require_string(task_args.get("command"), "command")
        task_cwd = os.path.abspath(str(task_args.get("cwd") or cwd))
        title = task_args.get("title")
        task_id = str(uuid.uuid4())
        output_file = taskOutputPath(task_id)
        os.makedirs(os.path.dirname(output_file), exist_ok=True)
        started_at = now_iso()
        sandbox_events: list[dict[str, Any]] = []
        proc = _spawn_shell(
            command,
            task_cwd,
            output_file,
            sandbox_config=task_args.get("sandbox"),
            runtime_home=task_args.get("runtimeHome"),
            sandbox_events=sandbox_events,
        )
        _PROCESS_REGISTRY[task_id] = proc
        meta = {
            "type": "background_command",
            "taskId": task_id,
            "id": task_id,
            "sessionId": session_id,
            "command": command,
            "cwd": task_cwd,
            "startedAt": started_at,
            "updatedAt": started_at,
            "status": "running",
            "outputFile": output_file,
            "title": title,
            "description": title,
            "pid": proc.pid,
            "detailStatus": "launching",
            "owner": "daemon",
            "supervisorId": supervisor_id,
        }
        await writeTaskMeta(task_id, meta)
        record = _build_task_record(meta, "Background command launched by daemon.")
        await saveTaskRecord(record)
        for sandbox_event in sandbox_events:
            await emit(sandbox_event["event"], sandbox_event["data"], session_id)

        def _append_task_id(record_session: dict[str, Any]) -> dict[str, Any]:
            metadata = record_session.setdefault("metadata", {})
            metadata["taskIds"] = _dedupe([*(metadata.get("taskIds") or []), task_id])
            return record_session

        await mutateSessionRecord(session_id, _append_task_id)
        _invalidate_live_work_cache(live_work_cache, session_id)
        await emit("task.updated", _task_record_event_payload(record), session_id)
        watcher = threading.Thread(target=_watch_process_thread, args=(task_id, command, task_cwd, output_file, emit), daemon=True)
        _WATCHERS.add(watcher)
        watcher.start()
        return {"taskId": task_id, "task_id": task_id, "outputFile": output_file, "output_file": output_file}

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
        started_at = now_iso()
        def _append_main_session_task(record_session: dict[str, Any]) -> dict[str, Any]:
            metadata = record_session.setdefault("metadata", {})
            metadata["taskIds"] = _dedupe([*(metadata.get("taskIds") or []), task_id])
            metadata["backgroundSessionIds"] = _dedupe([*(metadata.get("backgroundSessionIds") or []), session_id])
            return record_session

        await mutateSessionRecord(session_id, _append_main_session_task)
        _invalidate_live_work_cache(live_work_cache, session_id)
        command = prompt
        proc = _spawn_main_session_worker(task_id, session_id, prompt, task_cwd, output_file, runtime_home=task_args.get("runtimeHome"))
        _PROCESS_REGISTRY[task_id] = proc
        meta = {
            "type": "main_session",
            "taskId": task_id,
            "id": task_id,
            "sessionId": session_id,
            "command": command,
            "cwd": task_cwd,
            "startedAt": started_at,
            "updatedAt": started_at,
            "status": "running",
            "outputFile": output_file,
            "title": title,
            "description": prompt[:180],
            "pid": proc.pid,
            "attachableSessionId": session_id,
            "detailStatus": "session_starting",
            "owner": "daemon",
            "supervisorId": supervisor_id,
        }
        await writeTaskMeta(task_id, meta)
        record = _build_task_record(meta, "Detached main-session work is running in the daemon.")
        await saveTaskRecord(record)
        await emit("task.updated", _task_record_event_payload(record), session_id)
        watcher = threading.Thread(target=_watch_process_thread, args=(task_id, command, task_cwd, output_file, emit), daemon=True)
        _WATCHERS.add(watcher)
        watcher.start()
        return {"taskId": task_id, "task_id": task_id, "outputFile": output_file, "output_file": output_file}

    async def start_subagent_task(task_args: dict[str, Any] | None = None, **task_kwargs: Any) -> dict[str, str]:
        task_args = {**(task_args or {}), **task_kwargs}
        parent_session_id = _require_string(task_args.get("parentSessionId") or task_args.get("parent_session_id"), "parentSessionId")
        session_id = _require_string(task_args.get("sessionId") or task_args.get("session_id"), "sessionId")
        agent_id = _require_string(task_args.get("agentId") or task_args.get("agent_id"), "agentId")
        task_cwd = os.path.abspath(str(task_args.get("cwd") or cwd))
        title = task_args.get("label") or task_args.get("title") or agent_id
        child_session = await loadSession(session_id)
        if not child_session:
            raise RuntimeError(f"Subagent session not found: {session_id}")
        child_metadata = child_session.setdefault("metadata", {})
        runtime_contract = task_args.get("runtimeContract") or task_args.get("runtime_contract") or child_metadata.get("subagentRuntimeContract")
        capacity = check_subagent_capacity(resolve_subagent_concurrency_policy(), await listTaskRecords(), runtime_contract if isinstance(runtime_contract, dict) else None)
        if capacity:
            raise RuntimeError(f"Subagent capacity exceeded: {capacity.get('reason')} ({capacity.get('active')}/{capacity.get('limit')})")
        task_id = str(uuid.uuid4())
        output_file = taskOutputPath(task_id)
        os.makedirs(os.path.dirname(output_file), exist_ok=True)
        started_at = now_iso()
        child_metadata["subagentTaskId"] = task_id
        if isinstance(runtime_contract, dict) and runtime_contract:
            child_metadata["subagentRuntimeContract"] = runtime_contract
        await saveSession(child_session)
        command = f"{sys.executable} -m agent_core.background_subagent_runner --session-id {session_id} --agent-id {agent_id} --task-id {task_id}"
        proc = _spawn_subagent_worker(task_id, session_id, agent_id, task_cwd, output_file, runtime_home=task_args.get("runtimeHome"))
        _PROCESS_REGISTRY[task_id] = proc
        meta = {
            "type": "subagent",
            "taskId": task_id,
            "id": task_id,
            "agent_id": agent_id,
            "sessionId": session_id,
            "session_id": session_id,
            "parent_session_id": parent_session_id,
            "parent_task_id": task_args.get("parentTaskId") or task_args.get("parent_task_id"),
            "parent_tool_use_id": task_args.get("parentToolUseId") or task_args.get("parent_tool_use_id"),
            "parent_turn_id": task_args.get("parentTurnId") or task_args.get("parent_turn_id"),
            "launch_group_id": task_args.get("launchGroupId") or task_args.get("launch_group_id"),
            "command": command,
            "cwd": task_cwd,
            "pid": proc.pid,
            "startedAt": started_at,
            "updatedAt": started_at,
            "status": "running",
            "outputFile": output_file,
            "title": title,
            "description": title,
            "attachableSessionId": session_id,
            "retryable": False,
            "stoppable": True,
            "detailStatus": "worker_running",
            "owner": "daemon",
            "supervisorId": supervisor_id,
            "runtimeContract": runtime_contract if isinstance(runtime_contract, dict) else None,
            "runtimeHome": task_args.get("runtimeHome"),
            "concurrencyGroup": runtime_contract.get("concurrencyGroup") if isinstance(runtime_contract, dict) else None,
            "timeoutMs": runtime_contract.get("timeoutMs") if isinstance(runtime_contract, dict) else None,
            "metadata": {
                "runtime_contract": runtime_contract if isinstance(runtime_contract, dict) else None,
                "concurrency_group": runtime_contract.get("concurrencyGroup") if isinstance(runtime_contract, dict) else None,
                "launch_group_id": task_args.get("launchGroupId") or task_args.get("launch_group_id"),
                "parent_tool_use_id": task_args.get("parentToolUseId") or task_args.get("parent_tool_use_id"),
                "parent_turn_id": task_args.get("parentTurnId") or task_args.get("parent_turn_id"),
            },
        }
        await writeTaskMeta(task_id, meta)
        await append_task_event(
            task_id,
            {"type": "task.subagent.started", "agentId": agent_id, "childSessionId": session_id, "parentSessionId": parent_session_id, "launchGroupId": task_args.get("launchGroupId") or task_args.get("launch_group_id"), "pid": proc.pid},
        )
        record = _build_task_record(meta, "Background subagent is running under daemon supervision.")
        await saveTaskRecord(record)
        _invalidate_live_work_cache(live_work_cache, parent_session_id, session_id)
        await emit("task.updated", _task_record_event_payload(record), parent_session_id)
        watcher = threading.Thread(target=_watch_process_thread, args=(task_id, command, task_cwd, output_file, emit), daemon=True)
        _WATCHERS.add(watcher)
        watcher.start()
        return {"taskId": task_id, "task_id": task_id, "outputFile": output_file, "output_file": output_file}

    async def stop_task(task_id: str) -> bool:
        proc = _PROCESS_REGISTRY.get(task_id)
        meta = await readTaskMeta(task_id)
        affected_session_ids = _task_session_ids(meta)
        meta_type = meta.get("type") if meta else None
        if proc and proc.poll() is None and meta_type != "subagent":
            stopped = _terminate_process(proc.pid)
            _invalidate_live_work_cache(live_work_cache, *affected_session_ids)
            return stopped
        stop_time = now_iso()
        health = await mutateHealthCheckTask(
            task_id,
            lambda current: {**current, "status": "stopped", "updatedAt": stop_time, "stoppedAt": stop_time} if current else None,
        )
        if health:
            await writeTaskMeta(task_id, {**health, "type": "health_check", "updatedAt": stop_time})
            _invalidate_live_work_cache(live_work_cache, *affected_session_ids)
            return True
        if meta and meta_type != "subagent" and isinstance(meta.get("pid"), int) and _is_process_alive(meta["pid"]):
            stopped = _terminate_process(meta["pid"])
            _invalidate_live_work_cache(live_work_cache, *affected_session_ids)
            return stopped
        stopped = False
        if meta:
            now = now_iso()
            stop_updates: dict[str, Any] = {"status": "stopped", "updatedAt": now, "stoppedAt": now, "detailStatus": "stopped"}
            if meta.get("type") == "subagent":
                stop_updates.update({"completionReason": "cancelled", "resultSummary": "Stopped by request.", "retryable": True, "stoppable": False})
            loaded_meta = meta

            def _apply_stop(current: dict[str, Any] | None) -> dict[str, Any]:
                return {**(current or loaded_meta), **stop_updates}

            meta = await mutateTaskMeta(task_id, _apply_stop, fallback=loaded_meta)
            if meta.get("type") == "subagent":
                await _mark_subagent_cancelled_from_stop(meta, task_id, now)
            await saveTaskRecord(_build_task_record(meta, "Task stopped by request."))
        if proc and proc.poll() is None:
            stopped = _terminate_process(proc.pid)
            with contextlib.suppress(Exception):
                proc.wait(timeout=2)
        elif meta and isinstance(meta.get("pid"), int):
            stopped = _terminate_process(meta["pid"])
        if meta:
            await emit(_event_name_for_task_status(_map_status(meta.get("status"))), _task_event_payload(meta), meta.get("sessionId"))
        _PROCESS_REGISTRY.pop(task_id, None)
        _invalidate_live_work_cache(live_work_cache, *affected_session_ids)
        return stopped

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
            retried = await start_health_check(health)
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
        session_id = task_args.get("sessionId") or task_args.get("session_id")
        task_id = str(uuid.uuid4())
        now = now_iso()
        interval_ms = max(250, int(task_args.get("intervalMs") or task_args.get("interval_ms") or 10000))
        timeout_ms = max(1000, int(task_args.get("timeoutMs") or task_args.get("timeout_ms") or 10000))
        max_interval_ms = max(250, int(task_args.get("maxIntervalMs") or task_args.get("max_interval_ms") or interval_ms * 4))
        record = {
            "type": "health_check",
            "taskId": task_id,
            "id": task_id,
            "sessionId": session_id,
            "cwd": os.path.abspath(str(task_args.get("cwd") or cwd)),
            "title": _require_string(task_args.get("title"), "title"),
            "mode": task_args.get("mode") or ("command" if task_args.get("command") else "http"),
            "command": task_args.get("command"),
            "url": task_args.get("url"),
            "intervalMs": interval_ms,
            "currentIntervalMs": interval_ms,
            "timeoutMs": timeout_ms,
            "backoffMultiplier": max(1, float(task_args.get("backoffMultiplier") or task_args.get("backoff_multiplier") or 2)),
            "maxIntervalMs": max(interval_ms, max_interval_ms),
            "failureThreshold": max(1, int(task_args.get("failureThreshold") or task_args.get("failure_threshold") or 2)),
            "expectedStatusCodes": task_args.get("expectedStatusCodes") or task_args.get("expected_status_codes"),
            "expectedBodySubstring": task_args.get("expectedBodySubstring") or task_args.get("expected_body_substring"),
            "status": "active",
            "createdAt": now,
            "startedAt": now,
            "updatedAt": now,
            "lastRunAt": None,
            "lastResult": None,
            "consecutiveFailures": 0,
            "outputFile": taskOutputPath(task_id),
        }
        await saveHealthCheckTask(record)
        await writeTaskMeta(task_id, record)
        await saveTaskRecord(_build_task_record(record, "Health check is running."))
        if isinstance(session_id, str) and session_id:

            def _append_health_check_task(record_session: dict[str, Any]) -> dict[str, Any]:
                metadata = record_session.setdefault("metadata", {})
                metadata["taskIds"] = _dedupe([*(metadata.get("taskIds") or []), task_id])
                return record_session

            await mutateSessionRecord(session_id, _append_health_check_task)
            _invalidate_live_work_cache(live_work_cache, session_id)
        watcher = asyncio.create_task(_run_health_check(record, emit))
        _WATCHERS.add(watcher)
        watcher.add_done_callback(_WATCHERS.discard)
        return {"taskId": task_id, "task_id": task_id}

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
        for proc in list(_PROCESS_REGISTRY.values()):
            if proc.poll() is None:
                _terminate_process(proc.pid)
                with contextlib.suppress(Exception):
                    proc.wait(timeout=2)
        for watcher in list(_WATCHERS):
            if isinstance(watcher, threading.Thread) and watcher is not threading.current_thread() and watcher.is_alive():
                watcher.join(timeout=2)
            elif isinstance(watcher, asyncio.Task):
                watcher.cancel()
        await asyncio.gather(*(watcher for watcher in list(_WATCHERS) if isinstance(watcher, asyncio.Task)), return_exceptions=True)
        _PROCESS_REGISTRY.clear()

    def get_state() -> dict[str, Any]:
        return {"supervisorId": supervisor_id, "watchedSessions": len(attached_sessions), "activeTasks": sum(1 for proc in _PROCESS_REGISTRY.values() if proc.poll() is None), "attachedClients": attached_all_count + sum(attached_sessions.values())}

    async def get_bootstrap(session_id: str) -> dict[str, Any]:
        records = await _project_session_task_records(session_id, live_work_cache=live_work_cache)
        return {"taskSnapshot": _task_snapshot(records), "monitorSnapshot": _monitor_snapshot(await listSessionMonitors(session_id)), "collaborationSnapshot": await _collaboration_snapshot(session_id)}

    await _reconcile_persisted_tasks(supervisor_id, emit)

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
