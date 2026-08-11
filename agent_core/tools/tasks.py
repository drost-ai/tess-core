from __future__ import annotations

from ..contracts.context import ToolContext
from ..contracts.records import launch_task_id
from ..contracts.tools import ToolResult

import asyncio
import contextlib
import inspect
import signal
from typing import Any

from .._utils import maybe_await, now_iso, pretty_json
from ..payloads.bounded_io import read_file_tail_bounded
from ..payloads.execution_descriptors import create_task_execution_descriptor
from ..tasks.monitor_transitions import monitor_stop_note as _monitor_stop_note
from ..tasks.monitor_transitions import stop_monitor_record as _stop_monitor_record
from ..tasks.health_check_policy import (
    authorize_health_check_spec,
    health_check_spec_digest,
    normalize_health_check_spec,
)
from ..tasks.monitor_transactions import (
    commit_monitor_mutation,
    monitor_transition_event_fields,
    start_monitor_once,
)
from ..tasks.monitors import create_monitor_record, default_monitor_check_interval_ms, default_monitor_heartbeat_interval_ms
from ..process.identity import processIdentityFromRecord, signalOwnedProcess
from ..tasks.terminal_store import clear_prepared_task_stop, commit_prepared_task_stop, prepare_task_stop
from ..storage import list_session_monitors, load_monitor, read_task_meta, save_monitor, task_output_path
from ..tasks.ownership import require_model_owned_monitor, require_model_owned_task
from ..payloads.tools import get_tool_payload_policy, shape_text_tool_result
from .cancellation import _throw_if_tool_cancelled
from .definitions import ToolDefinition
from .permissions import _ensure_tool_permission
from .schemas import builtin_tool
from .session_metadata import _append_unique_metadata_list
from .validation import _require_string, _without_none_values


def _task_output_tool() -> ToolDefinition:
    async def handler(input: dict[str, Any], context: ToolContext) -> ToolResult:
        task_id = _require_string(input.get("task_id"), "task_id")
        await require_model_owned_task(context, task_id, "output")
        if input.get("block"):
            started = asyncio.get_running_loop().time()
            timeout = float(input.get("timeout") or 30000) / 1000
            while asyncio.get_running_loop().time() - started < timeout:
                _throw_if_tool_cancelled(context)
                meta = await read_task_meta(task_id)
                if not meta or meta.get("status") not in ("running", "reconciling"):
                    break
                from ..config._knobs import knob_value

                await asyncio.sleep(int(knob_value("tools.task_output_poll_ms")) / 1000)
        owned = await require_model_owned_task(context, task_id, "output")
        meta = owned["meta"]
        policy = get_tool_payload_policy(context["config"])
        output_result = None
        daemon = context.get("daemon")
        if daemon:
            try:
                output_result = await _read_daemon_task_output(
                    daemon["read_task_output"], task_id, policy["textInlineBytes"]
                )
            except Exception:
                output_result = None
        fallback_output = None
        if output_result is None:
            try:
                fallback_output = await read_file_tail_bounded(
                    filePath=task_output_path(task_id),
                    maxBytes=policy["textInlineBytes"],
                )
            except (OSError, ValueError):
                fallback_output = {
                    "text": "",
                    "originalBytes": 0,
                    "returnedBytes": 0,
                    "truncated": False,
                    "changed": False,
                }
        text = output_result.get("output", "") if output_result is not None else fallback_output["text"]
        source_truncated = bool(
            output_result.get("truncated") if output_result is not None else fallback_output["truncated"]
        )
        original_bytes = (
            fallback_output["originalBytes"]
            if fallback_output is not None
            else len(text.encode("utf-8", errors="replace"))
        )
        shaped = await shape_text_tool_result(
            config=context["config"],
            sessionId=context["session"]["id"],
            toolName="TaskOutput",
            family="bash",
            text=text,
            taskId=task_id,
            strategy="tail",
            persistArtifact=False,
        )
        execution = create_task_execution_descriptor(meta, text)
        output_file = output_result.get("outputFile") if output_result is not None else None
        output_file = output_file or task_output_path(task_id)
        output_manifest = {
            **shaped["view"],
            "originalBytes": original_bytes,
            "sourceFile": output_file,
            "sourceTruncated": source_truncated,
            **(
                {"changedDuringRead": True}
                if fallback_output is not None and fallback_output["changed"]
                else {}
            ),
            "recovery": {
                "type": "task_output_file",
                "filePath": output_file,
                "taskId": task_id,
            },
        }
        return {
            "content": pretty_json(
                {
                    **meta,
                    "output": shaped["text"],
                    "output_truncated": source_truncated or shaped["view"]["truncated"],
                    "output_manifest": output_manifest,
                    "execution": execution,
                }
            ),
            "meta": {"execution": execution},
        }

    return builtin_tool("TaskOutput", handler)


