from __future__ import annotations

from ..contracts.context import ToolContext

import asyncio
import os
from pathlib import Path
from typing import Any

from .._utils import maybe_await, now_iso, pretty_json
from ..execution_descriptors import createTaskExecutionDescriptor
from ..monitors import createMonitorRecord, defaultMonitorCheckIntervalMs, defaultMonitorHeartbeatIntervalMs
from ..storage import listSessionMonitors, loadMonitor, readTaskMeta, saveMonitor, taskOutputPath, writeTaskMeta
from ..tool_payloads import shapeTextToolResult
from .cancellation import _throw_if_tool_cancelled
from .definitions import ToolDefinition
from .permissions import _ensure_tool_permission
from .processes import _wait_for_tracked_background_runner
from .schemas import _tool_description, _tool_schema
from .session_metadata import _append_unique_metadata_list
from .validation import _require_string, _without_none_values


def _task_output_tool() -> ToolDefinition:
    async def handler(input: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        task_id = _require_string(input.get("task_id"), "task_id")
        if input.get("block"):
            started = asyncio.get_running_loop().time()
            timeout = float(input.get("timeout") or 30000) / 1000
            while asyncio.get_running_loop().time() - started < timeout:
                _throw_if_tool_cancelled(context)
                meta = await readTaskMeta(task_id)
                if not meta or meta.get("status") not in ("running", "reconciling"):
                    break
                await asyncio.sleep(0.25)
        meta = await readTaskMeta(task_id)
        if not meta:
            return {"content": f"Task not found: {task_id}", "isError": True}
        output_result = None
        daemon = context.get("daemon")
        if daemon:
            try:
                output_result = await daemon["readTaskOutput"](task_id)
            except Exception:
                output_result = None
        text = output_result.get("output") if output_result else Path(taskOutputPath(task_id)).read_text(encoding="utf-8", errors="replace") if Path(taskOutputPath(task_id)).exists() else ""
        shaped = await shapeTextToolResult(config=context["config"], sessionId=context["session"]["id"], toolName="TaskOutput", family="bash", text=text, taskId=task_id, strategy="tail")
        execution = createTaskExecutionDescriptor(meta, text)
        return {"content": pretty_json({**meta, "output": shaped["text"], "output_truncated": bool(output_result and output_result.get("truncated")), "output_manifest": shaped["view"], "execution": execution}), "meta": {"execution": execution}}

    return ToolDefinition("TaskOutput", _tool_description("TaskOutput"), _tool_schema("TaskOutput"), handler)


def _task_stop_tool() -> ToolDefinition:
    async def handler(input: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        task_id = _require_string(input.get("task_id"), "task_id")
        supervisor = context.get("daemon") or context.get("taskSupervisor")
        if supervisor:
            await _ensure_tool_permission(context, "TaskStop", "process", f"Stop background task {task_id}")
            stopped = await supervisor["stopTask"](task_id)
            if not stopped:
                return {"content": f"Task not found or not stoppable: {task_id}", "isError": True}
            context.get("outputJson", lambda *_: None)(
                "task.failed",
                {
                    "session_id": context["session"]["id"],
                    "task_id": task_id,
                    "summary": "Task terminated by user request.",
                },
            )
            return {"content": pretty_json({"task_id": task_id, "status": "terminated"})}
        meta = await readTaskMeta(task_id)
        if not meta:
            return {"content": f"Task not found: {task_id}", "isError": True}
        pid = meta.get("pid")
        if not isinstance(pid, int):
            return {"content": f"Task {task_id} has no PID", "isError": True}
        await _ensure_tool_permission(context, "TaskStop", "process", f"Stop background task {task_id} (pid {pid})")
        try:
            os.kill(pid, 15)
        except ProcessLookupError:
            pass
        meta.update({"status": "terminated", "stoppedAt": now_iso(), "updatedAt": now_iso()})
        await writeTaskMeta(task_id, meta)
        runner_pid = meta.get("runnerPid")
        if isinstance(runner_pid, int):
            await _wait_for_tracked_background_runner(runner_pid)
        execution = _stopped_task_execution_descriptor(task_id, meta)
        context.get("outputJson", lambda *_: None)(
            "task.failed",
            {
                "session_id": context["session"]["id"],
                "task_id": task_id,
                "kind": meta.get("type") or "background_command",
                "title": meta.get("title") or meta.get("description") or meta.get("command"),
                "summary": "Task terminated by user request.",
            },
        )
        return {"content": pretty_json({"task_id": task_id, "status": "terminated", "execution": execution}), "meta": {"execution": execution}}

    return ToolDefinition("TaskStop", _tool_description("TaskStop"), _tool_schema("TaskStop"), handler)


def _task_monitor_start_tool() -> ToolDefinition:
    async def handler(input: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        task_id = _require_string(input.get("task_id"), "task_id")
        goal = _require_string(input.get("goal"), "goal")
        task = await readTaskMeta(task_id)
        if not task:
            return {"content": f"Task not found: {task_id}", "isError": True}
        existing = next((monitor for monitor in await listSessionMonitors(context["session"]["id"]) if monitor.get("taskId") == task_id and monitor.get("status") == "active"), None)
        if existing:
            return {"content": pretty_json({"monitor_id": existing.get("id"), "task_id": existing.get("taskId"), "status": existing.get("status"), "goal": existing.get("goal"), "note": "Monitor already active for this task."})}
        summary_style = input.get("summary_style") if isinstance(input.get("summary_style"), str) and input.get("summary_style").strip() else None
        record = createMonitorRecord(
            {
                "sessionId": context["session"]["id"],
                "taskId": task_id,
                "goal": goal,
                "summaryStyle": summary_style.strip() if isinstance(summary_style, str) else None,
                "checkIntervalMs": defaultMonitorCheckIntervalMs(input.get("check_interval_ms")),
                "heartbeatIntervalMs": defaultMonitorHeartbeatIntervalMs(input.get("heartbeat_interval_ms")),
            }
        )
        if summary_style:
            record["summaryStyle"] = summary_style.strip()
        await saveMonitor(record)
        _append_unique_metadata_list(context["session"], "monitorIds", record["id"])
        await maybe_await(context.get("persistSession", lambda: None)())
        context.get("outputJson", lambda *_: None)("task.registered", {"session_id": context["session"]["id"], "task_id": f"monitor:{record['id']}", "kind": "monitor", "status": "running", "title": f"Monitor {goal}"})
        context.get("outputJson", lambda *_: None)("monitor.created", {"session_id": context["session"]["id"], "monitor_id": record["id"], "task_id": task_id, "goal": goal, "status": record["status"]})
        execution = {
            "id": record["id"],
            "kind": "task",
            "title": f"Monitoring task {task_id}",
            "status": "running",
            "cwd": task.get("cwd"),
            "command": task.get("command"),
            "startedAt": record.get("createdAt"),
            "metadata": _without_none_values({"monitor_id": record["id"], "summary_style": record.get("summaryStyle")}),
        }
        return {
            "content": pretty_json(
                {
                    "monitor_id": record["id"],
                    "task_id": task_id,
                    "status": record["status"],
                    "goal": goal,
                    "check_interval_ms": record["checkIntervalMs"],
                    "heartbeat_interval_ms": record["heartbeatIntervalMs"],
                    "execution": execution,
                }
            )
        }

    return ToolDefinition("TaskMonitorStart", _tool_description("TaskMonitorStart"), _tool_schema("TaskMonitorStart"), handler)


def _task_monitor_stop_tool() -> ToolDefinition:
    async def handler(input: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        monitor_id = _require_string(input.get("monitor_id"), "monitor_id")
        monitor = await loadMonitor(monitor_id)
        if not monitor or monitor.get("sessionId") != context["session"]["id"]:
            return {"content": f"Monitor not found: {monitor_id}", "isError": True}
        monitor.update({"status": "stopped", "updatedAt": now_iso(), "stoppedAt": now_iso()})
        await saveMonitor(monitor)
        context.get("outputJson", lambda *_: None)(
            "task.failed",
            {
                "session_id": context["session"]["id"],
                "task_id": f"monitor:{monitor['id']}",
                "kind": "monitor",
                "title": f"Monitor {monitor.get('goal')}",
                "summary": "Monitor stopped.",
            },
        )
        context.get("outputJson", lambda *_: None)("monitor.status_changed", {"session_id": context["session"]["id"], "monitor_id": monitor["id"], "task_id": monitor.get("taskId"), "status": monitor["status"]})
        return {"content": pretty_json({"monitor_id": monitor["id"], "task_id": monitor.get("taskId"), "status": monitor["status"]})}

    return ToolDefinition("TaskMonitorStop", _tool_description("TaskMonitorStop"), _tool_schema("TaskMonitorStop"), handler)


def _task_health_check_start_tool() -> ToolDefinition:
    async def handler(input: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        supervisor = context.get("daemon")
        if not supervisor:
            raise RuntimeError("Health checks require the local task supervisor daemon.")
        title = _require_string(input.get("title"), "title")
        mode = _require_string(input.get("mode"), "mode")
        started = await supervisor["startHealthCheck"](
            {
                "sessionId": context["session"]["id"],
                "cwd": context["session"]["cwd"],
                "title": title,
                "mode": "command" if mode == "command" else "http",
                "command": input.get("command") if isinstance(input.get("command"), str) else None,
                "url": input.get("url") if isinstance(input.get("url"), str) else None,
                "intervalMs": input.get("interval_ms") if isinstance(input.get("interval_ms"), (int, float)) else None,
                "timeoutMs": input.get("timeout_ms") if isinstance(input.get("timeout_ms"), (int, float)) else None,
                "failureThreshold": input.get("failure_threshold") if isinstance(input.get("failure_threshold"), (int, float)) else None,
                "backoffMultiplier": input.get("backoff_multiplier") if isinstance(input.get("backoff_multiplier"), (int, float)) else None,
                "maxIntervalMs": input.get("max_interval_ms") if isinstance(input.get("max_interval_ms"), (int, float)) else None,
                "expectedStatusCodes": [value for value in input.get("expected_status_codes", []) if isinstance(value, (int, float))] if isinstance(input.get("expected_status_codes"), list) else None,
                "expectedBodySubstring": input.get("expected_body_substring") if isinstance(input.get("expected_body_substring"), str) and input.get("expected_body_substring").strip() else None,
            }
        )
        task_id = started.get("taskId") or started.get("task_id")
        _append_unique_metadata_list(context["session"], "taskIds", task_id)
        await maybe_await(context.get("persistSession", lambda: None)())
        context.get("outputJson", lambda *_: None)("task.registered", {"session_id": context["session"]["id"], "task_id": task_id, "kind": "health_check", "status": "running", "title": title, "owner": "daemon"})
        return {"content": pretty_json({"task_id": task_id, "status": "running", "kind": "health_check", "title": title})}

    return ToolDefinition("TaskHealthCheckStart", _tool_description("TaskHealthCheckStart"), _tool_schema("TaskHealthCheckStart"), handler)


def _task_health_check_stop_tool() -> ToolDefinition:
    async def handler(input: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        supervisor = context.get("daemon")
        if not supervisor:
            raise RuntimeError("Health checks require the local task supervisor daemon.")
        task_id = _require_string(input.get("task_id"), "task_id")
        stopped = await supervisor["stopTask"](task_id)
        if not stopped:
            raise RuntimeError(f"Task not found or not stoppable: {task_id}")
        context.get("outputJson", lambda *_: None)("task.failed", {"session_id": context["session"]["id"], "task_id": task_id, "kind": "health_check", "status": "stopped"})
        return {"content": pretty_json({"task_id": task_id, "stopped": True})}

    return ToolDefinition("TaskHealthCheckStop", _tool_description("TaskHealthCheckStop"), _tool_schema("TaskHealthCheckStop"), handler)


def _stopped_task_execution_descriptor(task_id: str, meta: dict[str, Any]) -> dict[str, Any]:
    return _without_none_values(
        {
            "id": task_id,
            "kind": "task",
            "title": f"Stopped background task {task_id}",
            "status": "terminated",
            "cwd": meta.get("cwd"),
            "command": meta.get("command"),
            "startedAt": meta.get("startedAt"),
            "finishedAt": meta.get("finishedAt") or meta.get("stoppedAt") or now_iso(),
            "metadata": _without_none_values({"pid": meta.get("pid")}),
        }
    )
