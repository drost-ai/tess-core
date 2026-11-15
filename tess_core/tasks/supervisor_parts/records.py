from __future__ import annotations

from ..._utils import now_iso
from ...storage import load_session, read_task_meta, task_output_path
from ..communication import read_task_questions
from .mappers import (
    _map_child_agent_status_to_task_status,
    _task_record_event_payload,
    _map_monitor_status,
    _map_remote_status,
    _map_status,
)
from .summaries import (
    _build_background_command_result_summary,
    _build_detached_main_session_result_summary,
    _describe_background_command_detail_status,
    _describe_child_agent_detail_status,
    _describe_detached_main_session_detail_status,
    _summarize_background_command_task,
    _summarize_child_agent_task,
    _summarize_command,
    _summarize_detached_main_session_task,
    _summarize_health_check_task,
    _summarize_latest_message,
    _summarize_monitor,
    _summarize_recovered_subagent,
)
from .utils import (
    _first_text,
    _latest_timestamp,
    _output_size,
    _read_output_preview,
)
from pathlib import Path
from typing import Any
from ...contracts.records import task_meta_id


def _health_check_status(meta: dict[str, Any]) -> str:
    status = meta.get("status")
    if status == "active":
        return "running"
    if status == "unhealthy":
        return "degraded"
    if status == "stopped":
        return "stopped"
    return "failed"


def _process_task_status(meta: dict[str, Any]) -> str:
    status = _map_status(meta.get("status"))
    if status == "running" and not meta.get("finishedAt") and not meta.get("stoppedAt"):
        return "backgrounded"
    return status


# Descriptor table for the task kinds the supervisor projects. Each daemon
# task-meta kind carries its record family, capability flags, and the four
# kind-dependent projections (status/summary/detailStatus/resultSummary,
# each called with (meta, status, ctx)). The monitor/remote entries describe
# projection-only pseudo-records: their ids carry a "monitor:"/"remote:"
# prefix and never correspond to a daemon task meta.
TASK_KINDS: dict[str, dict[str, Any]] = {
    "background_command": {
        "family": "shell",
        "foregroundable": True,
        "retrieveOnly": True,
        "status": _process_task_status,
        "summary": lambda meta, status, ctx: meta.get("error") or _summarize_background_command_task(meta, status, ctx["outputCursor"], ctx["outputPreview"]),
        "detailStatus": lambda meta, status, ctx: _describe_background_command_detail_status(meta, status, ctx["outputPreview"]),
        "resultSummary": lambda meta, status, ctx: _build_background_command_result_summary(meta, ctx["outputPreview"]),
    },
    "main_session": {
        "family": "detached_session",
        "foregroundable": True,
        "retrieveOnly": False,
        "status": _process_task_status,
        "summary": lambda meta, status, ctx: _summarize_detached_main_session_task(status, ctx["latestAssistant"], ctx["latestUser"], ctx["outputPreview"]),
        "detailStatus": lambda meta, status, ctx: _describe_detached_main_session_detail_status(status, ctx["latestAssistant"]),
        "resultSummary": lambda meta, status, ctx: _build_detached_main_session_result_summary(status, ctx["latestAssistant"], ctx["latestUser"]),
    },
    # Resident-subagent lifecycle: turn-complete is NOT terminal. Idle
    # residents sit at awaiting_input (waitingReason resident_idle), turns
    # run at running, and terminal states come only from stop/kill/crash
    # (stopped/failed/daemon_lost). "completed" for a subagent means it was
    # terminated after finishing — a parent-initiated end, never something
    # a resident reaches by itself; only the resident:false one-shot
    # contract finalizes completed from its own stop reason.
    "subagent": {
        "family": "subagent",
        "foregroundable": True,
        "retrieveOnly": False,
        "status": _process_task_status,
        "summary": lambda meta, status, ctx: meta.get("summary") or meta.get("error") or meta.get("detailStatus") or status,
        "detailStatus": lambda meta, status, ctx: str(meta.get("detailStatus") or meta.get("completionReason") or meta.get("waitingReason") or status),
        "resultSummary": lambda meta, status, ctx: _first_text(meta.get("resultSummary"), meta.get("summary"), meta.get("error"), meta.get("waitingReason"), meta.get("stopReason")),
    },
    "health_check": {
        "family": "health_check",
        "foregroundable": False,
        "retrieveOnly": True,
        "status": _health_check_status,
        "summary": lambda meta, status, ctx: _summarize_health_check_task(meta),
        "detailStatus": lambda meta, status, ctx: "unhealthy" if meta.get("status") == "unhealthy" else str(meta.get("status") or status),
        "resultSummary": lambda meta, status, ctx: _first_text(meta.get("lastError"), meta.get("lastResponseSnippet")),
    },
    "monitor": {"family": "monitor", "foregroundable": False, "retrieveOnly": True, "projection": True},
    "remote": {"family": "remote", "foregroundable": True, "retrieveOnly": False, "projection": True},
}

