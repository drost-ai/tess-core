from __future__ import annotations

import copy
from typing import Any, Callable

from ._utils import now_iso


ACTIVE_TASK_STATUSES = {"queued", "running", "backgrounded", "awaiting_input", "reconciling", "degraded"}


def createInitialRuntimeState(session: dict[str, Any] | None = None) -> dict[str, Any]:
    metadata = session.get("metadata", {}) if session else {}
    return {
        "session": (
            {
                "activeSessionId": session.get("id"),
                "summary": summarizeSessionRecord(session),
                "lastActivatedAt": session.get("updatedAt"),
            }
            if session
            else {}
        ),
        "turn": {"status": "idle", "lastOutcome": deriveStoredTurnOutcome(session) if session else None},
        "provider": {"phase": "idle"},
        "compaction": {"status": "idle"},
        "daemon": {"phase": "offline"},
        "tasks": {"total": 0, "tasks": [], "activeCount": 0, "backgroundedCount": 0, "awaitingInputCount": 0, "failedCount": 0, "labels": []},
        "monitors": {"snapshot": {"total": 0, "active": 0, "failed": 0, "stopped": 0, "muted": 0, "labels": []}, "activeMonitorIds": [], "recentAssistantUpdates": []},
        "bridge": {"attached": bool(metadata.get("bridgeId")), "bridgeId": metadata.get("bridgeId"), "remoteSessionId": metadata.get("remoteSessionId")},
        "collaboration": {
            "workers": [],
            "activeCount": 0,
            "awaitingInputCount": 0,
            "backgroundCount": 0,
            "remoteStatus": "active" if metadata.get("remoteControlEnabled") else None,
            "remoteTitle": metadata.get("remoteSessionTitle"),
            "labels": [],
            "recentAssistantUpdates": [],
        },
    }


def createRuntimeStateStore(initialState: dict[str, Any] | None = None) -> dict[str, Any]:
    state = copy.deepcopy(initialState or createInitialRuntimeState())
    subscribers: list[Callable[[dict[str, Any]], None]] = []

    def get_state():
        return copy.deepcopy(state)

    def dispatch(event):
        nonlocal state
        next_state = reduceRuntimeState(state, event)
        if next_state == state:
            return
        state = next_state
        for subscriber in list(subscribers):
            subscriber()

    def subscribe(fn):
        subscribers.append(fn)
        return lambda: subscribers.remove(fn) if fn in subscribers else None

    return {"getState": get_state, "dispatch": dispatch, "subscribe": subscribe, "applyEvent": dispatch}


