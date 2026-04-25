from __future__ import annotations

import copy
import json
from typing import Any

from .storage import listChildAgents, listSessionMonitors, loadBridge, loadRemoteSession


async def createCollaborationRuntime(args: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
    args = {**(args or {}), **kwargs}
    session_ref = args["session"]
    config_ref = args.get("config") or {}
    emit = args.get("emit") or (lambda *_: None)
    state = {"closed": False, "foregroundBusy": False, "snapshot": emptyCollaborationSnapshot(), "previousWorkers": {}, "previousRemoteSignature": ""}

    async def refresh() -> dict[str, Any]:
        if state["closed"]:
            return copy.deepcopy(state["snapshot"])
        remote = await loadRemoteSession(session_ref.get("metadata", {}).get("remoteSessionId")) if session_ref.get("metadata", {}).get("remoteSessionId") else None
        bridge = await loadBridge(session_ref.get("metadata", {}).get("bridgeId")) if session_ref.get("metadata", {}).get("bridgeId") else None
        child_agents = await listChildAgents()
        monitors = await listSessionMonitors(session_ref["id"])
        workers = buildCollaborationWorkers({"session": session_ref, "childAgents": child_agents, "monitors": monitors, "remote": remote, "bridge": bridge})
        snapshot = createCollaborationSnapshot(workers, remote, bridge)
        if json.dumps(snapshot, sort_keys=True, default=str) != json.dumps(state["snapshot"], sort_keys=True, default=str):
            state["snapshot"] = snapshot
            emit("collaboration.snapshot", collaborationSnapshotPayload(snapshot))
        candidates = buildUpdateCandidates({"session": session_ref, "workers": workers, "previousWorkers": state["previousWorkers"], "remote": remote, "bridge": bridge, "previousRemoteSignature": state["previousRemoteSignature"]})
        state["previousWorkers"] = {worker["id"]: worker for worker in workers}
        state["previousRemoteSignature"] = serializeRemote(remote, bridge)
        for candidate in candidates:
            emit(candidate["rawEvent"], candidate["payload"])
        return copy.deepcopy(state["snapshot"])

    def get_snapshot() -> dict[str, Any]:
        return copy.deepcopy(state["snapshot"])

    def set_foreground_busy(busy: bool) -> None:
        state["foregroundBusy"] = bool(busy)

    def set_session(session: dict[str, Any], config: dict[str, Any] | None = None) -> None:
        nonlocal session_ref, config_ref
        session_ref = session
        config_ref = config or config_ref

    async def close() -> None:
        state["closed"] = True

    await refresh()
    return {"workers": state["snapshot"]["workers"], "snapshot": get_snapshot, "getSnapshot": get_snapshot, "refresh": refresh, "setForegroundBusy": set_foreground_busy, "setSession": set_session, "close": close}


def emptyCollaborationSnapshot() -> dict[str, Any]:
    return {"workers": [], "activeCount": 0, "awaitingInputCount": 0, "backgroundCount": 0, "labels": []}


def buildCollaborationWorkers(args: dict[str, Any]) -> list[dict[str, Any]]:
    session = args["session"]
    workers: list[dict[str, Any]] = []
    for record in args.get("childAgents") or []:
        if not isRelatedChildAgent(session, record):
            continue
        workers.append(
            {
                "id": record.get("agentId"),
                "kind": "subagent",
                "status": mapChildAgentStatus(record.get("status")),
                "label": record.get("name") or record.get("agentType") or record.get("agentId"),
                "summary": buildSubagentWorkerSummary(record),
                "sessionId": record.get("sessionId"),
                "parentSessionId": record.get("parentSessionId"),
                "parentTaskId": record.get("parentTaskId"),
                "childSessionId": record.get("sessionId"),
                "taskId": record.get("taskId"),
                "attachableSessionId": record.get("attachableSessionId") or record.get("sessionId"),
                "worktreePath": record.get("worktreePath"),
                "owner": record.get("owner"),
                "waitingReason": record.get("waitingReason"),
                "completionReason": record.get("completionReason"),
                "resultSummary": record.get("resultSummary"),
                "latestResultAt": record.get("latestResultAt"),
                "retryable": record.get("retryable"),
                "stoppable": record.get("stoppable"),
                "recoveryState": record.get("recoveryState"),
                "updatedAt": record.get("lastUpdatedAt") or record.get("completedAt") or record.get("startedAt"),
            }
        )
    for monitor in args.get("monitors") or []:
        workers.append(
            {
                "id": monitor.get("id"),
                "kind": "monitor",
                "status": mapMonitorStatus(monitor.get("status")),
                "label": monitor.get("goal"),
                "summary": monitor.get("lastAssistantUpdateSummary") or monitor.get("failureReason") or monitor.get("lastObservedTaskStatus"),
                "sessionId": session["id"],
                "taskId": monitor.get("taskId"),
                "monitorId": monitor.get("id"),
                "updatedAt": monitor.get("updatedAt"),
            }
        )
    remote = args.get("remote")
    bridge = args.get("bridge")
    if remote:
        workers.append(
            {
                "id": remote.get("id"),
                "kind": "remote",
                "status": mapRemoteStatus(remote.get("status")),
                "label": remote.get("title"),
                "summary": remote.get("lastEventSummary"),
                "sessionId": session["id"],
                "remoteSessionId": remote.get("id"),
                "bridgeId": (bridge or {}).get("id") or remote.get("bridgeId"),
                "attachableSessionId": remote.get("sessionId"),
                "updatedAt": remote.get("updatedAt"),
            }
        )
    return sorted(workers, key=lambda item: item.get("updatedAt") or "", reverse=True)


def createCollaborationSnapshot(workers: list[dict[str, Any]], remote: dict[str, Any] | None = None, bridge: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "workers": workers,
        "activeCount": sum(1 for worker in workers if worker.get("status") in {"active", "running", "backgrounded"}),
        "awaitingInputCount": sum(1 for worker in workers if worker.get("status") == "awaiting_input"),
        "backgroundCount": sum(1 for worker in workers if worker.get("kind") != "remote"),
        "remoteStatus": (remote or {}).get("status"),
        "remoteTitle": (remote or {}).get("title"),
        "bridgeStatus": (bridge or {}).get("status") or (remote or {}).get("bridgeStatus"),
        "pendingBridgeMessages": (bridge or {}).get("pendingMessages") if bridge else (remote or {}).get("unreadBridgeMessages"),
        "labels": [worker.get("label") for worker in workers if worker.get("status") in {"active", "running", "backgrounded"} and worker.get("label")][:6],
    }


def collaborationSnapshotPayload(snapshot: dict[str, Any]) -> dict[str, Any]:
    return {
        "active_count": snapshot.get("activeCount"),
        "awaiting_input_count": snapshot.get("awaitingInputCount"),
        "background_count": snapshot.get("backgroundCount"),
        "remote_status": snapshot.get("remoteStatus"),
        "remote_title": snapshot.get("remoteTitle"),
        "bridge_status": snapshot.get("bridgeStatus"),
        "pending_bridge_messages": snapshot.get("pendingBridgeMessages"),
        "labels": snapshot.get("labels") or [],
        "workers": [_worker_payload(worker) for worker in snapshot.get("workers", [])],
    }


def buildUpdateCandidates(args: dict[str, Any]) -> list[dict[str, Any]]:
    candidates = []
    previous = args.get("previousWorkers") or {}
    for worker in args.get("workers") or []:
        prior = previous.get(worker.get("id"))
        if not prior:
            candidates.append(workerEventCandidate(worker, "started"))
            continue
        if prior.get("status") != worker.get("status") or (prior.get("summary") or "") != (worker.get("summary") or "") or prior.get("updatedAt") != worker.get("updatedAt"):
            candidates.append(workerEventCandidate(worker, "changed"))
    remote_signature = serializeRemote(args.get("remote"), args.get("bridge"))
    remote = args.get("remote")
    bridge = args.get("bridge")
    if remote_signature and remote_signature != args.get("previousRemoteSignature") and remote:
        candidates.append(
            {
                "key": f"remote:{remote.get('id')}:{remote.get('status')}:{remote.get('updatedAt')}",
                "rawEvent": "remote.status_changed",
                "payload": {
                    "remote_session_id": remote.get("id"),
                    "session_id": args["session"]["id"],
                    "status": remote.get("status"),
                    "title": remote.get("title"),
                    "bridge_id": (bridge or {}).get("id") or remote.get("bridgeId"),
                    "bridge_status": (bridge or {}).get("status") or remote.get("bridgeStatus"),
                    "unread_bridge_messages": (bridge or {}).get("pendingMessages") if bridge else remote.get("unreadBridgeMessages"),
                    "summary": remote.get("lastEventSummary"),
                },
            }
        )
    if bridge and bridge.get("lastEventName") == "bridge.message.queued" and (bridge.get("pendingMessages") or 0) > 0:
        candidates.append(
            {
                "key": f"bridge:{bridge.get('id')}:{bridge.get('pendingMessages')}:{bridge.get('updatedAt')}",
                "rawEvent": "bridge.status_changed",
                "payload": {"bridge_id": bridge.get("id"), "remote_session_id": (remote or {}).get("id"), "status": bridge.get("status"), "pending_messages": bridge.get("pendingMessages"), "last_event_name": bridge.get("lastEventName")},
            }
        )
    return _dedupe_candidates(candidates)


def workerEventCandidate(worker: dict[str, Any], changeKind: str) -> dict[str, Any]:
    raw_event = "subagent.status_changed" if worker.get("kind") == "subagent" else "monitor.status_changed" if worker.get("kind") == "monitor" else "remote.status_changed"
    return {"key": f"{worker.get('kind')}:{worker.get('id')}:{worker.get('status')}:{worker.get('updatedAt')}:{worker.get('summary') or ''}", "rawEvent": raw_event, "payload": {**_worker_payload(worker), "change_kind": changeKind}}


def buildSubagentWorkerSummary(record: dict[str, Any]) -> str | None:
    if record.get("waitingReason"):
        return record.get("waitingReason")
    if record.get("status") == "completed" and record.get("resultSummary"):
        return record.get("resultSummary")
    if record.get("status") in {"failed", "truncated", "cancelled", "stopped"} and (record.get("resultSummary") or record.get("error")):
        return record.get("resultSummary") or record.get("error")
    return record.get("lastSummary") or record.get("error") or record.get("stopReason")


def isRelatedChildAgent(session: dict[str, Any], record: dict[str, Any]) -> bool:
    return record.get("parentSessionId") == session.get("id") or record.get("sessionId") == session.get("id") or record.get("agentId") in (session.get("metadata", {}).get("childAgentIds") or [])


def mapChildAgentStatus(status: str | None) -> str:
    return {"queued": "idle", "running": "running", "backgrounded": "backgrounded", "awaiting_plan_approval": "awaiting_input", "awaiting_input": "awaiting_input", "completed": "completed", "failed": "failed", "truncated": "failed", "cancelled": "stopped", "stopped": "stopped"}.get(status or "", "idle")


def mapMonitorStatus(status: str | None) -> str:
    return {"active": "active", "completed": "completed", "failed": "failed", "stopped": "stopped", "muted": "stopped"}.get(status or "", "idle")


def mapRemoteStatus(status: str | None) -> str:
    return {"created": "idle", "idle": "idle", "active": "active", "awaiting_input": "awaiting_input", "completed": "completed", "failed": "failed", "closed": "stopped"}.get(status or "", "idle")


def serializeRemote(remote: dict[str, Any] | None, bridge: dict[str, Any] | None) -> str:
    if not remote and not bridge:
        return ""
    return json.dumps({"remoteId": (remote or {}).get("id"), "remoteStatus": (remote or {}).get("status"), "remoteSummary": (remote or {}).get("lastEventSummary"), "unread": (remote or {}).get("unreadBridgeMessages"), "bridgeId": (bridge or {}).get("id"), "bridgeStatus": (bridge or {}).get("status"), "pending": (bridge or {}).get("pendingMessages"), "event": (bridge or {}).get("lastEventName"), "updatedAt": (remote or {}).get("updatedAt") or (bridge or {}).get("updatedAt")}, sort_keys=True)


def _worker_payload(worker: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": worker.get("id"),
        "agent_id": worker.get("id") if worker.get("kind") == "subagent" else None,
        "kind": worker.get("kind"),
        "status": worker.get("status"),
        "label": worker.get("label"),
        "summary": worker.get("summary"),
        "session_id": worker.get("sessionId"),
        "parent_session_id": worker.get("parentSessionId"),
        "parent_task_id": worker.get("parentTaskId"),
        "child_session_id": worker.get("childSessionId"),
        "task_id": worker.get("taskId"),
        "monitor_id": worker.get("monitorId"),
        "remote_session_id": worker.get("remoteSessionId"),
        "bridge_id": worker.get("bridgeId"),
        "attachable_session_id": worker.get("attachableSessionId"),
        "worktree_path": worker.get("worktreePath"),
        "owner": worker.get("owner"),
        "waiting_reason": worker.get("waitingReason"),
        "completion_reason": worker.get("completionReason"),
        "result_summary": worker.get("resultSummary"),
        "latest_result_at": worker.get("latestResultAt"),
        "retryable": worker.get("retryable"),
        "stoppable": worker.get("stoppable"),
        "recovery_state": worker.get("recoveryState"),
        "updated_at": worker.get("updatedAt"),
    }


def _dedupe_candidates(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen = set()
    out = []
    for candidate in candidates:
        key = candidate.get("key")
        if key in seen:
            continue
        seen.add(key)
        out.append(candidate)
    return out