async def _read_daemon_task_output(reader: Any, task_id: str, max_bytes: int) -> Any:
    try:
        signature = inspect.signature(reader)
    except (TypeError, ValueError):
        return await reader(task_id, max_bytes)
    try:
        signature.bind(task_id, max_bytes)
    except TypeError:
        return await reader(task_id)
    return await reader(task_id, max_bytes)


def _task_stop_tool() -> ToolDefinition:
    async def handler(input: dict[str, Any], context: ToolContext) -> ToolResult:
        task_id = _require_string(input.get("task_id"), "task_id")
        owned = await require_model_owned_task(context, task_id, "stop")
        existing_status = _terminal_task_status(
            (owned.get("record") or {}).get("status"),
            (owned.get("meta") or {}).get("status"),
        )
        if existing_status:
            semantic_status = "stopped" if existing_status == "terminated" else existing_status
            return {
                "content": pretty_json(
                    {
                        "task_id": task_id,
                        "previous_status": semantic_status,
                        "status": semantic_status,
                        "changed": False,
                        "stopped": False,
                        "note": "Task was already terminal; no process signal was sent.",
                    }
                )
            }
        supervisor = context.get("daemon") or context.get("taskSupervisor")
        if supervisor:
            await _ensure_tool_permission(context, "TaskStop", "process", f"Stop background task {task_id}")
            stopped = await supervisor["stopTask"](task_id)
            if not stopped:
                return {"content": f"Task not found or not stoppable: {task_id}", "isError": True}
            if stopped is True:
                stopped = {
                    "previousStatus": "running",
                    "status": "stopped",
                    "changed": True,
                    "note": "Task stopped by deliberate request.",
                }
            return {"content": pretty_json(_task_stop_content(task_id, stopped))}
        owned = await require_model_owned_task(context, task_id, "stop")
        meta = owned["meta"]
        identity = processIdentityFromRecord(owned.get("record"), meta)
        await _ensure_tool_permission(context, "TaskStop", "process", f"Stop background task {task_id} (pid {identity['pid']})")
        prepared = await prepare_task_stop(task_id, "SIGTERM", "tool")
        if not prepared:
            return {"content": f"Task not found or not stoppable: {task_id}", "isError": True}
        if prepared.get("result"):
            return {"content": pretty_json(_task_stop_content(task_id, prepared["result"]))}
        transition_id = prepared.get("transitionId")
        prepared_identity = prepared.get("identity")
        if not isinstance(transition_id, str) or not isinstance(prepared_identity, dict):
            raise RuntimeError(
                f"Task stop preparation did not capture an owned process: {task_id}"
            )
        try:
            await signalOwnedProcess(prepared_identity, signal.SIGTERM)
        except Exception:
            with contextlib.suppress(Exception):
                await clear_prepared_task_stop(task_id, transition_id)
            raise
        committed = await commit_prepared_task_stop(task_id, transition_id, "tool")
        stop_result = committed["result"]
        record = committed.get("record")
        if stop_result.get("changed") and isinstance(record, dict):
            context.get("outputJson", lambda *_: None)(
                "task.stopped",
                {
                    "session_id": context["session"]["id"],
                    "task_id": task_id,
                    "kind": record.get("kind"),
                    "title": record.get("title"),
                    "summary": record.get("summary"),
                    "transition_id": stop_result.get("transitionId"),
                },
            )
        committed_meta = await read_task_meta(task_id) or meta
        execution = _stopped_task_execution_descriptor(task_id, committed_meta)
        return {
            "content": pretty_json(
                {**_task_stop_content(task_id, stop_result), "execution": execution}
            ),
            "meta": {"execution": execution},
        }

    return builtin_tool("TaskStop", handler)


