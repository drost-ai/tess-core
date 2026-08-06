from __future__ import annotations

import copy
import json
import threading
import time
from typing import Any

from .._utils import maybe_await
from . import updates as _collaboration_updates
from .updates import *
from ..session.persistence import persistSession
from ..storage import listChildAgents, listSessionMonitors, loadBridge, loadRemoteSession
from ..runtime.generation import createRuntimeSessionBinding, sameRuntimeBinding
from ..runtime.paths import runWithRuntimePaths

MIN_ASSISTANT_UPDATE_SPACING_MS = 20_000
COLLABORATION_TICK_MS = 1_500

# Preserve the historical monkeypatch surface after splitting provider
# narration into a focused module. Tests and embedders may replace these on
# ``agent_core.collaboration``; the delegating wrapper installs the current
# values before each request.
sendOpenAiRequest = _collaboration_updates.sendOpenAiRequest
sendAnthropicMessagesRequest = _collaboration_updates.sendAnthropicMessagesRequest


async def authorCollaborationUpdate(args: dict[str, Any]) -> str:
    _collaboration_updates.sendOpenAiRequest = sendOpenAiRequest
    _collaboration_updates.sendAnthropicMessagesRequest = sendAnthropicMessagesRequest
    return await _collaboration_updates.authorCollaborationUpdate(args)


