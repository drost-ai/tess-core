from __future__ import annotations

import copy
from typing import Any, Callable

from ._utils import now_iso


def createInitialRuntimeState(session: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "session": {"activeSessionId": session.get("id"), "summary": summarizeSessionRecord(session), "lastActivatedAt": session.get("updatedAt")} if session else {"activeSessionId": None, "summary": None},
        "turn": {"status": "idle"},
        "provider": {"phase": "idle"},
        "compaction": {"status": "idle"},
        "daemon": {"phase": "offline"},
        "tasks": {"total": 0, "tasks": [], "activeCount": 0, "backgroundedCount": 0, "awaitingInputCount": 0, "failedCount": 0, "labels": []},
        "monitors": {"snapshot": {}, "activeMonitorIds": [], "recentAssistantUpdates": []},
        "bridge": {"attached": False},
        "collaboration": {},
        "status": None,
    }


def createRuntimeStateStore(initialState: dict[str, Any] | None = None) -> dict[str, Any]:
    state = copy.deepcopy(initialState or createInitialRuntimeState())
    subscribers: list[Callable[[dict[str, Any]], None]] = []

    def get_state():
        return copy.deepcopy(state)

    def dispatch(event):
        nonlocal state
        state = reduceRuntimeState(state, event)
        for subscriber in list(subscribers):
            subscriber(get_state())

    def subscribe(fn):
        subscribers.append(fn)
        return lambda: subscribers.remove(fn) if fn in subscribers else None

    return {"getState": get_state, "dispatch": dispatch, "subscribe": subscribe}