def reduceRuntimeState(state: dict[str, Any], event: dict[str, Any]) -> dict[str, Any]:
    next_state = copy.deepcopy(state)
    typ = event.get("kind") or event.get("type") or event.get("event")
    data = event.get("payload") if isinstance(event.get("payload"), dict) else event.get("data") or {}
    timestamp = event.get("timestamp") or event.get("createdAt") or now_iso()

    if typ == "session.activated":
        summary = data.get("session") or data
        session_summary = summarizeSessionRecord(summary)
        session_id = event.get("sessionId") or session_summary.get("id") or _field(data, "sessionId", "session_id") or ""
        session_summary["id"] = session_id
        remote_session_id = _field(summary, "remoteSessionId", "remote_session_id")
        workspace_name = _field(summary, "workspaceName", "workspace_name")
        next_state["session"] = {"activeSessionId": session_id, "summary": session_summary, "lastActivatedAt": timestamp}
        next_state["turn"] = {
            **next_state.get("turn", {}),
            "status": "idle",
            "prompt": None,
            "agentName": None,
            "model": session_summary.get("model"),
            "startedAt": None,
            "liveStatus": None,
        }
        next_state["bridge"] = {
            **next_state.get("bridge", {}),
            "attached": bool(session_summary.get("bridgeId")),
            "bridgeId": session_summary.get("bridgeId"),
            "remoteSessionId": remote_session_id,
        }
        collaboration = copy.deepcopy(next_state.get("collaboration") or {})
        collaboration["remoteTitle"] = collaboration.get("remoteTitle") or _session_title_from_summary(workspace_name, remote_session_id)
        next_state["collaboration"] = collaboration
    elif typ == "turn.started":
        next_state["turn"] = {
            "status": "running",
            "prompt": _field(data, "prompt"),
            "agentName": _field(data, "agentName", "agent_name"),
            "model": _field(data, "model") or next_state.get("turn", {}).get("model") or (next_state.get("session", {}).get("summary") or {}).get("model"),
            "startedAt": timestamp,
            "liveStatus": None,
            "lastOutcome": next_state.get("turn", {}).get("lastOutcome"),
        }
        next_state["provider"] = {**next_state.get("provider", {}), "phase": "idle"}
        next_state["compaction"] = {**next_state.get("compaction", {}), "status": "idle"}
    elif typ == "turn.cancelling":
        next_state["turn"] = {**next_state.get("turn", {}), "status": "cancelling"}
    elif typ == "turn.completed":
        next_state["turn"] = {
            **next_state.get("turn", {}),
            "status": "completed",
            "prompt": None,
            "agentName": None,
            "liveStatus": None,
            "lastOutcome": {
                "kind": "completed",
                "message": _field(data, "message") or _default_outcome_message(_field(data, "stopReason", "stop_reason")),
                "stopReason": _field(data, "stopReason", "stop_reason"),
                "model": _field(data, "model") or next_state.get("turn", {}).get("model"),
                "updatedAt": timestamp,
            },
        }
        next_state["provider"] = {**next_state.get("provider", {}), "phase": "idle"}
    elif typ == "turn.failed":
        next_state["turn"] = {
            **next_state.get("turn", {}),
            "status": "failed",
            "liveStatus": None,
            "lastOutcome": {"kind": "failed", "message": "Turn failed. Review the error above.", "updatedAt": timestamp},
        }
    elif typ == "turn.cancelled":
        next_state["turn"] = {
            **next_state.get("turn", {}),
            "status": "cancelled",
            "liveStatus": None,
            "lastOutcome": {
                "kind": "cancelled",
                "message": "Turn cancelled.",
                "stopReason": _field(data, "stopReason", "stop_reason") or "cancelled",
                "model": _field(data, "model") or next_state.get("turn", {}).get("model"),
                "updatedAt": timestamp,
            },
        }
        next_state["provider"] = {**next_state.get("provider", {}), "phase": "idle"}
    elif typ == "status.updated":
        next_state.pop("status", None)
        next_state["turn"] = {**next_state.get("turn", {}), "liveStatus": {"message": _field(data, "message"), "detail": _field(data, "detail"), "source": "model", "updatedAt": timestamp}}
    elif typ == "status.cleared":
        next_state.pop("status", None)
        next_state["turn"] = {**next_state.get("turn", {}), "liveStatus": None}
    elif typ == "provider.retrying":
        next_attempt = _field(data, "nextAttempt", "next_attempt")
        provider_label = _field(data, "providerLabel", "provider_label") or "Provider"
        next_state["provider"] = {**next_state.get("provider", {}), "phase": "retrying", "nextAttempt": next_attempt, "delayMs": _field(data, "delayMs", "delay_ms")}
        suffix = f" (attempt {next_attempt})" if next_attempt else ""
        next_state["turn"] = {
            **next_state.get("turn", {}),
            "liveStatus": {
                "message": f"{provider_label} failed transiently. Retrying{suffix}...",
                "detail": f"retry in {_field(data, 'delayMs', 'delay_ms')} ms" if _field(data, "delayMs", "delay_ms") else None,
                "source": "runtime",
                "updatedAt": timestamp,
            },
        }
    elif typ == "provider.recovered":
        next_state["provider"] = {**next_state.get("provider", {}), "phase": "recovered", "lastRecoveryAt": timestamp}
        next_state["turn"] = {**next_state.get("turn", {}), "liveStatus": {"message": "Provider recovered. Continuing...", "detail": _field(data, "providerLabel", "provider_label"), "source": "runtime", "updatedAt": timestamp}}
    elif typ == "provider.failed":
        failure = {
            "provider": _field(data, "provider"),
            "providerLabel": _field(data, "providerLabel", "provider_label"),
            "category": _field(data, "category"),
            "phase": _field(data, "phase"),
            "message": _field(data, "message"),
            "retryable": _field(data, "retryable"),
            "attempt": _field(data, "attempt"),
            "status": _field(data, "status"),
            "code": _field(data, "code"),
            "errorType": _field(data, "errorType", "error_type"),
            "occurredAt": timestamp,
        }
        next_state["provider"] = {**next_state.get("provider", {}), "phase": "failed", "provider": failure.get("provider"), "lastFailure": failure}
        next_state["turn"] = {**next_state.get("turn", {}), "liveStatus": None}
    elif typ == "provider.input_tokens":
        next_state["provider"] = {
            **next_state.get("provider", {}),
            "provider": _field(data, "provider"),
            "model": _field(data, "model"),
            "inputTokens": _field(data, "inputTokens", "input_tokens"),
            "inputTokenSource": _field(data, "source"),
            "baseInputTokens": _field(data, "baseInputTokens", "base_input_tokens"),
            "safetyMarginTokens": _field(data, "safetyMarginTokens", "safety_margin_tokens"),
        }
    elif typ == "provider.fallback":
        next_state["provider"] = {**next_state.get("provider", {}), "provider": _field(data, "provider"), "model": _field(data, "model")}
    elif typ == "tool.started":
        if (next_state.get("turn", {}).get("liveStatus") or {}).get("source") != "model":
            name = _field(data, "name") or "tool"
            next_state["turn"] = {**next_state.get("turn", {}), "liveStatus": {"message": _default_tool_status_message(name), "detail": _default_tool_status_detail(name), "source": "runtime", "updatedAt": timestamp}}
    elif typ == "tool.completed":
        next_state = _clear_runtime_live_status(next_state)
    elif typ == "compaction.started":
        next_state["compaction"] = {
            **next_state.get("compaction", {}),
            "status": "running",
            "selectedMode": None,
            "appliedModes": None,
            "pressureLevel": None,
            "reactiveAttempt": None,
            "plannedModes": None,
            "trigger": _field(data, "trigger"),
            "currentTokenEstimate": _field(data, "currentTokenEstimate", "current_token_estimate"),
            "currentTokenSource": _field(data, "currentTokenSource", "current_token_source"),
            "updatedAt": timestamp,
        }
        if (next_state.get("turn", {}).get("liveStatus") or {}).get("source") != "model":
            next_state["turn"] = {**next_state.get("turn", {}), "liveStatus": {"message": "Compacting context...", "detail": _format_compaction_live_detail(_field(data, "trigger"), _field(data, "currentTokenEstimate", "current_token_estimate")), "source": "runtime", "updatedAt": timestamp}}
    elif typ == "context_relief.selected":
        next_state["compaction"] = {
            **next_state.get("compaction", {}),
            "selectedMode": _field(data, "mode"),
            "pressureLevel": _normalize_pressure_level(_field(data, "pressureLevel", "pressure_level")),
            "reactiveAttempt": _field(data, "reactiveAttempt", "reactive_attempt"),
            "plannedModes": _field(data, "plannedModes", "planned_modes"),
            "updatedAt": timestamp,
        }
    elif typ == "context_relief.applied":
        pressure = _normalize_pressure_level(_field(data, "pressureLevel", "pressure_level")) or next_state.get("compaction", {}).get("pressureLevel")
        modes = _field(data, "modes")
        next_state["compaction"] = {
            **next_state.get("compaction", {}),
            "selectedMode": _field(data, "mode"),
            "appliedModes": modes if modes is not None else [_field(data, "mode")],
            "pressureLevel": pressure,
            "reactiveAttempt": _field(data, "reactiveAttempt", "reactive_attempt") if _field(data, "reactiveAttempt", "reactive_attempt") is not None else next_state.get("compaction", {}).get("reactiveAttempt"),
            "lastDetail": _field(data, "detail"),
            "lastReinjectionSummary": _field(data, "reinjectionSummary", "reinjection_summary"),
            "updatedAt": timestamp,
        }
    elif typ == "daemon.snapshot":
        phase = _normalize_daemon_phase(_field(data, "phase")) or "running"
        next_state["daemon"] = {
            "phase": phase,
            "epoch": _field(data, "epoch"),
            "pid": _field(data, "pid"),
            "socketPath": _field(data, "socketPath", "socket_path"),
            "supervisorId": _field(data, "supervisorId", "supervisor_id"),
            "attachedClients": _field(data, "attachedClients", "attached_clients"),
            "watchedSessions": _field(data, "watchedSessions", "watched_sessions"),
            "activeTasks": _field(data, "activeTasks", "active_tasks"),
            "updatedAt": _field(data, "updatedAt", "updated_at") or timestamp,
        }
    elif typ in ("daemon.recovering", "daemon.recovered"):
        phase = "recovering" if typ == "daemon.recovering" else "running"
        next_state["daemon"] = {**next_state.get("daemon", {}), "phase": phase, "epoch": _field(data, "epoch") or next_state.get("daemon", {}).get("epoch"), "updatedAt": timestamp}
        if (next_state.get("turn", {}).get("liveStatus") or {}).get("source") != "model":
            message = "Reconnecting to the local task supervisor..." if typ == "daemon.recovering" else "Local task supervisor recovered."
            epoch = _field(data, "epoch")
            next_state["turn"] = {**next_state.get("turn", {}), "liveStatus": {"message": message, "detail": f"epoch {epoch[:8]}" if isinstance(epoch, str) and epoch else None, "source": "runtime", "updatedAt": timestamp}}
    elif typ == "compaction.skipped":
        next_state = _clear_runtime_live_status(next_state)
        next_state["compaction"] = {**next_state.get("compaction", {}), "status": "skipped", "trigger": _field(data, "trigger"), "qualityClassification": _field(data, "qualityClassification", "quality_classification"), "updatedAt": timestamp}
    elif typ == "compaction.completed":
        compaction = next_state.get("compaction", {})
        next_state = _clear_runtime_live_status(next_state)
        next_state["compaction"] = {
            "status": "completed",
            "trigger": _field(data, "trigger"),
            "selectedMode": compaction.get("selectedMode"),
            "appliedModes": compaction.get("appliedModes"),
            "pressureLevel": compaction.get("pressureLevel"),
            "reactiveAttempt": compaction.get("reactiveAttempt"),
            "plannedModes": compaction.get("plannedModes"),
            "generation": _field(data, "generation"),
            "currentTokenEstimate": compaction.get("currentTokenEstimate"),
            "currentTokenSource": compaction.get("currentTokenSource"),
            "qualityClassification": _field(data, "qualityClassification", "quality_classification"),
            "verificationVerdict": _field(data, "verificationVerdict", "verification_verdict"),
            "verificationConfidence": _field(data, "verificationConfidence", "verification_confidence"),
            "compactionProvider": _field(data, "compactionProvider", "compaction_provider"),
            "compactionModel": _field(data, "compactionModel", "compaction_model"),
            "verificationProvider": _field(data, "verificationProvider", "verification_provider"),
            "verificationModel": _field(data, "verificationModel", "verification_model"),
            "summaryPath": _field(data, "summaryPath", "summary_path"),
            "statePath": _field(data, "statePath", "state_path"),
            "workingContextPath": _field(data, "workingContextPath", "working_context_path"),
            "checkpointPath": _field(data, "checkpointPath", "checkpoint_path"),
            "lastDetail": compaction.get("lastDetail"),
            "lastReinjectionSummary": compaction.get("lastReinjectionSummary"),
            "updatedAt": timestamp,
        }
    elif typ == "compaction.failed":
        next_state = _clear_runtime_live_status(next_state)
        next_state["compaction"] = {**next_state.get("compaction", {}), "status": "failed", "trigger": _field(data, "trigger"), "lastError": _field(data, "error"), "updatedAt": timestamp}
    elif typ == "task.snapshot":
        tasks = [_task_from_snapshot_record(task, next_state.get("session", {}).get("activeSessionId"), timestamp) for task in (_field(data, "tasks") or [])]
        next_state["tasks"] = _summarize_tasks(
            {
                "total": _field(data, "total", default=len(tasks)),
                "tasks": tasks,
                "activeCount": _field(data, "active", "activeCount", default=0),
                "backgroundedCount": _field(data, "backgrounded", "backgroundedCount", default=0),
                "awaitingInputCount": _field(data, "awaitingInput", "awaiting_input", "awaitingInputCount", default=0),
                "failedCount": _field(data, "failed", "failedCount", default=0),
                "labels": _field(data, "labels") or [],
                "lastUpdatedAt": timestamp,
            }
        )
    elif typ in ("task.registered", "task.restored"):
        task_id = _field(data, "taskId", "task_id", "id")
        if task_id:
            status = _field(data, "status") or ("idle" if typ == "task.restored" else "running")
            next_state = _upsert_runtime_task(next_state, _task_from_event(data, task_id, status, next_state.get("session", {}).get("activeSessionId"), timestamp, created=True))
    elif typ == "task.updated":
        next_state = _update_runtime_task(next_state, _field(data, "taskId", "task_id", "id"), _task_patch_from_event(data, _field(data, "status"), timestamp))
    elif typ == "task.output_delta":
        next_state = _update_runtime_task(next_state, _field(data, "taskId", "task_id", "id"), {"outputCursor": _field(data, "outputCursor", "output_cursor"), "updatedAt": timestamp})
    elif typ == "task.completed":
        patch = _task_patch_from_event(data, "completed", timestamp)
        patch["completedAt"] = timestamp
        next_state = _update_runtime_task(next_state, _field(data, "taskId", "task_id", "id"), patch)
    elif typ == "task.failed":
        patch = _task_patch_from_event(data, "failed", timestamp)
        patch["completedAt"] = timestamp
        next_state = _update_runtime_task(next_state, _field(data, "taskId", "task_id", "id"), patch)
    elif typ == "health_check.unhealthy":
        title = _field(data, "title") or "Health check"
        error = _field(data, "error")
        summary = f"{title} is unhealthy: {error}" if error else f"{title} is unhealthy."
        next_state = _update_runtime_task(next_state, _field(data, "taskId", "task_id"), {"kind": "health_check", "status": "degraded", "title": _field(data, "title"), "summary": summary, "updatedAt": timestamp})
    elif typ == "health_check.recovered":
        title = _field(data, "title") or "Health check"
        next_state = _update_runtime_task(next_state, _field(data, "taskId", "task_id"), {"kind": "health_check", "status": "running", "title": _field(data, "title"), "summary": f"{title} recovered and is running again.", "updatedAt": timestamp})
    elif typ == "monitor.snapshot":
        next_state["monitors"] = {
            **next_state.get("monitors", {}),
            "snapshot": {
                "total": _field(data, "total"),
                "active": _field(data, "active"),
                "failed": _field(data, "failed"),
                "stopped": _field(data, "stopped"),
                "muted": _field(data, "muted"),
                "nextCheckInMs": _field(data, "nextCheckInMs", "next_check_in_ms"),
                "labels": _field(data, "labels") or [],
            },
        }
    elif typ == "monitor.created":
        monitor_id = _field(data, "monitorId", "monitor_id")
        if monitor_id:
            active = _unique_push(next_state.get("monitors", {}).get("activeMonitorIds", []), monitor_id)
            next_state["monitors"] = {**next_state.get("monitors", {}), "activeMonitorIds": active}
    elif typ == "monitor.assistant_update":
        updates = [
            {
                "monitorId": _field(data, "monitorId", "monitor_id"),
                "taskId": _field(data, "taskId", "task_id"),
                "status": _field(data, "status"),
                "reason": _field(data, "reason"),
                "text": _field(data, "text") or _field(data, "message") or "",
                "timestamp": timestamp,
            },
            *next_state.get("monitors", {}).get("recentAssistantUpdates", []),
        ]
        next_state["monitors"] = {**next_state.get("monitors", {}), "recentAssistantUpdates": updates[:10]}
    elif typ == "collaboration.snapshot":
        collab = next_state.get("collaboration", {})
        next_state["collaboration"] = {
            **collab,
            "workers": [_collaboration_worker_record(worker, timestamp) for worker in (_field(data, "workers") or [])],
            "activeCount": _field(data, "activeCount", "active_count", default=0),
            "awaitingInputCount": _field(data, "awaitingInputCount", "awaiting_input_count", default=0),
            "backgroundCount": _field(data, "backgroundCount", "background_count", default=0),
            "remoteStatus": _field(data, "remoteStatus", "remote_status"),
            "remoteTitle": _field(data, "remoteTitle", "remote_title"),
            "bridgeStatus": _field(data, "bridgeStatus", "bridge_status"),
            "pendingBridgeMessages": _field(data, "pendingBridgeMessages", "pending_bridge_messages"),
            "labels": _field(data, "labels") or [],
        }
    elif typ == "collaboration.assistant_update":
        updates = [
            {
                "sourceKind": _field(data, "sourceKind", "source_kind"),
                "sourceId": _field(data, "sourceId", "source_id"),
                "status": _field(data, "status"),
                "label": _field(data, "label"),
                "text": _field(data, "text") or "",
                "timestamp": timestamp,
            },
            *next_state.get("collaboration", {}).get("recentAssistantUpdates", []),
        ]
        next_state["collaboration"] = {**next_state.get("collaboration", {}), "recentAssistantUpdates": updates[:10]}
    elif typ == "bridge.message":
        bridge_id = _field(data, "bridgeId", "bridge_id") or next_state.get("bridge", {}).get("bridgeId")
        remote_session_id = _field(data, "remoteSessionId", "remote_session_id") or next_state.get("bridge", {}).get("remoteSessionId")
        next_state["bridge"] = {
            "attached": True,
            "bridgeId": bridge_id,
            "remoteSessionId": remote_session_id,
            "lastMessageText": _field(data, "text") or _field(data, "message"),
            "lastMessageAt": timestamp,
            "lastEventName": typ,
            "lastEventAt": timestamp,
        }
        next_state["collaboration"] = {**next_state.get("collaboration", {}), "pendingBridgeMessages": max(1, int(next_state.get("collaboration", {}).get("pendingBridgeMessages") or 0))}
    elif typ == "remote.status_changed":
        remote_session_id = _field(data, "remoteSessionId", "remote_session_id") or next_state.get("bridge", {}).get("remoteSessionId")
        bridge_id = _field(data, "bridgeId", "bridge_id") or next_state.get("bridge", {}).get("bridgeId")
        bridge_status = _field(data, "bridgeStatus", "bridge_status")
        unread = _field(data, "unreadBridgeMessages", "unread_bridge_messages")
        next_state["bridge"] = {
            **next_state.get("bridge", {}),
            "attached": bool(bridge_id) or next_state.get("bridge", {}).get("attached"),
            "bridgeId": bridge_id,
            "remoteSessionId": remote_session_id,
            "lastEventName": typ,
            "lastEventAt": timestamp,
        }
        next_state["collaboration"] = {
            **next_state.get("collaboration", {}),
            "remoteStatus": _field(data, "status") or next_state.get("collaboration", {}).get("remoteStatus"),
            "remoteTitle": _field(data, "title") or next_state.get("collaboration", {}).get("remoteTitle"),
            "bridgeStatus": bridge_status or next_state.get("collaboration", {}).get("bridgeStatus"),
            **({"pendingBridgeMessages": unread} if unread is not None else {}),
        }
    elif typ == "bridge.status_changed":
        status = _field(data, "status")
        pending = _field(data, "pendingMessages", "pending_messages")
        event_name = _field(data, "lastEventName", "last_event_name") or typ
        next_state["bridge"] = {
            **next_state.get("bridge", {}),
            "attached": False if status == "closed" else True,
            "bridgeId": _field(data, "bridgeId", "bridge_id") or next_state.get("bridge", {}).get("bridgeId"),
            "remoteSessionId": _field(data, "remoteSessionId", "remote_session_id") or next_state.get("bridge", {}).get("remoteSessionId"),
            "lastEventName": event_name,
            "lastEventAt": timestamp,
        }
        next_state["collaboration"] = {
            **next_state.get("collaboration", {}),
            "bridgeStatus": status or next_state.get("collaboration", {}).get("bridgeStatus"),
            **({"pendingBridgeMessages": pending} if pending is not None else {}),
        }
    elif typ in ("bridge.event", "remote.event"):
        event_name = _field(data, "eventName", "event_name", "event") or typ
        bridge = next_state.get("bridge", {})
        if typ == "bridge.event":
            next_state["bridge"] = {
                **bridge,
                "attached": False if event_name == "bridge.closed" else True,
                "bridgeId": _field(data, "bridgeId", "bridge_id") or bridge.get("bridgeId"),
                "remoteSessionId": _field(data, "remoteSessionId", "remote_session_id") or bridge.get("remoteSessionId"),
                "lastEventName": event_name,
                "lastEventAt": timestamp,
            }
            next_state["collaboration"] = {**next_state.get("collaboration", {}), "bridgeStatus": "closed" if event_name == "bridge.closed" else next_state.get("collaboration", {}).get("bridgeStatus")}
        else:
            next_state["bridge"] = {**bridge, "remoteSessionId": _field(data, "remoteSessionId", "remote_session_id") or bridge.get("remoteSessionId"), "lastEventName": event_name, "lastEventAt": timestamp}
            remote_status = "active" if event_name in {"remote.session_created", "remote.session_resumed"} else next_state.get("collaboration", {}).get("remoteStatus")
            next_state["collaboration"] = {**next_state.get("collaboration", {}), "remoteStatus": remote_status}
    return next_state