def _task_monitor_start_tool() -> ToolDefinition:
    async def handler(input: dict[str, Any], context: ToolContext) -> ToolResult:
        task_id = _require_string(input.get("task_id"), "task_id")
        goal = _require_string(input.get("goal"), "goal")
        owned = await require_model_owned_task(context, task_id, "monitor")
        task = owned["meta"]
        summary_style = input.get("summary_style") if isinstance(input.get("summary_style"), str) and input.get("summary_style").strip() else None
        started = await start_monitor_once(
            sessionId=context["session"]["id"],
            taskId=task_id,
            create=lambda: create_monitor_record(
                {
                    "sessionId": context["session"]["id"],
                    "taskId": task_id,
                    "goal": goal,
                    "summaryStyle": summary_style.strip() if isinstance(summary_style, str) else None,
                    "checkIntervalMs": default_monitor_check_interval_ms(input.get("check_interval_ms")),
                    "heartbeatIntervalMs": default_monitor_heartbeat_interval_ms(input.get("heartbeat_interval_ms")),
                }
            ),
        )
        record = started["monitor"]
        if summary_style:
            record["summaryStyle"] = summary_style.strip()
        monitor_ids = context["session"].setdefault("metadata", {}).get("monitorIds") or []
        if record["id"] not in monitor_ids:
            _append_unique_metadata_list(context["session"], "monitorIds", record["id"])
            await maybe_await(context.get("persist_session", lambda: None)())
        if not started["changed"]:
            return {
                "content": pretty_json(
                    {
                        "monitor_id": record["id"],
                        "task_id": record["taskId"],
                        "status": record["status"],
                        "goal": record.get("goal"),
                        "changed": False,
                        "revision": started["revision"],
                        "note": "Monitor already active for this task.",
                    }
                )
            }
        context.get("outputJson", lambda *_: None)("task.registered", {"session_id": context["session"]["id"], "task_id": f"monitor:{record['id']}", "kind": "monitor", "status": "running", "title": f"Monitor {goal}"})
        context.get("outputJson", lambda *_: None)("monitor.created", {"session_id": context["session"]["id"], "monitor_id": record["id"], "task_id": task_id, "goal": goal, "status": record["status"], "changed": True, **monitor_transition_event_fields(started)})
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
                    "changed": True,
                    "revision": started["revision"],
                    "transition_id": started.get("transitionId"),
                    "check_interval_ms": record["checkIntervalMs"],
                    "heartbeat_interval_ms": record["heartbeatIntervalMs"],
                    "execution": execution,
                }
            )
        }

    return builtin_tool("TaskMonitorStart", handler)


def _task_monitor_stop_tool() -> ToolDefinition:
    async def handler(input: dict[str, Any], context: ToolContext) -> ToolResult:
        monitor_id = _require_string(input.get("monitor_id"), "monitor_id")
        monitor = await require_model_owned_monitor(context, monitor_id)
        committed = await commit_monitor_mutation(
            monitorId=monitor["id"],
            expectedSessionId=context["session"]["id"],
            mutate=lambda latest: _monitor_stop_mutation(latest),
        )
        transition = {
            **committed["value"],
            "record": committed["current"],
            "previousStatus": committed["previous"]["status"],
            "changed": committed["changed"],
        }
        record = committed["current"]
        if committed.get("transitionId"):
            context.get("outputJson", lambda *_: None)(
                "monitor.status_changed",
                {
                    "session_id": context["session"]["id"],
                    "monitor_id": record["id"],
                    "task_id": record.get("taskId"),
                    "status": record["status"],
                    "previous_status": transition["previousStatus"],
                    "changed": True,
                    **monitor_transition_event_fields(committed),
                },
            )
        return {
            "content": pretty_json(
                _without_none_values(
                    {
                        "monitor_id": record["id"],
                        "task_id": record.get("taskId"),
                        "status": record["status"],
                        "previous_status": transition["previousStatus"],
                        "changed": transition["changed"],
                        "revision": committed["revision"],
                        "transition_id": committed.get("transitionId"),
                        "note": _monitor_stop_note(transition),
                    }
                )
            )
        }

    return builtin_tool("TaskMonitorStop", handler)