# The kinds a daemon task meta may declare (everything except the
# projection-only pseudo-kinds).
DAEMON_TASK_KINDS = frozenset(name for name, spec in TASK_KINDS.items() if not spec.get("projection"))


def _build_recovered_subagent_task_record(record: dict[str, Any], child: dict[str, Any]) -> dict[str, Any]:
    status = _map_child_agent_status_to_task_status(child.get("status"))
    updated_at = child.get("lastUpdatedAt") or child.get("latestResultAt") or child.get("completedAt") or now_iso()
    result_summary = child.get("resultSummary") or child.get("lastSummary") or child.get("error") or child.get("waitingReason") or child.get("stopReason")
    metadata = dict(record.get("metadata") or {})
    metadata.update(
        {
            "agent_id": child.get("agentId"),
            "child_session_id": child.get("sessionId"),
            "waiting_reason": child.get("waitingReason"),
            "completion_reason": child.get("completionReason"),
            "recovery_state": "completed_while_detached",
            "retryable": child.get("retryable"),
            "stoppable": child.get("stoppable"),
        }
    )
    return {
        **record,
        "status": status,
        "title": child.get("name") or child.get("agentType") or child.get("agentId"),
        "summary": _summarize_recovered_subagent(child, result_summary),
        "updatedAt": updated_at,
        "completedAt": child.get("completedAt") or child.get("latestResultAt") or updated_at if status in {"completed", "failed", "stopped"} else None,
        "parentTaskId": child.get("parentTaskId") or record.get("parentTaskId"),
        "relatedChildSessionIds": [child.get("sessionId")] if child.get("sessionId") else [],
        "owner": child.get("owner") or record.get("owner"),
        "taskFamily": "subagent",
        "detailStatus": child.get("completionReason") or child.get("waitingReason") or child.get("status"),
        "attachableSessionId": child.get("attachableSessionId") or child.get("sessionId"),
        "retrieveOnly": False,
        "resultSummary": result_summary,
        "metadata": metadata,
    }


def _build_monitor_task_record(session: dict[str, Any], monitor: dict[str, Any]) -> dict[str, Any]:
    task_id = monitor.get("taskId")
    output_file = task_output_path(task_id) if isinstance(task_id, str) else None
    output_cursor = Path(output_file).stat().st_size if output_file and Path(output_file).exists() else 0
    return {
        "id": f"monitor:{monitor.get('id')}",
        "sessionId": session.get("id"),
        "sourceId": monitor.get("id"),
        "kind": "monitor",
        "status": _map_monitor_status(monitor.get("status")),
        "title": f"Monitor {monitor.get('goal')}",
        "summary": monitor.get("lastAssistantUpdateSummary") or _summarize_monitor(monitor),
        "createdAt": monitor.get("createdAt"),
        "updatedAt": monitor.get("updatedAt") or monitor.get("createdAt"),
        "startedAt": monitor.get("createdAt"),
        "completedAt": monitor.get("completedAt") or monitor.get("stoppedAt"),
        "foregroundable": TASK_KINDS["monitor"]["foregroundable"],
        "restorable": True,
        "outputCursor": output_cursor,
        "relatedMonitorIds": [monitor.get("id")] if monitor.get("id") else [],
        "taskFamily": TASK_KINDS["monitor"]["family"],
        "detailStatus": monitor.get("status"),
        "retrieveOnly": TASK_KINDS["monitor"]["retrieveOnly"],
        "resultSummary": monitor.get("lastAssistantUpdateSummary"),
        "metadata": {
            "task_id": monitor.get("taskId"),
            "goal": monitor.get("goal"),
            "last_task_status": monitor.get("lastObservedTaskStatus"),
            "muted": monitor.get("muted"),
        },
    }


