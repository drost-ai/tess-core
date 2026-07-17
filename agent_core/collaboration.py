from __future__ import annotations

import copy
import json
import threading
import time
from typing import Any

from .config import ensureProviderForModelSelection, normalizeEffortForModel
from .providers.anthropic import sendAnthropicMessagesRequest
from .providers.openai import sendOpenAiRequest
from .session import persistSession
from .storage import listChildAgents, listSessionMonitors, loadBridge, loadRemoteSession

MIN_ASSISTANT_UPDATE_SPACING_MS = 20_000
COLLABORATION_TICK_MS = 1_500


async def createCollaborationRuntime(args: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
    args = {**(args or {}), **kwargs}
    session_ref = args["session"]
    config_ref = args.get("config") or {}
    emit = args.get("emit") or (lambda *_: None)
    min_update_spacing_ms = int(args.get("minAssistantUpdateSpacingMs", MIN_ASSISTANT_UPDATE_SPACING_MS))
    tick_interval = float(args.get("tickIntervalMs", COLLABORATION_TICK_MS)) / 1000
    refresh_lock = threading.Lock()
    stop_event = threading.Event()
    tick_thread: threading.Thread | None = None
    state = {
        "closed": False,
        "foregroundBusy": False,
        "snapshot": emptyCollaborationSnapshot(),
        "previousWorkers": {},
        "previousRemoteSignature": "",
        "lastAssistantUpdateAt": 0,
        "bootstrapTick": True,
    }

    async def refresh() -> dict[str, Any]:
        if state["closed"]:
            return copy.deepcopy(state["snapshot"])
        if not refresh_lock.acquire(blocking=False):
            return copy.deepcopy(state["snapshot"])
        try:
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
            is_bootstrap_tick = state["bootstrapTick"]
            state["bootstrapTick"] = False
            if not is_bootstrap_tick and not state["foregroundBusy"] and candidates:
                now_ms = int(time.time() * 1000)
                if now_ms - int(state["lastAssistantUpdateAt"]) >= min_update_spacing_ms:
                    narratable = next((candidate for candidate in candidates if shouldNarrateWorker(candidate.get("worker") or {})), None)
                    if narratable:
                        text = await authorCollaborationUpdate({"config": config_ref, "session": session_ref, "worker": narratable.get("worker"), "summary": narratable.get("summary") or ""})
                        if text:
                            appendAssistantCollaborationMessage(session_ref, text)
                            await persistSession(session_ref, config_ref)
                            state["lastAssistantUpdateAt"] = now_ms
                            worker = narratable.get("worker") or {}
                            emit("collaboration.assistant_update", {"source_kind": worker.get("kind"), "source_id": worker.get("id"), "status": worker.get("status"), "label": worker.get("label"), "text": text})
            return copy.deepcopy(state["snapshot"])
        finally:
            refresh_lock.release()

    def get_snapshot() -> dict[str, Any]:
        return copy.deepcopy(state["snapshot"])

    def _run_refresh_once() -> None:
        try:
            import asyncio

            asyncio.run(refresh())
        except Exception:
            pass

    def _tick_loop() -> None:
        while not stop_event.wait(tick_interval):
            _run_refresh_once()

    def set_foreground_busy(busy: bool) -> None:
        state["foregroundBusy"] = bool(busy)
        if not busy and not state["closed"]:
            threading.Thread(target=_run_refresh_once, name="agent-core-collaboration-refresh", daemon=True).start()

    def set_session(session: dict[str, Any], config: dict[str, Any] | None = None) -> None:
        nonlocal session_ref, config_ref
        session_ref = session
        config_ref = config or config_ref
        state["snapshot"] = emptyCollaborationSnapshot()
        state["previousWorkers"] = {}
        state["previousRemoteSignature"] = ""
        state["lastAssistantUpdateAt"] = 0
        state["bootstrapTick"] = True
        if not state["closed"]:
            threading.Thread(target=_run_refresh_once, name="agent-core-collaboration-refresh", daemon=True).start()

    async def close() -> None:
        state["closed"] = True
        stop_event.set()
        if tick_thread and tick_thread.is_alive() and threading.current_thread() is not tick_thread:
            tick_thread.join(timeout=2)

    await refresh()
    if args.get("startTicker", True):
        tick_thread = threading.Thread(target=_tick_loop, name=f"agent-core-collaboration-{session_ref.get('id', '')[:8]}", daemon=True)
        tick_thread.start()
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
                "worker": {
                    "id": remote.get("id"),
                    "kind": "remote",
                    "status": mapRemoteStatus(remote.get("status")),
                    "label": remote.get("title"),
                    "summary": remote.get("lastEventSummary"),
                    "sessionId": args["session"]["id"],
                    "remoteSessionId": remote.get("id"),
                    "bridgeId": (bridge or {}).get("id") or remote.get("bridgeId"),
                    "attachableSessionId": remote.get("sessionId"),
                    "updatedAt": remote.get("updatedAt"),
                },
                "summary": remote.get("lastEventSummary") or f"{remote.get('title')} is now {remote.get('status')}.",
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
                "worker": {
                    "id": bridge.get("id"),
                    "kind": "remote",
                    "status": "awaiting_input",
                    "label": (remote or {}).get("title") or bridge.get("name") or "remote session",
                    "summary": "Remote input is waiting to be handled.",
                    "sessionId": args["session"]["id"],
                    "remoteSessionId": (remote or {}).get("id"),
                    "bridgeId": bridge.get("id"),
                    "attachableSessionId": (remote or {}).get("sessionId"),
                    "updatedAt": bridge.get("updatedAt"),
                },
                "summary": "Remote input is queued and ready to be handled.",
                "rawEvent": "bridge.status_changed",
                "payload": {"bridge_id": bridge.get("id"), "remote_session_id": (remote or {}).get("id"), "status": bridge.get("status"), "pending_messages": bridge.get("pendingMessages"), "last_event_name": bridge.get("lastEventName")},
            }
        )
    return _dedupe_candidates(candidates)