def _monitor_stop_mutation(latest: dict[str, Any]) -> dict[str, Any]:
    transition = _stop_monitor_record(latest, now_iso())
    return {
        "record": transition["record"],
        "changed": transition["changed"],
        "transition": transition["changed"],
        "transitionLabel": (
            f"stop:{transition['previousStatus']}->{transition['record']['status']}"
        ),
        "value": transition,
    }


def _task_health_check_start_tool() -> ToolDefinition:
    async def handler(input: dict[str, Any], context: ToolContext) -> ToolResult:
        supervisor = context.get("daemon")
        if not supervisor:
            raise RuntimeError("Health checks require the local task supervisor daemon.")
        spec = await normalize_health_check_spec(
            {
                "title": input.get("title"),
                "mode": input.get("mode"),
                "cwd": context["session"]["cwd"],
                "command": input.get("command"),
                "url": input.get("url"),
                "intervalMs": input.get("interval_ms"),
                "timeoutMs": input.get("timeout_ms"),
                "failureThreshold": input.get("failure_threshold"),
                "backoffMultiplier": input.get("backoff_multiplier"),
                "maxIntervalMs": input.get("max_interval_ms"),
                "expectedStatusCodes": input.get("expected_status_codes"),
                "expectedBodyPolicy": input.get("expected_body_policy"),
                "expectedBodySubstring": input.get("expected_body_substring"),
                "outputLimitBytes": input.get("output_limit_bytes"),
                "bodyLimitBytes": input.get("body_limit_bytes"),
            },
            context["session"]["cwd"],
        )
        execution_policy = await authorize_health_check_spec(context, spec)
        started = await supervisor["startHealthCheck"](
            {
                "sessionId": context["session"]["id"],
                "spec": spec,
                "specDigest": health_check_spec_digest(spec),
                "executionPolicy": execution_policy,
            }
        )
        task_id = launch_task_id(started)
        _append_unique_metadata_list(context["session"], "taskIds", task_id)
        await maybe_await(context.get("persist_session", lambda: None)())
        context.get("outputJson", lambda *_: None)("task.registered", {"session_id": context["session"]["id"], "task_id": task_id, "kind": "health_check", "status": "running", "title": spec["title"], "owner": "daemon"})
        return {"content": pretty_json({"task_id": task_id, "status": "running", "kind": "health_check", "title": spec["title"]})}

    return builtin_tool("TaskHealthCheckStart", handler)


def _task_health_check_stop_tool() -> ToolDefinition:
    async def handler(input: dict[str, Any], context: ToolContext) -> ToolResult:
        supervisor = context.get("daemon")
        if not supervisor:
            raise RuntimeError("Health checks require the local task supervisor daemon.")
        task_id = _require_string(input.get("task_id"), "task_id")
        await require_model_owned_task(context, task_id, "health_stop")
        stopped = await supervisor["stopTask"](task_id)
        if not stopped:
            raise RuntimeError(f"Task not found or not stoppable: {task_id}")
        if stopped is True:
            stopped = {
                "previousStatus": "running",
                "status": "stopped",
                "changed": True,
                "note": "Task stopped by deliberate request.",
            }
        return {"content": pretty_json(_task_stop_content(task_id, stopped))}

    return builtin_tool("TaskHealthCheckStop", handler)


def _task_stop_content(task_id: str, result: dict[str, Any]) -> dict[str, Any]:
    return _without_none_values(
        {
            "task_id": task_id,
            "previous_status": result.get("previousStatus"),
            "status": result.get("status"),
            "changed": bool(result.get("changed")),
            "stopped": result.get("status") == "stopped",
            "note": result.get("note"),
            "transition_id": result.get("transitionId"),
        }
    )


def _terminal_task_status(record_status: Any, meta_status: Any) -> str | None:
    for status in (record_status, meta_status):
        normalized = str(status or "")
        if normalized in {"completed", "failed", "stopped", "terminated"}:
            return normalized
    return None


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