async def _build_child_agent_task_record(session: dict[str, Any], child: dict[str, Any]) -> dict[str, Any]:
    task_id = child.get("taskId")
    task_meta = await read_task_meta(task_id) if isinstance(task_id, str) and task_id else None
    questions = await read_task_questions(task_id) if isinstance(task_id, str) and task_id else []
    pending_questions = [question for question in questions if question.get("status") == "pending"]
    child_session = await load_session(child.get("sessionId")) if isinstance(child.get("sessionId"), str) else None
    latest_assistant = _summarize_latest_message(child_session or {}, "assistant")
    latest_user = _summarize_latest_message(child_session or {}, "user")
    result_summary = child.get("resultSummary") or child.get("lastSummary") or latest_assistant or child.get("error") or child.get("waitingReason")
    owner = child.get("owner") or ((task_meta or {}).get("owner") if (task_meta or {}).get("owner") in {"daemon", "client"} else None)
    task_status = _map_child_agent_status_to_task_status(child.get("status"))
    if pending_questions:
        task_status = "awaiting_input"
    runtime_contract = child.get("runtimeContract") or (task_meta or {}).get("runtimeContract")
    return {
        "id": task_id or child.get("agentId"),
        "sessionId": session.get("id"),
        "sourceId": child.get("agentId"),
        "kind": "subagent",
        "status": task_status,
        "title": child.get("name") or child.get("agentType") or child.get("agentId"),
        "summary": _summarize_child_agent_task(child, latest_assistant, latest_user, result_summary),
        "createdAt": child.get("startedAt"),
        "updatedAt": child.get("lastUpdatedAt") or child.get("completedAt") or child.get("startedAt"),
        "startedAt": child.get("startedAt"),
        "completedAt": child.get("completedAt"),
        "foregroundable": True,
        "restorable": True,
        "outputCursor": _output_size(task_output_path(task_id)) if isinstance(task_id, str) else None,
        "outputFile": task_output_path(task_id) if isinstance(task_id, str) else None,
        "parentTaskId": child.get("parentTaskId"),
        "relatedChildSessionIds": [child.get("sessionId")] if child.get("sessionId") else [],
        "owner": owner,
        "foregroundMode": "background" if child.get("runInBackground") else "foreground",
        "taskFamily": "subagent",
        "detailStatus": "child_question" if pending_questions else _describe_child_agent_detail_status(child),
        "attachableSessionId": child.get("attachableSessionId") or child.get("sessionId"),
        "retrieveOnly": False,
        "resultSummary": result_summary,
        "metadata": {
            "agent_id": child.get("agentId"),
            "child_session_id": child.get("sessionId"),
            "task_id": task_id,
            "worktree_path": child.get("worktreePath"),
            "pid": child.get("pid"),
            "waiting_reason": child.get("waitingReason"),
            "completion_reason": child.get("completionReason"),
            "retryable": child.get("retryable"),
            "stoppable": child.get("stoppable"),
            "owner": owner,
            "recovery_state": child.get("recoveryState"),
            "daemon_status": (task_meta or {}).get("status") if isinstance((task_meta or {}).get("status"), str) else None,
            "runtime_contract": runtime_contract,
            "concurrency_group": (runtime_contract or {}).get("concurrencyGroup") if isinstance(runtime_contract, dict) else None,
            "pending_questions": len(pending_questions),
            "pending_question_ids": [question.get("questionId") for question in pending_questions],
            "structured_result_status": child.get("structuredResultStatus") or (task_meta or {}).get("structuredResultStatus"),
            "structured_result_path": child.get("structuredResultPath") or (task_meta or {}).get("structuredResultPath"),
            "structured_result_errors": child.get("structuredResultErrors") or (task_meta or {}).get("structuredResultErrors"),
        },
    }