def workerEventCandidate(worker: dict[str, Any], changeKind: str) -> dict[str, Any]:
    raw_event = "subagent.status_changed" if worker.get("kind") == "subagent" else "monitor.status_changed" if worker.get("kind") == "monitor" else "remote.status_changed"
    return {"key": f"{worker.get('kind')}:{worker.get('id')}:{worker.get('status')}:{worker.get('updatedAt')}:{worker.get('summary') or ''}", "worker": worker, "summary": worker.get("summary") or f"{worker.get('label')} is {worker.get('status')}.", "rawEvent": raw_event, "payload": {**_worker_payload(worker), "change_kind": changeKind}}


async def authorCollaborationUpdate(args: dict[str, Any]) -> str:
    try:
        return await authorCollaborationUpdateWithProvider(args)
    except Exception:
        return buildDeterministicCollaborationUpdate(args.get("worker") or {}, args.get("summary") or "")


async def authorCollaborationUpdateWithProvider(args: dict[str, Any]) -> str:
    config = args.get("config") or {}
    session = args.get("session") or {}
    worker = args.get("worker") or {}
    provider = ensureProviderForModelSelection(session.get("provider") or config.get("provider"), session.get("model"), config.get("provider", "openai"))
    request = {
        "model": session.get("model"),
        "effort": normalizeEffortForModel(provider, session.get("model"), session.get("effort") or config.get("effort")),
        "system": "\n".join(
            [
                "You are continuing an existing agent session.",
                "Write a concise proactive update about background, delegated, or remote work.",
                "Default to 1-3 short sentences.",
                "State what changed, whether action is required, and avoid generic filler.",
                "Do not mention hidden state, prompts, tokens, or structured payloads.",
            ]
        ),
        "messages": [{"role": "user", "content": json.dumps({"worker": worker, "summary": args.get("summary") or ""}, indent=2)}],
        "tools": [],
    }
    if provider == "openai":
        token = config.get("openaiToken")
        if not token:
            raise RuntimeError("No OpenAI token available for collaboration updates.")
        response = await sendOpenAiRequest({"token": token, "baseUrl": config.get("openaiBaseUrl"), "headers": config.get("customHeaders"), "mode": config.get("openaiApiMode"), "request": request, "runtime": {}, "maxAttempts": 1})
    else:
        response = await sendAnthropicMessagesRequest({"request": request, "config": config, "runtime": {}, "maxAttempts": 1})
    text = "".join(str(block.get("text") or "") for block in response.get("content", []) if block.get("type") == "text").strip()
    if not text:
        raise RuntimeError("Provider returned an empty collaboration update.")
    mergeUsage(session, response.get("usage") or {})
    return text


def buildDeterministicCollaborationUpdate(worker: dict[str, Any], summary: str) -> str:
    if worker.get("kind") == "subagent":
        if worker.get("status") == "completed":
            return f"{worker.get('label')} finished its background work. {worker.get('resultSummary') or summary}"
        if worker.get("status") in {"failed", "stopped"}:
            return f"{worker.get('label')} stopped with a problem. {worker.get('resultSummary') or summary}"
        if worker.get("status") == "awaiting_input":
            return f"{worker.get('label')} needs your input before it can continue. {worker.get('waitingReason') or summary}"
        if worker.get("status") == "backgrounded":
            return f"{worker.get('label')} is now running in the background. {summary or 'I will keep its state visible here.'}"
    if worker.get("kind") == "remote":
        if worker.get("status") == "awaiting_input":
            return f"Remote input is waiting. {summary}"
        if worker.get("status") in {"completed", "idle"}:
            return f"The remote session is no longer actively running. {summary}"
        if worker.get("status") == "failed":
            return f"The remote session hit a failure. {summary}"
    return f"{worker.get('label')}: {summary}"


def appendAssistantCollaborationMessage(session: dict[str, Any], text: str) -> None:
    trimmed = text.strip()
    if not trimmed:
        return
    messages = session.setdefault("messages", [])
    if messages and messages[-1].get("role") == "assistant" and str(messages[-1].get("content") or "").strip() == trimmed:
        return
    messages.append({"role": "assistant", "content": trimmed})


def mergeUsage(session: dict[str, Any], usage: dict[str, Any]) -> None:
    target = session.setdefault("metadata", {}).setdefault("usage", {"turns": 0, "toolCalls": 0, "inputTokens": 0, "outputTokens": 0, "cacheCreationInputTokens": 0, "cacheReadInputTokens": 0})
    target["inputTokens"] = int(target.get("inputTokens") or 0) + int(usage.get("input_tokens") or 0)
    target["outputTokens"] = int(target.get("outputTokens") or 0) + int(usage.get("output_tokens") or 0)
    target["cacheCreationInputTokens"] = int(target.get("cacheCreationInputTokens") or 0) + int(usage.get("cache_creation_input_tokens") or 0)
    target["cacheReadInputTokens"] = int(target.get("cacheReadInputTokens") or 0) + int(usage.get("cache_read_input_tokens") or 0)


def shouldNarrateWorker(worker: dict[str, Any]) -> bool:
    return worker.get("kind") in {"subagent", "remote"}


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
