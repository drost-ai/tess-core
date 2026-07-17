from __future__ import annotations

from ..contracts.events import event_kind, event_payload, event_time

from .shared import *
from .mcp import MCP_RUNTIME_STATE_EVENTS as _MCP_RUNTIME_STATE_EVENTS
from .mcp import reduce_mcp_runtime_state as _reduce_mcp_runtime_state

def reduceRuntimeState(state: dict[str, Any], event: dict[str, Any]) -> dict[str, Any]:
    next_state = copy.deepcopy(state)
    typ = event_kind(event)
    data = event_payload(event)
    timestamp = event_time(event) or now_iso()

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
    elif typ in _MCP_RUNTIME_STATE_EVENTS:
        next_state = _reduce_mcp_runtime_state(next_state, typ, data, timestamp)
    elif typ == "sandbox.applied":
        next_state["sandbox"] = {
            **next_state.get("sandbox", {}),
            "backend": _field(data, "backend"),
            "enforcement": _field(data, "enforcement"),
            "writableRoots": _field(data, "writableRoots", "writable_roots"),
            "allowNetwork": _field(data, "allowNetwork", "allow_network"),
            "lastAppliedAt": timestamp,
        }
    elif typ == "sandbox.downgraded":
        next_state["sandbox"] = {
            **next_state.get("sandbox", {}),
            "backend": _field(data, "backend"),
            "enforcement": _field(data, "actual"),
            "lastDowngrade": {
                "requested": _field(data, "requested"),
                "actual": _field(data, "actual"),
                "backend": _field(data, "backend"),
                "reason": _field(data, "reason"),
                "updatedAt": timestamp,
            },
            "updatedAt": timestamp,
        }
    elif typ == "context.raw_pressure":
        next_state["compaction"] = {
            **next_state.get("compaction", {}),
            "rawContext": {
                "rawTokenEstimate": _field(data, "rawTokenEstimate", "raw_token_estimate"),
                "messageCount": _field(data, "messageCount", "message_count"),
                "assistantToolUseCount": _field(data, "assistantToolUseCount", "assistant_tool_use_count"),
                "toolResultCount": _field(data, "toolResultCount", "tool_result_count"),
                "messagesSinceLastCheckpoint": _field(data, "messagesSinceLastCheckpoint", "messages_since_last_checkpoint"),
                "toolResultsSinceLastCheckpoint": _field(data, "toolResultsSinceLastCheckpoint", "tool_results_since_last_checkpoint"),
                "rawTokenDeltaSinceLastCheckpoint": _field(data, "rawTokenDeltaSinceLastCheckpoint", "raw_token_delta_since_last_checkpoint"),
                "toolResultTokenEstimateSinceLastCheckpoint": _field(
                    data,
                    "toolResultTokenEstimateSinceLastCheckpoint",
                    "tool_result_token_estimate_since_last_checkpoint",
                ),
                "largestToolResultTokenEstimate": _field(data, "largestToolResultTokenEstimate", "largest_tool_result_token_estimate"),
                "elapsedMsSinceLastCheckpoint": _field(data, "elapsedMsSinceLastCheckpoint", "elapsed_ms_since_last_checkpoint"),
                "hasDurableCheckpoint": _field(data, "hasDurableCheckpoint", "has_durable_checkpoint"),
                "latestCompactionAt": _field(data, "latestCompactionAt", "latest_compaction_at"),
            },
            "updatedAt": timestamp,
        }
    elif typ == "compaction.decision":
        next_state["compaction"] = {
            **next_state.get("compaction", {}),
            "lastDecision": {
                "shouldCheckpoint": _field(data, "shouldCheckpoint", "should_checkpoint"),
                "trigger": _field(data, "trigger"),
                "reason": _field(data, "reason"),
                "rawTokenEstimate": _field(data, "rawTokenEstimate", "raw_token_estimate"),
                "messageCount": _field(data, "messageCount", "message_count"),
                "messagesSinceLastCheckpoint": _field(data, "messagesSinceLastCheckpoint", "messages_since_last_checkpoint"),
                "toolResultsSinceLastCheckpoint": _field(data, "toolResultsSinceLastCheckpoint", "tool_results_since_last_checkpoint"),
                "rawTokenDeltaSinceLastCheckpoint": _field(data, "rawTokenDeltaSinceLastCheckpoint", "raw_token_delta_since_last_checkpoint"),
                "toolResultTokenEstimateSinceLastCheckpoint": _field(
                    data,
                    "toolResultTokenEstimateSinceLastCheckpoint",
                    "tool_result_token_estimate_since_last_checkpoint",
                ),
                "largestToolResultTokenEstimate": _field(data, "largestToolResultTokenEstimate", "largest_tool_result_token_estimate"),
                "hasDurableCheckpoint": _field(data, "hasDurableCheckpoint", "has_durable_checkpoint"),
                "latestCompactionAt": _field(data, "latestCompactionAt", "latest_compaction_at"),
            },
            "updatedAt": timestamp,
        }
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
            "decisionReason": _field(data, "decisionReason", "decision_reason"),
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
            "durableCheckpointAvailable": _field(data, "durableCheckpointAvailable", "durable_checkpoint_available"),
            "compactions": _field(data, "compactions"),
            "reliefWithoutCheckpoint": _field(data, "reliefWithoutCheckpoint", "relief_without_checkpoint"),
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
            "durableCheckpointAvailable": _field(data, "durableCheckpointAvailable", "durable_checkpoint_available"),
            "compactions": _field(data, "compactions"),
            "reliefWithoutCheckpoint": _field(data, "reliefWithoutCheckpoint", "relief_without_checkpoint"),
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