def summarizeSessionRecord(session: dict[str, Any]) -> dict[str, Any]:
    metadata = session.get("metadata", {}) if isinstance(session.get("metadata"), dict) else {}
    return {
        "id": _field(session, "id", "sessionId", "session_id"),
        "cwd": _field(session, "cwd"),
        "name": _field(session, "name"),
        "provider": _field(session, "provider"),
        "model": _field(session, "model"),
        "permissionMode": _field(session, "permissionMode", "permission_mode"),
        "updatedAt": _field(session, "updatedAt", "updated_at"),
        "workspaceName": _field(session, "workspaceName", "workspace_name") or metadata.get("workspaceName"),
        "gitRoot": _field(session, "gitRoot", "git_root") or metadata.get("gitRoot"),
        "gitBranch": _field(session, "gitBranch", "git_branch") or metadata.get("gitBranch"),
        "remoteSessionId": _field(session, "remoteSessionId", "remote_session_id") or metadata.get("remoteSessionId"),
        "bridgeId": _field(session, "bridgeId", "bridge_id") or metadata.get("bridgeId"),
    }


def deriveStoredTurnOutcome(session: dict[str, Any]) -> dict[str, Any] | None:
    text = _extract_last_assistant_text(session)
    if not text:
        return None
    return {"kind": "completed", "message": "Awaiting your reply." if text.rstrip().endswith("?") else "Previous turn complete.", "updatedAt": session.get("updatedAt")}