async def createCollaborationRuntime(args: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
    args = {**(args or {}), **kwargs}
    session_ref = args["session"]
    config_ref = args.get("config") or {}
    runtime_binding = args.get("runtimeBinding") or createRuntimeSessionBinding(
        session_ref["id"], 1, 1
    )
    runtime_paths = args.get("runtimePaths")
    emit = args.get("emit") or (lambda *_: None)
    min_update_spacing_ms = int(args.get("minAssistantUpdateSpacingMs", MIN_ASSISTANT_UPDATE_SPACING_MS))
    tick_interval = float(args.get("tickIntervalMs", COLLABORATION_TICK_MS)) / 1000
    refresh_lock = threading.Lock()
    state_lock = threading.RLock()
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
        with state_lock:
            if state["closed"]:
                return copy.deepcopy(state["snapshot"])
            captured_session = session_ref
            captured_config = config_ref
            captured_binding = runtime_binding
            captured_previous_workers = copy.deepcopy(state["previousWorkers"])
            captured_remote_signature = state["previousRemoteSignature"]
            captured_bootstrap_tick = state["bootstrapTick"]
        if not refresh_lock.acquire(blocking=False):
            return get_snapshot()
        try:
            metadata = captured_session.get("metadata", {})
            remote = await loadRemoteSession(metadata.get("remoteSessionId")) if metadata.get("remoteSessionId") else None
            bridge = await loadBridge(metadata.get("bridgeId")) if metadata.get("bridgeId") else None
            child_agents = await listChildAgents()
            monitors = await listSessionMonitors(captured_session["id"])
            if not _is_active(captured_binding):
                await _report_stale(captured_binding, "collaboration.tick_discarded")
                return get_snapshot()
            workers = buildCollaborationWorkers({"session": captured_session, "childAgents": child_agents, "monitors": monitors, "remote": remote, "bridge": bridge})
            snapshot = createCollaborationSnapshot(workers, remote, bridge)
            with state_lock:
                previous_snapshot = copy.deepcopy(state["snapshot"])
            if json.dumps(snapshot, sort_keys=True, default=str) != json.dumps(previous_snapshot, sort_keys=True, default=str):
                await _emit_bound(captured_binding, "collaboration.snapshot", collaborationSnapshotPayload(snapshot))
                if not _is_active(captured_binding):
                    return get_snapshot()
            candidates = buildUpdateCandidates({"session": captured_session, "workers": workers, "previousWorkers": captured_previous_workers, "remote": remote, "bridge": bridge, "previousRemoteSignature": captured_remote_signature})
            stale_before_commit = False
            with state_lock:
                if not _is_active_locked(captured_binding):
                    stale_before_commit = True
                else:
                    state["snapshot"] = snapshot
                    state["previousWorkers"] = {worker["id"]: worker for worker in workers}
                    state["previousRemoteSignature"] = serializeRemote(remote, bridge)
                    state["bootstrapTick"] = False
            if stale_before_commit:
                await _report_stale(captured_binding, "collaboration.tick_discarded")
                return get_snapshot()
            for candidate in candidates:
                await _emit_bound(captured_binding, candidate["rawEvent"], candidate["payload"])
            with state_lock:
                foreground_busy = state["foregroundBusy"]
                last_update_at = int(state["lastAssistantUpdateAt"])
            if not captured_bootstrap_tick and not foreground_busy and candidates:
                now_ms = int(time.time() * 1000)
                if now_ms - last_update_at >= min_update_spacing_ms:
                    narratable = next((candidate for candidate in candidates if shouldNarrateWorker(candidate.get("worker") or {})), None)
                    if narratable:
                        text = await authorCollaborationUpdate({"config": captured_config, "session": captured_session, "worker": narratable.get("worker"), "summary": narratable.get("summary") or ""})
                        if text and _is_active(captured_binding):
                            appendAssistantCollaborationMessage(captured_session, text)
                            await persistSession(captured_session, captured_config)
                            if not _is_active(captured_binding):
                                await _report_stale(captured_binding, "collaboration.assistant_update")
                                return get_snapshot()
                            with state_lock:
                                state["lastAssistantUpdateAt"] = now_ms
                            worker = narratable.get("worker") or {}
                            await _emit_bound(captured_binding, "collaboration.assistant_update", {"source_kind": worker.get("kind"), "source_id": worker.get("id"), "status": worker.get("status"), "label": worker.get("label"), "text": text})
            return get_snapshot()
        except BaseException as exc:
            if not _is_active(captured_binding):
                await _report_stale(captured_binding, "collaboration.tick_failed")
                return get_snapshot()
            await _emit_contained(
                "collaboration.tick_failed",
                {
                    "session_id": captured_binding.get("sessionId"),
                    "generation": captured_binding.get("generation"),
                    "error": str(exc)[:2_048],
                },
            )
            return get_snapshot()
        finally:
            refresh_lock.release()

    def get_snapshot() -> dict[str, Any]:
        with state_lock:
            return copy.deepcopy(state["snapshot"])

    def _is_active(binding: Any) -> bool:
        with state_lock:
            return _is_active_locked(binding)

    def _is_active_locked(binding: Any) -> bool:
        return not state["closed"] and sameRuntimeBinding(binding, runtime_binding)

    async def _emit_contained(event: str, data: dict[str, Any]) -> None:
        try:
            await maybe_await(emit(event, data))
        except BaseException:
            return

    async def _report_stale(binding: Any, source_event: str) -> None:
        with state_lock:
            active = runtime_binding
        await _emit_contained(
            "runtime.stale_work_discarded",
            {
                "session_id": active.get("sessionId"),
                "stale_session_id": binding.get("sessionId"),
                "stale_generation": binding.get("generation"),
                "active_generation": active.get("generation"),
                "runtime_instance_id": active.get("runtimeInstanceId"),
                "path_graph_id": active.get("pathGraphId"),
                "source_event": source_event,
            },
        )

    async def _emit_bound(binding: Any, event: str, data: dict[str, Any]) -> None:
        if not _is_active(binding):
            await _report_stale(binding, event)
            return
        await maybe_await(emit(event, {**data, "session_id": binding.get("sessionId")}))
        if not _is_active(binding):
            await _report_stale(binding, "collaboration.tick_failed")

    def _run_refresh_once() -> None:
        try:
            import asyncio

            work = (lambda: refresh())
            asyncio.run(runWithRuntimePaths(runtime_paths, work) if runtime_paths else work())
        except Exception:
            pass

    def _tick_loop() -> None:
        while not stop_event.wait(tick_interval):
            _run_refresh_once()

    def set_foreground_busy(busy: bool) -> None:
        with state_lock:
            state["foregroundBusy"] = bool(busy)
            should_refresh = not busy and not state["closed"]
        if should_refresh:
            threading.Thread(target=_run_refresh_once, name="agent-core-collaboration-refresh", daemon=True).start()

    def set_session(session: dict[str, Any], config: dict[str, Any] | None = None) -> None:
        with state_lock:
            next_binding = createRuntimeSessionBinding(
                session["id"],
                int(runtime_binding.get("generation") or 0) + 1,
                int(runtime_binding.get("configRevision") or 0) + 1,
                runtimeInstanceId=runtime_binding.get("runtimeInstanceId") or "standalone",
                pathGraphId=runtime_binding.get("pathGraphId") or "unbound",
            )
        _install_session(session, config, next_binding)
        with state_lock:
            should_refresh = not state["closed"]
        if should_refresh:
            threading.Thread(target=_run_refresh_once, name="agent-core-collaboration-refresh", daemon=True).start()

    async def activate_session(
        session: dict[str, Any], config: dict[str, Any], binding: Any
    ) -> None:
        _install_session(session, config, binding)
        await refresh()

    def _install_session(session: dict[str, Any], config: dict[str, Any] | None, binding: Any) -> None:
        nonlocal session_ref, config_ref, runtime_binding
        with state_lock:
            if state["closed"]:
                return
            session_ref = session
            config_ref = config or config_ref
            runtime_binding = binding
            state["snapshot"] = emptyCollaborationSnapshot()
            state["previousWorkers"] = {}
            state["previousRemoteSignature"] = ""
            state["lastAssistantUpdateAt"] = 0
            state["bootstrapTick"] = True

    async def close() -> None:
        with state_lock:
            state["closed"] = True
        stop_event.set()
        if tick_thread and tick_thread.is_alive() and threading.current_thread() is not tick_thread:
            tick_thread.join(timeout=2)
        acquired = await __import__("asyncio").to_thread(refresh_lock.acquire, True, 2)
        if acquired:
            refresh_lock.release()

    await refresh()
    if args.get("startTicker", True):
        tick_thread = threading.Thread(target=_tick_loop, name=f"agent-core-collaboration-{session_ref.get('id', '')[:8]}", daemon=True)
        tick_thread.start()
    return {"workers": state["snapshot"]["workers"], "snapshot": get_snapshot, "getSnapshot": get_snapshot, "refresh": refresh, "setForegroundBusy": set_foreground_busy, "setSession": set_session, "activateSession": activate_session, "close": close}


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