def reduceRuntimeState(state: dict[str, Any], event: dict[str, Any]) -> dict[str, Any]:
    next_state = copy.deepcopy(state)
    typ = event.get("type") or event.get("event")
    data = event.get("data") or {}
    timestamp = event.get("timestamp") or event.get("createdAt") or now_iso()
    if typ == "session.activated":
        summary = data.get("session") or data
        next_state["session"] = {"activeSessionId": summary.get("id") or data.get("session_id"), "summary": summarizeSessionRecord(summary), "lastActivatedAt": timestamp}
        next_state["turn"] = {**next_state.get("turn", {}), "status": "idle", "model": summary.get("model"), "liveStatus": None}
        next_state["bridge"] = {**next_state.get("bridge", {}), "attached": bool(summary.get("bridgeId")), "bridgeId": summary.get("bridgeId"), "remoteSessionId": summary.get("remoteSessionId")}
    elif typ == "turn.started":
        next_state["turn"] = {"status": "running", "prompt": data.get("prompt"), "agentName": data.get("agentName"), "model": data.get("model") or next_state.get("turn", {}).get("model"), "startedAt": timestamp, "lastOutcome": next_state.get("turn", {}).get("lastOutcome")}
        next_state["provider"] = {**next_state.get("provider", {}), "phase": "idle"}
    elif typ in ("turn.completed", "turn.failed", "turn.cancelled"):
        status = typ.split(".")[1]
        next_state["turn"] = {**next_state.get("turn", {}), "status": status, "prompt": None, "agentName": None, "liveStatus": None, "lastOutcome": {"kind": status, "message": data.get("message") or status, "stopReason": data.get("stopReason"), "model": data.get("model"), "updatedAt": timestamp}}
        if status != "failed":
            next_state["provider"] = {**next_state.get("provider", {}), "phase": "idle"}
    elif typ == "turn.cancelling":
        next_state["turn"] = {**next_state.get("turn", {}), "status": "cancelling"}
    elif typ == "status.updated":
        next_state["status"] = data
        next_state["turn"] = {**next_state.get("turn", {}), "liveStatus": {"message": data.get("message"), "detail": data.get("detail"), "source": "model", "updatedAt": timestamp}}
    elif typ == "status.cleared":
        next_state["status"] = None
        next_state["turn"] = {**next_state.get("turn", {}), "liveStatus": None}
    elif typ == "provider.input_tokens":
        next_state["provider"] = {**next_state.get("provider", {}), "provider": data.get("provider"), "model": data.get("model"), "inputTokens": data.get("input_tokens") or data.get("inputTokens"), "inputTokenSource": data.get("source"), "baseInputTokens": data.get("baseInputTokens"), "safetyMarginTokens": data.get("safetyMarginTokens")}
    elif typ == "provider.retrying":
        next_state["provider"] = {**next_state.get("provider", {}), "phase": "retrying", "nextAttempt": data.get("nextAttempt"), "delayMs": data.get("delayMs")}
    elif typ == "provider.recovered":
        next_state["provider"] = {**next_state.get("provider", {}), "phase": "recovered", "lastRecoveryAt": timestamp}
    elif typ == "provider.failed":
        next_state["provider"] = {**next_state.get("provider", {}), "phase": "failed", "provider": data.get("provider"), "lastFailure": {**data, "occurredAt": timestamp}}
    elif typ == "tool.started":
        if next_state.get("turn", {}).get("liveStatus", {}).get("source") != "model":
            next_state["turn"] = {**next_state.get("turn", {}), "liveStatus": {"message": f"Running {data.get('name') or 'tool'}...", "detail": data.get("summary"), "source": "runtime", "updatedAt": timestamp}}
    elif typ == "tool.completed":
        if next_state.get("turn", {}).get("liveStatus", {}).get("source") == "runtime":
            next_state["turn"] = {**next_state.get("turn", {}), "liveStatus": None}
    elif typ == "compaction.started":
        next_state["compaction"] = {**next_state.get("compaction", {}), "status": "running", "trigger": data.get("trigger"), "currentTokenEstimate": data.get("currentTokenEstimate"), "updatedAt": timestamp}
    elif typ == "context_relief.selected":
        next_state["compaction"] = {**next_state.get("compaction", {}), "selectedMode": data.get("mode"), "pressureLevel": data.get("pressureLevel"), "reactiveAttempt": data.get("reactiveAttempt"), "plannedModes": data.get("plannedModes"), "updatedAt": timestamp}
    elif typ == "context_relief.applied":
        next_state["compaction"] = {**next_state.get("compaction", {}), "selectedMode": data.get("mode"), "appliedModes": data.get("modes") or [data.get("mode")], "pressureLevel": data.get("pressureLevel"), "lastDetail": data.get("detail"), "lastReinjectionSummary": data.get("reinjectionSummary"), "updatedAt": timestamp}
    elif typ in ("compaction.skipped", "compaction.completed", "compaction.failed"):
        status = typ.split(".")[1]
        next_state["compaction"] = {**next_state.get("compaction", {}), "status": status, **data, "updatedAt": timestamp}
    elif typ == "daemon.snapshot":
        next_state["daemon"] = {"phase": data.get("phase") or "running", "epoch": data.get("epoch"), "pid": data.get("pid"), "socketPath": data.get("socketPath"), "supervisorId": data.get("supervisorId"), "attachedClients": data.get("attachedClients"), "watchedSessions": data.get("watchedSessions"), "activeTasks": data.get("activeTasks"), "updatedAt": data.get("updatedAt") or timestamp}
    elif typ == "daemon.recovering":
        next_state["daemon"] = {**next_state.get("daemon", {}), "phase": "recovering", "epoch": data.get("epoch") or next_state.get("daemon", {}).get("epoch"), "updatedAt": timestamp}
    elif typ == "daemon.recovered":
        next_state["daemon"] = {**next_state.get("daemon", {}), "phase": "running", "epoch": data.get("epoch") or next_state.get("daemon", {}).get("epoch"), "updatedAt": timestamp}
    elif typ == "task.snapshot":
        tasks = data.get("tasks") or []
        next_state["tasks"] = {"total": data.get("total", len(tasks)), "tasks": tasks, "activeCount": data.get("active", _count_tasks(tasks, "running")), "backgroundedCount": data.get("backgrounded", _count_tasks(tasks, "running")), "awaitingInputCount": data.get("awaitingInput", _count_tasks(tasks, "awaiting_input")), "failedCount": data.get("failed", _count_tasks(tasks, "failed")), "labels": data.get("labels") or [task.get("title") for task in tasks if task.get("title")], "lastUpdatedAt": timestamp}
    elif typ in ("task.registered", "task.updated", "task.completed", "task.failed", "task.restored", "task.output_delta"):
        task_id = data.get("task_id") or data.get("taskId") or data.get("id")
        if task_id:
            status = "completed" if typ == "task.completed" else "failed" if typ == "task.failed" else data.get("status")
            next_state["tasks"] = _upsert_task(next_state.get("tasks", {}), {"id": task_id, "sessionId": data.get("session_id") or data.get("sessionId"), "kind": data.get("kind"), "status": status, "title": data.get("title") or task_id, "summary": data.get("summary"), "outputCursor": data.get("output_cursor") or data.get("outputCursor"), "updatedAt": timestamp})
    elif typ == "monitor.snapshot":
        next_state["monitors"] = {**next_state.get("monitors", {}), "snapshot": data, "activeMonitorIds": data.get("activeMonitorIds") or data.get("active_monitor_ids") or []}
    elif typ == "monitor.created":
        monitor_id = data.get("monitor_id") or data.get("monitorId")
        if monitor_id:
            active = list(dict.fromkeys([*next_state.get("monitors", {}).get("activeMonitorIds", []), monitor_id]))
            next_state["monitors"] = {**next_state.get("monitors", {}), "activeMonitorIds": active}
    elif typ == "monitor.assistant_update":
        updates = [{"monitorId": data.get("monitor_id") or data.get("monitorId"), "taskId": data.get("task_id") or data.get("taskId"), "status": data.get("status"), "text": data.get("text") or data.get("message") or "", "timestamp": timestamp}, *next_state.get("monitors", {}).get("recentAssistantUpdates", [])]
        next_state["monitors"] = {**next_state.get("monitors", {}), "recentAssistantUpdates": updates[:10]}
    elif typ == "bridge.message":
        next_state["bridge"] = {**next_state.get("bridge", {}), "attached": True, "bridgeId": data.get("bridgeId") or data.get("bridge_id"), "remoteSessionId": data.get("remoteSessionId") or data.get("remote_session_id"), "lastMessageText": data.get("text") or data.get("message"), "lastMessageAt": timestamp}
    elif typ in ("bridge.event", "remote.event"):
        next_state["bridge"] = {**next_state.get("bridge", {}), "lastEventName": data.get("event") or data.get("name") or typ, "lastEventAt": timestamp}
    return next_state