def _field(data: dict[str, Any], *names: str, default: Any = None) -> Any:
    for name in names:
        if name in data:
            return data.get(name)
    return default


def _extract_last_assistant_text(session: dict[str, Any]) -> str:
    for message in reversed(session.get("messages") or []):
        if message.get("role") != "assistant":
            continue
        content = message.get("content")
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, list):
            return "".join(str(block.get("text") or "") for block in content if isinstance(block, dict) and block.get("type") == "text").strip()
    return ""


def _clear_runtime_live_status(state: dict[str, Any]) -> dict[str, Any]:
    if (state.get("turn", {}).get("liveStatus") or {}).get("source") != "runtime":
        return state
    state["turn"] = {**state.get("turn", {}), "liveStatus": None}
    return state


def _task_from_snapshot_record(task: dict[str, Any], active_session_id: str | None, timestamp: str) -> dict[str, Any]:
    task_id = _field(task, "id", "taskId", "task_id")
    updated_at = _field(task, "updatedAt", "updated_at") or timestamp
    return {
        "id": task_id,
        "sessionId": _field(task, "sessionId", "session_id") or active_session_id or "",
        "sourceId": task_id,
        "kind": _field(task, "kind") or "background_command",
        "status": _field(task, "status") or "idle",
        "title": _field(task, "title") or task_id,
        "summary": _field(task, "summary"),
        "createdAt": updated_at,
        "updatedAt": updated_at,
        "startedAt": updated_at,
        "foregroundable": True,
        "restorable": True,
        "foregroundMode": _field(task, "foregroundMode", "foreground_mode"),
        "taskFamily": _field(task, "taskFamily", "task_family"),
        "detailStatus": _field(task, "detailStatus", "detail_status"),
        "attachableSessionId": _field(task, "attachableSessionId", "attachable_session_id"),
        "retrieveOnly": _field(task, "retrieveOnly", "retrieve_only"),
        "resultSummary": _field(task, "resultSummary", "result_summary"),
    }