def _build_remote_task_record(session: dict[str, Any], remote: dict[str, Any], bridge_status: Any = None) -> dict[str, Any]:
    return {
        "id": f"remote:{remote.get('id')}",
        "sessionId": session.get("id"),
        "sourceId": remote.get("id"),
        "kind": "remote",
        "status": _map_remote_status(remote.get("status")),
        "title": remote.get("title"),
        "summary": remote.get("lastEventSummary"),
        "createdAt": remote.get("createdAt"),
        "updatedAt": remote.get("updatedAt"),
        "startedAt": remote.get("createdAt"),
        "completedAt": remote.get("updatedAt") if remote.get("status") in {"completed", "failed", "closed"} else None,
        "foregroundable": TASK_KINDS["remote"]["foregroundable"],
        "restorable": True,
        "foregroundMode": "foreground" if remote.get("status") == "active" else "background",
        "taskFamily": TASK_KINDS["remote"]["family"],
        "detailStatus": remote.get("status"),
        "attachableSessionId": remote.get("foregroundSessionId") or remote.get("sessionId"),
        "retrieveOnly": TASK_KINDS["remote"]["retrieveOnly"],
        "resultSummary": remote.get("lastEventSummary"),
        "metadata": {
            "remote_session_id": remote.get("id"),
            "bridge_id": remote.get("bridgeId"),
            "bridge_status": bridge_status,
            "foreground_session_id": remote.get("foregroundSessionId"),
        },
    }