def summarizeSessionRecord(session: dict[str, Any]) -> dict[str, Any]:
    return {"id": session.get("id"), "cwd": session.get("cwd"), "name": session.get("name"), "provider": session.get("provider"), "model": session.get("model"), "permissionMode": session.get("permissionMode"), "updatedAt": session.get("updatedAt"), "workspaceName": session.get("metadata", {}).get("workspaceName"), "gitRoot": session.get("metadata", {}).get("gitRoot"), "gitBranch": session.get("metadata", {}).get("gitBranch"), "remoteSessionId": session.get("metadata", {}).get("remoteSessionId"), "bridgeId": session.get("metadata", {}).get("bridgeId")}


def deriveStoredTurnOutcome(session: dict[str, Any]) -> dict[str, Any] | None:
    messages = session.get("messages", [])
    if not messages:
        return None
    last = messages[-1]
    return {"status": "completed", "lastRole": last.get("role"), "updatedAt": session.get("updatedAt")}


def _count_tasks(tasks: list[dict[str, Any]], status: str) -> int:
    return sum(1 for task in tasks if task.get("status") == status)


def _upsert_task(task_slice: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    tasks = [copy.deepcopy(task) for task in task_slice.get("tasks", [])]
    index = next((idx for idx, task in enumerate(tasks) if task.get("id") == patch.get("id")), -1)
    if index >= 0:
        tasks[index] = {**tasks[index], **{key: value for key, value in patch.items() if value is not None}}
    else:
        tasks.append({key: value for key, value in patch.items() if value is not None})
    active = [task for task in tasks if task.get("status") in ("running", "queued", "awaiting_input")]
    failed = [task for task in tasks if task.get("status") in ("failed", "stopped")]
    return {**task_slice, "total": len(tasks), "tasks": tasks, "activeCount": len(active), "backgroundedCount": len(active), "awaitingInputCount": _count_tasks(tasks, "awaiting_input"), "failedCount": len(failed), "labels": [task.get("title") for task in tasks if task.get("title")], "lastUpdatedAt": patch.get("updatedAt")}