def _task_from_event(data: dict[str, Any], task_id: str, status: str, active_session_id: str | None, timestamp: str, *, created: bool) -> dict[str, Any]:
    return {
        "id": task_id,
        "sessionId": _field(data, "sessionId", "session_id") or active_session_id or "",
        "sourceId": task_id,
        "kind": _field(data, "kind") or "background_command",
        "status": status,
        "title": _field(data, "title") or task_id,
        "summary": _field(data, "summary"),
        "createdAt": timestamp if created else _field(data, "createdAt", "created_at") or timestamp,
        "updatedAt": timestamp,
        "startedAt": timestamp,
        "foregroundable": True,
        "restorable": True,
        "foregroundMode": _field(data, "foregroundMode", "foreground_mode"),
        "taskFamily": _field(data, "taskFamily", "task_family"),
        "detailStatus": _field(data, "detailStatus", "detail_status"),
        "attachableSessionId": _field(data, "attachableSessionId", "attachable_session_id"),
        "retrieveOnly": _field(data, "retrieveOnly", "retrieve_only"),
        "resultSummary": _field(data, "resultSummary", "result_summary"),
    }


def _task_patch_from_event(data: dict[str, Any], status: str | None, timestamp: str) -> dict[str, Any]:
    return {
        "kind": _field(data, "kind"),
        "status": status,
        "title": _field(data, "title"),
        "summary": _field(data, "summary"),
        "foregroundMode": _field(data, "foregroundMode", "foreground_mode"),
        "taskFamily": _field(data, "taskFamily", "task_family"),
        "detailStatus": _field(data, "detailStatus", "detail_status"),
        "attachableSessionId": _field(data, "attachableSessionId", "attachable_session_id"),
        "retrieveOnly": _field(data, "retrieveOnly", "retrieve_only"),
        "resultSummary": _field(data, "resultSummary", "result_summary"),
        "updatedAt": timestamp,
    }