def _build_task_record(meta: dict[str, Any], summary: str | None, session: dict[str, Any] | None = None) -> dict[str, Any]:
    now = now_iso()
    task_id = task_meta_id(meta)
    output_file = meta.get("outputFile") or task_output_path(task_id)
    output_cursor = Path(output_file).stat().st_size if output_file and Path(output_file).exists() else 0
    output_preview = _read_output_preview(output_file)
    task_type = meta.get("type") or "background_command"
    kind = task_type if task_type in TASK_KINDS and not TASK_KINDS[task_type].get("projection") else "background_command"
    spec = TASK_KINDS[kind]
    status = spec["status"](meta)
    is_subagent = kind == "subagent"
    is_main_session = kind == "main_session"
    is_health_check = kind == "health_check"
    session_data = session or {}
    latest_assistant = _summarize_latest_message(session_data, "assistant") if is_main_session else None
    latest_user = _summarize_latest_message(session_data, "user") if is_main_session else None
    ctx = {
        "outputCursor": output_cursor,
        "outputPreview": output_preview,
        "latestAssistant": latest_assistant,
        "latestUser": latest_user,
    }
    if summary is None:
        summary = spec["summary"](meta, status, ctx)
    metadata = dict(meta.get("metadata") or {})
    metadata.update(
        {
            "pid": meta.get("pid"),
            "exitCode": meta.get("exitCode"),
            "exit_code": meta.get("exitCode"),
            "signal": meta.get("signal"),
            "lastResult": meta.get("lastResult"),
            "process_state": meta.get("processState") or metadata.get("process_state"),
            "process_reconciled_at": meta.get("processReconciledAt") or metadata.get("process_reconciled_at"),
            "terminal_transition_id": meta.get("terminalTransitionId")
            or metadata.get("terminal_transition_id"),
            "terminal_event_owner": meta.get("terminalEventOwner")
            or metadata.get("terminal_event_owner"),
        }
    )
    if not is_health_check:
        metadata.update(
            {
                "runner_pid": meta.get("runnerPid"),
                "detached_type": "main_session" if is_main_session else "background_command" if not is_subagent else None,
                "output_preview": output_preview,
                "recovery_state": "recovered_after_daemon_restart" if meta.get("status") == "reconciling" else metadata.get("recovery_state"),
            }
        )
    if is_health_check:
        metadata.update(
            {
                "mode": meta.get("mode"),
                "interval_ms": meta.get("intervalMs"),
                "current_interval_ms": meta.get("currentIntervalMs") or meta.get("intervalMs"),
                "timeout_ms": meta.get("timeoutMs"),
                "backoff_multiplier": meta.get("backoffMultiplier"),
                "max_interval_ms": meta.get("maxIntervalMs"),
                "failure_threshold": meta.get("failureThreshold"),
                "consecutive_failures": meta.get("consecutiveFailures") or 0,
                "last_status_code": meta.get("lastStatusCode"),
                "last_exit_code": meta.get("lastExitCode"),
                "last_signal": meta.get("lastSignal"),
                "last_timed_out": meta.get("lastTimedOut"),
                "last_terminated": meta.get("lastTerminated"),
                "expected_body_substring": meta.get("expectedBodySubstring"),
            }
        )
    if is_main_session:
        metadata.update(
            {
                "latest_assistant_summary": latest_assistant,
                "latest_user_prompt": latest_user,
                "session_updated_at": session_data.get("updatedAt"),
            }
        )
    if is_subagent:
        runtime_contract = meta.get("runtimeContract") or metadata.get("runtime_contract")
        metadata.update(
            {
                "agent_id": meta.get("agent_id"),
                "child_session_id": meta.get("session_id") or meta.get("sessionId"),
                "parent_session_id": meta.get("parent_session_id"),
                "parent_task_id": meta.get("parent_task_id"),
                "runtime_contract": runtime_contract,
                "concurrency_group": meta.get("concurrencyGroup") or metadata.get("concurrency_group"),
                "structured_result_status": meta.get("structuredResultStatus"),
                "structured_result_path": meta.get("structuredResultPath"),
                "structured_result_errors": meta.get("structuredResultErrors"),
            }
        )
    session_id = meta.get("parent_session_id") if is_subagent else meta.get("sessionId") or session_data.get("id")
    updated_at = meta.get("updatedAt") or meta.get("finishedAt") or meta.get("startedAt") or now
    if is_main_session:
        updated_at = _latest_timestamp(session_data.get("updatedAt"), updated_at)
    return {
        "id": task_id,
        "sessionId": session_id,
        "sourceId": meta.get("agent_id") if is_subagent else task_id,
        "kind": kind,
        "status": status,
        "title": _first_text(meta.get("title"), meta.get("description"), _summarize_command(meta.get("command")), task_id),
        "summary": summary or meta.get("error") or meta.get("detailStatus") or status,
        "createdAt": meta.get("createdAt") or meta.get("startedAt") or now,
        "updatedAt": updated_at,
        "startedAt": meta.get("startedAt") or meta.get("createdAt"),
        "completedAt": meta.get("updatedAt") if is_health_check and meta.get("status") == "stopped" else meta.get("finishedAt") or meta.get("stoppedAt"),
        "foregroundable": spec["foregroundable"],
        "restorable": True,
        "cwd": meta.get("cwd"),
        "command": meta.get("command") or meta.get("url") if is_health_check else meta.get("command"),
        "outputFile": output_file,
        "outputCursor": output_cursor,
        "relatedMonitorIds": meta.get("relatedMonitorIds") or [],
        "foregroundMode": "background",
        "taskFamily": spec["family"],
        "detailStatus": spec["detailStatus"](meta, status, ctx),
        "retrieveOnly": spec["retrieveOnly"],
        "attachableSessionId": (meta.get("attachableSessionId") or session_data.get("id")) if is_main_session else meta.get("attachableSessionId") if is_subagent else None,
        "parentTaskId": meta.get("parent_task_id") if is_subagent else None,
        "relatedChildSessionIds": [meta.get("session_id") or meta.get("sessionId")] if is_subagent and (meta.get("session_id") or meta.get("sessionId")) else [],
        "owner": meta.get("owner") or "daemon",
        "supervisorId": meta.get("supervisorId"),
        "processIdentity": meta.get("processIdentity"),
        "launchReservation": meta.get("launchReservation"),
        "resultSummary": spec["resultSummary"](meta, status, ctx),
        "metadata": metadata,
    }


def _task_event_payload(meta: dict[str, Any]) -> dict[str, Any]:
    record = _build_task_record(meta, None)
    return _task_record_event_payload(record)