def _upsert_runtime_task(state: dict[str, Any], task: dict[str, Any]) -> dict[str, Any]:
    task_slice = copy.deepcopy(state.get("tasks") or {})
    tasks = [copy.deepcopy(existing) for existing in task_slice.get("tasks", [])]
    index = next((idx for idx, existing in enumerate(tasks) if existing.get("id") == task.get("id")), -1)
    if index >= 0:
        tasks[index] = {**tasks[index], **{key: value for key, value in task.items() if value is not None}}
    else:
        tasks.insert(0, {key: value for key, value in task.items() if value is not None})
    state["tasks"] = _summarize_tasks({**task_slice, "tasks": tasks, "lastUpdatedAt": task.get("updatedAt")})
    return state


def _update_runtime_task(state: dict[str, Any], task_id: str | None, patch: dict[str, Any]) -> dict[str, Any]:
    if not task_id:
        return state
    task_slice = state.get("tasks") or {}
    existing = next((task for task in task_slice.get("tasks", []) if task.get("id") == task_id), None)
    if not existing:
        return state
    merged = {**existing, **{key: value for key, value in patch.items() if value is not None}}
    return _upsert_runtime_task(state, merged)


def _summarize_tasks(task_slice: dict[str, Any]) -> dict[str, Any]:
    tasks = [copy.deepcopy(task) for task in task_slice.get("tasks", [])]
    tasks.sort(key=lambda task: str(task.get("updatedAt") or ""), reverse=True)
    tasks = tasks[:24]
    active = [task for task in tasks if task.get("status") in ACTIVE_TASK_STATUSES]
    labels = [task.get("title") for task in active[:6] if task.get("title")]
    return {
        **task_slice,
        "total": max(int(task_slice.get("total") or 0), len(tasks)),
        "tasks": tasks,
        "activeCount": len(active),
        "backgroundedCount": _count_tasks(tasks, "backgrounded"),
        "awaitingInputCount": _count_tasks(tasks, "awaiting_input"),
        "failedCount": sum(1 for task in tasks if task.get("status") in {"failed", "degraded"}),
        "labels": labels,
    }


def _count_tasks(tasks: list[dict[str, Any]], status: str) -> int:
    return sum(1 for task in tasks if task.get("status") == status)


def _collaboration_worker_record(worker: dict[str, Any], timestamp: str) -> dict[str, Any]:
    return {
        "id": _field(worker, "id"),
        "kind": _field(worker, "kind") or "monitor",
        "status": _field(worker, "status") or "idle",
        "label": _field(worker, "label") or _field(worker, "id"),
        "summary": _field(worker, "summary"),
        "sessionId": _field(worker, "sessionId", "session_id"),
        "parentSessionId": _field(worker, "parentSessionId", "parent_session_id"),
        "parentTaskId": _field(worker, "parentTaskId", "parent_task_id"),
        "childSessionId": _field(worker, "childSessionId", "child_session_id"),
        "taskId": _field(worker, "taskId", "task_id"),
        "monitorId": _field(worker, "monitorId", "monitor_id"),
        "remoteSessionId": _field(worker, "remoteSessionId", "remote_session_id"),
        "bridgeId": _field(worker, "bridgeId", "bridge_id"),
        "attachableSessionId": _field(worker, "attachableSessionId", "attachable_session_id"),
        "owner": _field(worker, "owner") if _field(worker, "owner") in {"daemon", "client"} else None,
        "waitingReason": _field(worker, "waitingReason", "waiting_reason"),
        "completionReason": _field(worker, "completionReason", "completion_reason"),
        "resultSummary": _field(worker, "resultSummary", "result_summary"),
        "latestResultAt": _field(worker, "latestResultAt", "latest_result_at"),
        "retryable": _field(worker, "retryable"),
        "stoppable": _field(worker, "stoppable"),
        "recoveryState": _field(worker, "recoveryState", "recovery_state"),
        "updatedAt": _field(worker, "updatedAt", "updated_at") or timestamp,
    }


def _unique_push(values: list[str], next_value: str) -> list[str]:
    return values if next_value in values else [*values, next_value]


def _format_compaction_live_detail(trigger: str | None, current_token_estimate: int | None) -> str | None:
    parts = [part for part in [trigger.strip() if isinstance(trigger, str) else None, f"{current_token_estimate} tokens" if isinstance(current_token_estimate, int) else None] if part]
    return " | ".join(parts) if parts else None


def _default_tool_status_message(name: str) -> str:
    if name == "Read":
        return "Reading files..."
    if name in {"Write", "Edit", "NotebookEdit"}:
        return "Editing files..."
    if name == "Bash":
        return "Running command..."
    if name in {"WebFetch", "WebSearch"}:
        return "Checking the web..."
    if name == "TaskOutput":
        return "Checking task output..."
    return f"{name} running..."


def _default_tool_status_detail(name: str) -> str | None:
    if name == "TaskMonitorStart":
        return "starting background monitor"
    if name == "TaskMonitorStop":
        return "stopping background monitor"
    return None


def _default_outcome_message(stop_reason: str | None) -> str:
    if stop_reason == "plan_approval_required":
        return "Plan ready for review."
    if stop_reason == "plan_chat_requested":
        return "Still in plan mode. Awaiting your reply."
    if stop_reason == "plan_rejected":
        return "Plan rejected."
    if stop_reason == "max_tokens":
        return "Response truncated. Awaiting your next prompt."
    if stop_reason == "max_budget_exceeded":
        return "Budget limit reached."
    return "Turn complete. Ready for the next prompt."


def _session_title_from_summary(workspace_name: str | None, remote_session_id: str | None) -> str | None:
    if workspace_name:
        return workspace_name
    return f"remote {remote_session_id[:8]}" if remote_session_id else None


def _normalize_pressure_level(value: str | None) -> str | None:
    return value if value in {"low", "elevated", "high", "critical"} else None


def _normalize_daemon_phase(value: str | None) -> str | None:
    return value if value in {"starting", "recovering", "running", "shutting_down"} else None
