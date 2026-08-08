from __future__ import annotations

import json
import re
from typing import Any

import agent_core as core

from .model import ScenarioAssertionError
from .runtime_driver import ensure_session_config
from .sandbox import ScenarioSandbox
from agent_core._utils import now_iso


async def seed_collaboration_fixture(data: dict[str, Any], sandbox: ScenarioSandbox, state: dict[str, Any]) -> list[str]:
    session, _config = await ensure_session_config(data, sandbox, state)
    metadata = session.setdefault("metadata", {})
    metadata["workspaceName"] = data.get("workspaceName") or "Remote Harness"
    metadata["gitBranch"] = data.get("gitBranch") or "slice-8"
    metadata["remoteControlEnabled"] = True
    children = _child_records(session["id"])
    for record in children:
        await core.saveChildAgent(record)
    metadata["childAgentIds"] = [record["agentId"] for record in children]
    monitor = core.createMonitorRecord({"sessionId": session["id"], "taskId": "task-monitor", "goal": "Watch remote tests"})
    monitor.update({"id": "monitor-remote", "updatedAt": "2026-01-01T00:00:06Z", "lastAssistantUpdateSummary": "Remote tests are still running."})
    await core.saveMonitor(monitor)
    await core.writeTaskMeta("task-monitor", {"type": "background_command", "taskId": "task-monitor", "id": "task-monitor", "sessionId": session["id"], "command": "pytest", "cwd": str(sandbox.workspace), "startedAt": "2026-01-01T00:00:00Z", "updatedAt": "2026-01-01T00:00:06Z", "status": "running", "outputFile": core.taskOutputPath("task-monitor")})
    await core.saveSession(session)
    state.setdefault("child_agent_aliases", {}).update({record["agentId"].replace("agent-", ""): record["agentId"] for record in children})
    state.setdefault("remote_driver_results", []).append({"driver": "seed_collaboration_fixture", "status": "passed", "childCount": len(children), "monitorId": monitor["id"]})
    await _refresh_remote_state(state)
    return ["remote_driver_results", "remote_sessions", "bridge_records"]


async def create_remote_bridge(data: dict[str, Any], sandbox: ScenarioSandbox, state: dict[str, Any]) -> list[str]:
    session, _config = await ensure_session_config(data, sandbox, state)
    alias = str(data.get("alias") or "main")
    remote = await core.createRemoteSession({"id": data.get("remoteId") or f"remote-{alias}", "session": session, "description": data.get("description") or f"Remote {alias}", "channelServers": data.get("channelServers", ["local"])})
    if data.get("attachSession", True):
        metadata = session.setdefault("metadata", {})
        metadata["remoteControlEnabled"] = True
        metadata["remoteSessionId"] = remote["id"]
    runtime = await _create_bridge_runtime(alias, data, session, remote["id"], state, reconnect=False)
    if data.get("attachSession", True):
        session["metadata"]["bridgeId"] = runtime["bridge"]["id"]
        await core.saveSession(session)
    _remember_bridge(state, alias, remote["id"], runtime)
    state.setdefault("remote_driver_results", []).append({"driver": "create_remote_bridge", "status": "passed", "alias": alias, "remoteId": remote["id"], "bridgeId": runtime["bridge"]["id"]})
    await _refresh_remote_state(state)
    return _remote_keys()


async def queue_bridge_message(data: dict[str, Any], _sandbox: ScenarioSandbox, state: dict[str, Any]) -> list[str]:
    alias = str(data.get("alias") or "main")
    message = await core.sendBridgeMessage({"bridgeId": _bridge_id(state, alias), "text": data.get("text") or "continue", "source": data.get("source", "remote")})
    state.setdefault("bridge_message_aliases", {})[str(data.get("messageAlias") or "last")] = message["id"]
    state.setdefault("remote_driver_results", []).append({"driver": "queue_bridge_message", "status": "passed", "alias": alias, "message": _json_safe(message)})
    await _refresh_remote_state(state)
    return _remote_keys()


async def poll_bridge_inbox(data: dict[str, Any], _sandbox: ScenarioSandbox, state: dict[str, Any]) -> list[str]:
    alias = str(data.get("alias") or "main")
    runtime = _bridge_runtime(state, alias)
    try:
        delivered = await runtime["pollInbox"]()
        record = {"driver": "poll_bridge_inbox", "status": "passed", "alias": alias, "delivered": _json_safe(delivered)}
    except Exception as exc:
        record = {"driver": "poll_bridge_inbox", "status": "failed", "alias": alias, "error": str(exc)}
        state.setdefault("remote_driver_results", []).append(record)
        await _refresh_remote_state(state)
        if not data.get("expectFailure"):
            raise ScenarioAssertionError(f"Bridge inbox poll failed: {exc}", category="driver")
        return _remote_keys()
    state.setdefault("remote_driver_results", []).append(record)
    await _refresh_remote_state(state)
    return _remote_keys()


async def set_bridge_emit_failure(data: dict[str, Any], _sandbox: ScenarioSandbox, state: dict[str, Any]) -> list[str]:
    alias = str(data.get("alias") or "main")
    state.setdefault("bridge_emit_failures", {})[alias] = bool(data.get("enabled", True))
    state.setdefault("remote_driver_results", []).append({"driver": "set_bridge_emit_failure", "status": "passed", "alias": alias, "enabled": bool(data.get("enabled", True))})
    return ["remote_driver_results"]


async def close_bridge_runtime(data: dict[str, Any], _sandbox: ScenarioSandbox, state: dict[str, Any]) -> list[str]:
    alias = str(data.get("alias") or "main")
    runtime = (state.get("bridge_runtimes") or {}).get(alias)
    if runtime and runtime.get("close"):
        result = runtime["close"]()
        if hasattr(result, "__await__"):
            await result
    state.setdefault("remote_driver_results", []).append({"driver": "close_bridge_runtime", "status": "passed", "alias": alias})
    await _refresh_remote_state(state)
    return _remote_keys()


async def reconnect_bridge(data: dict[str, Any], sandbox: ScenarioSandbox, state: dict[str, Any]) -> list[str]:
    session, _config = await ensure_session_config(data, sandbox, state)
    alias = str(data.get("alias") or "main")
    runtime = await _create_bridge_runtime(alias, {"bridgeId": _bridge_id(state, alias), "name": data.get("name"), "startPolling": data.get("startPolling", False)}, session, _remote_id(state, alias), state, reconnect=True)
    _remember_bridge(state, alias, _remote_id(state, alias), runtime)
    state.setdefault("remote_driver_results", []).append({"driver": "reconnect_bridge", "status": "passed", "alias": alias, "bridgeId": runtime["bridge"]["id"]})
    await _refresh_remote_state(state)
    return _remote_keys()


async def collect_bridge_diagnostics(data: dict[str, Any], _sandbox: ScenarioSandbox, state: dict[str, Any]) -> list[str]:
    alias = str(data.get("alias") or "main")
    bridge_id = _bridge_id(state, alias)
    diagnostics = await core.getBridgeDiagnostics(bridge_id)
    tail = await core.formatBridgeTail(bridge_id)
    rows = [_stable_bridge_row(row) for row in await core.listActiveBridgeRows()]
    record = {"driver": "collect_bridge_diagnostics", "status": "passed", "alias": alias, "diagnostics": _json_safe(diagnostics), "tail": tail, "activeRows": rows}
    state.setdefault("bridge_diagnostics", []).append(record)
    state.setdefault("remote_driver_results", []).append({"driver": "collect_bridge_diagnostics", "status": "passed", "alias": alias, "eventCount": len(diagnostics.get("events") or [])})
    await _refresh_remote_state(state)
    return ["bridge_diagnostics", *_remote_keys()]


async def refresh_collaboration(data: dict[str, Any], sandbox: ScenarioSandbox, state: dict[str, Any]) -> list[str]:
    session, config = await ensure_session_config(data, sandbox, state)
    runtime = state.get("collaboration_runtime")
    if not runtime or data.get("newRuntime"):
        events = state.setdefault("collaboration_events", [])
        runtime = await core.createCollaborationRuntime({"session": session, "config": config, "startTicker": False, "minAssistantUpdateSpacingMs": int(data.get("minAssistantUpdateSpacingMs") or 10**15), "emit": lambda event, payload: events.append({"event": event, "data": _json_safe(payload)})})
        state["collaboration_runtime"] = runtime
    else:
        await runtime["refresh"]()
    snapshot = _json_safe(runtime["getSnapshot"]())
    state.setdefault("collaboration_snapshots", []).append({"alias": str(data.get("alias") or "latest"), "snapshot": snapshot})
    state.setdefault("remote_driver_results", []).append({"driver": "refresh_collaboration", "status": "passed", "activeCount": snapshot.get("activeCount"), "awaitingInputCount": snapshot.get("awaitingInputCount")})
    return ["collaboration_events", "collaboration_snapshots", "remote_driver_results"]


async def update_child_agent_status(data: dict[str, Any], _sandbox: ScenarioSandbox, state: dict[str, Any]) -> list[str]:
    agent_id = (state.get("child_agent_aliases") or {}).get(str(data.get("alias") or ""), data.get("agentId"))
    if not agent_id:
        raise ScenarioAssertionError("No child-agent alias supplied", category="harness")
    record = await core.loadChildAgent(agent_id)
    if not record:
        raise ScenarioAssertionError(f"Child agent not found: {agent_id}", category="driver")
    record.update({key: value for key, value in data.items() if key in {"status", "completionReason", "resultSummary", "latestResultAt", "completedAt", "lastUpdatedAt", "retryable", "stoppable"}})
    record.setdefault("lastUpdatedAt", now_iso())
    await core.saveChildAgent(record)
    state.setdefault("remote_driver_results", []).append({"driver": "update_child_agent_status", "status": "passed", "agentId": agent_id, "nextStatus": record.get("status")})
    return ["remote_driver_results"]


async def bootstrap_remote_runtime(data: dict[str, Any], sandbox: ScenarioSandbox, state: dict[str, Any]) -> list[str]:
    session, config = await ensure_session_config(data, sandbox, state)
    boot = await core.bootstrapRuntime(
        {
            "session": session,
            "config": config,
            "cwd": str(sandbox.workspace),
            "daemonEnabled": False,
            "enableBridge": True,
            "bridgeName": data.get("bridgeName") or "qa",
            "bridgeStartPolling": False,
            # Scenario evidence is step-scoped. Background refresh is covered
            # by task-runtime tests; leaving it live here makes the captured
            # event set depend on whether the scenario crosses the 1.5s tick.
            "taskStartTicker": False,
        }
    )
    events = state.setdefault("remote_bootstrap_events", [])
    boot["subscribeEvents"](lambda envelope: events.append(_json_safe(envelope)))
    boot["emitEvent"]("session.activated", core.activeSessionPayload(session, config))
    runtime_state = _json_safe(boot["getState"]())
    record = {"driver": "bootstrap_remote_runtime", "status": "passed", "toolNames": [_tool_name(tool) for tool in boot.get("tools") or [] if _tool_name(tool)], "hasBridge": bool(boot.get("bridge")), "hasCollaboration": bool(boot.get("collaboration")), "hasTasks": bool(boot.get("tasks")), "hasMonitors": bool(boot.get("monitors")), "bridge": runtime_state.get("bridge"), "collaboration": runtime_state.get("collaboration")}
    state["runtime"] = boot
    state.setdefault("remote_bootstrap_results", []).append(record)
    state.setdefault("remote_driver_results", []).append({"driver": "bootstrap_remote_runtime", "status": "passed", "bridgeAttached": (runtime_state.get("bridge") or {}).get("attached")})
    await _refresh_remote_state(state)
    return ["remote_bootstrap_results", "remote_bootstrap_events", *_remote_keys()]


async def project_remote_events(data: dict[str, Any], sandbox: ScenarioSandbox, state: dict[str, Any]) -> list[str]:
    session, _config = await ensure_session_config(data, sandbox, state)
    runtime_state = core.createInitialRuntimeState(session)
    projected = []
    records = [*(state.get("remote_events") or []), *(state.get("collaboration_events") or []), *(_bootstrap_event_records(state))]
    for record in records:
        typed = core.createRuntimeEvent({"event": record.get("event"), "data": record.get("data") or {}, "defaultSessionId": session["id"]})
        runtime_state = core.reduceRuntimeState(runtime_state, typed)
        projected.append({"event": typed.get("event"), "payload": typed.get("payload")})
    state["remote_runtime_state"] = _json_safe(runtime_state)
    state["remote_projected_events"] = _json_safe(projected)
    state.setdefault("remote_driver_results", []).append({"driver": "project_remote_events", "status": "passed", "eventCount": len(projected)})
    return ["remote_runtime_state", "remote_projected_events", "remote_driver_results"]


def assert_bridge_diagnostics(state: dict[str, Any], data: dict[str, Any]) -> None:
    record = _latest_diagnostics(state, str(data.get("alias") or "main"))
    diagnostics = record.get("diagnostics") or {}
    for key in ("status", "remoteStatus", "bridgeStatus", "pendingMessages", "messageCount", "inboxCount"):
        if key in data and diagnostics.get(key) != data[key]:
            raise ScenarioAssertionError(f"bridge diagnostics {key} expected {data[key]!r}, got {diagnostics.get(key)!r}")
    event_names = [event.get("event") for event in diagnostics.get("events") or []]
    missing = sorted(set(data.get("eventsInclude") or []) - set(event_names))
    if missing:
        raise ScenarioAssertionError(f"bridge diagnostics missing events: {missing}")
    body = json.dumps(record, sort_keys=True)
    if data.get("textContains") and data["textContains"] not in body:
        raise ScenarioAssertionError(f"bridge diagnostics missing {data['textContains']!r}")


def assert_collaboration_snapshot(state: dict[str, Any], data: dict[str, Any]) -> None:
    snapshots = state.get("collaboration_snapshots") or []
    if not snapshots:
        raise ScenarioAssertionError("No collaboration snapshot captured")
    snapshot = snapshots[int(data.get("index", len(snapshots) - 1))]["snapshot"]
    for key in ("activeCount", "awaitingInputCount", "backgroundCount", "pendingBridgeMessages", "remoteStatus", "bridgeStatus"):
        if key in data and snapshot.get(key) != data[key]:
            raise ScenarioAssertionError(f"collaboration {key} expected {data[key]!r}, got {snapshot.get(key)!r}")
    workers = {worker.get("label") or worker.get("id"): worker for worker in snapshot.get("workers") or []}
    for label, status in (data.get("workersInclude") or {}).items():
        if (workers.get(label) or {}).get("status") != status:
            raise ScenarioAssertionError(f"worker {label!r} expected status {status!r}")
    names = [event.get("event") for event in state.get("collaboration_events") or []]
    missing = sorted(set(data.get("eventsInclude") or []) - set(names))
    if missing:
        raise ScenarioAssertionError(f"collaboration events missing: {missing}")


def assert_remote_runtime_state(state: dict[str, Any], data: dict[str, Any]) -> None:
    runtime_state = state.get("remote_runtime_state") or {}
    checks = {"bridgeAttached": (runtime_state.get("bridge") or {}).get("attached"), "bridgeLastEventName": (runtime_state.get("bridge") or {}).get("lastEventName"), "remoteStatus": (runtime_state.get("collaboration") or {}).get("remoteStatus"), "pendingBridgeMessages": (runtime_state.get("collaboration") or {}).get("pendingBridgeMessages")}
    for key, expected in data.items():
        if checks.get(key) != expected:
            raise ScenarioAssertionError(f"remote runtime state {key} expected {expected!r}, got {checks.get(key)!r}")


def assert_remote_result(state: dict[str, Any], data: dict[str, Any]) -> None:
    records = [record for record in state.get("remote_driver_results") or [] if not data.get("driver") or record.get("driver") == data["driver"]]
    if not records:
        raise ScenarioAssertionError(f"No remote driver result found for {data.get('driver')!r}")
    record = records[int(data.get("index", len(records) - 1))]
    if data.get("status") and record.get("status") != data["status"]:
        raise ScenarioAssertionError(f"remote result status expected {data['status']!r}, got {record.get('status')!r}")
    if data.get("textContains") and data["textContains"] not in json.dumps(record, sort_keys=True):
        raise ScenarioAssertionError(f"remote result missing {data['textContains']!r}")


async def close_remote_driver(state: dict[str, Any]) -> None:
    runtime = state.pop("collaboration_runtime", None)
    if runtime and runtime.get("close"):
        result = runtime["close"]()
        if hasattr(result, "__await__"):
            await result
    for runtime in list((state.get("bridge_runtimes") or {}).values()):
        if runtime and runtime.get("close"):
            result = runtime["close"]()
            if hasattr(result, "__await__"):
                await result


async def _create_bridge_runtime(alias: str, data: dict[str, Any], session: dict[str, Any], remote_id: str, state: dict[str, Any], *, reconnect: bool) -> dict[str, Any]:
    def emit(event: str, payload: dict[str, Any]) -> None:
        state.setdefault("remote_events", []).append({"alias": alias, "event": event, "data": _json_safe(payload)})
        if event == "bridge.message" and (state.get("bridge_emit_failures") or {}).get(alias):
            raise RuntimeError(f"emit failure for {alias}")

    payload = {"session": session, "remoteSessionId": remote_id, "name": data.get("name"), "startPolling": data.get("startPolling", False), "emit": emit}
    if reconnect:
        payload["bridgeId"] = data.get("bridgeId")
    else:
        payload["id"] = data.get("bridgeId") or data.get("id") or f"bridge-{alias}"
    return await core.createBridgeRuntime(payload)


def _child_records(session_id: str) -> list[dict[str, Any]]:
    base = {"parentSessionId": session_id, "provider": "openai", "model": "gpt-5.4", "mode": "bypassPermissions", "runInBackground": True, "owner": "daemon"}
    return [
        {**base, "agentId": "agent-running", "sessionId": "child-running", "name": "Running worker", "status": "running", "startedAt": "2026-01-01T00:00:01Z", "lastUpdatedAt": "2026-01-01T00:00:01Z", "lastSummary": "Still running.", "stoppable": True},
        {**base, "agentId": "agent-completed", "sessionId": "child-completed", "name": "Completed worker", "status": "completed", "completedAt": "2026-01-01T00:00:04Z", "lastUpdatedAt": "2026-01-01T00:00:04Z", "completionReason": "finished", "resultSummary": "Collected final notes.", "latestResultAt": "2026-01-01T00:00:04Z", "stoppable": False},
        {**base, "agentId": "agent-failed", "sessionId": "child-failed", "name": "Failed worker", "status": "failed", "completedAt": "2026-01-01T00:00:03Z", "lastUpdatedAt": "2026-01-01T00:00:03Z", "completionReason": "worker_failed", "resultSummary": "Tests failed.", "retryable": True, "stoppable": False},
        {**base, "agentId": "agent-awaiting", "sessionId": "child-awaiting", "name": "Awaiting input", "status": "awaiting_input", "startedAt": "2026-01-01T00:00:02Z", "lastUpdatedAt": "2026-01-01T00:00:02Z", "waitingReason": "Needs operator input.", "stoppable": True},
    ]


async def _refresh_remote_state(state: dict[str, Any]) -> None:
    remotes = await core.listRemoteSessions()
    bridges = await core.listBridges()
    state["remote_sessions"] = _json_safe(sorted(remotes, key=lambda item: item.get("id") or ""))
    state["bridge_records"] = _json_safe(
        sorted(
            bridges,
            key=lambda item: (
                int(item.get("generation") or 0),
                item.get("createdAt") or "",
                item.get("id") or "",
            ),
        )
    )
    state["bridge_inbox"] = _json_safe([{"bridgeId": bridge["id"], "messages": await core.loadBridgeInbox(bridge["id"])} for bridge in state["bridge_records"]])
    state["bridge_events"] = _json_safe([{"bridgeId": bridge["id"], "events": await core.loadBridgeEvents(bridge["id"])} for bridge in state["bridge_records"]])


def _remember_bridge(state: dict[str, Any], alias: str, remote_id: str, runtime: dict[str, Any]) -> None:
    state.setdefault("remote_aliases", {})[alias] = remote_id
    state.setdefault("bridge_aliases", {})[alias] = runtime["bridge"]["id"]
    state.setdefault("bridge_runtimes", {})[alias] = runtime


def _remote_id(state: dict[str, Any], alias: str) -> str:
    remote_id = (state.get("remote_aliases") or {}).get(alias)
    if not remote_id:
        raise ScenarioAssertionError(f"Unknown remote alias: {alias}", category="harness")
    return remote_id


def _bridge_id(state: dict[str, Any], alias: str) -> str:
    bridge_id = (state.get("bridge_aliases") or {}).get(alias)
    if not bridge_id:
        raise ScenarioAssertionError(f"Unknown bridge alias: {alias}", category="harness")
    return bridge_id


def _bridge_runtime(state: dict[str, Any], alias: str) -> dict[str, Any]:
    runtime = (state.get("bridge_runtimes") or {}).get(alias)
    if not runtime:
        raise ScenarioAssertionError(f"No bridge runtime for alias: {alias}", category="harness")
    return runtime


def _latest_diagnostics(state: dict[str, Any], alias: str) -> dict[str, Any]:
    records = [record for record in state.get("bridge_diagnostics") or [] if record.get("alias") == alias]
    if not records:
        raise ScenarioAssertionError(f"No bridge diagnostics for alias: {alias}")
    return records[-1]


def _bootstrap_event_records(state: dict[str, Any]) -> list[dict[str, Any]]:
    return [{"event": event.get("event"), "data": event.get("data") or {}} for event in state.get("remote_bootstrap_events") or []]


def _remote_keys() -> list[str]:
    return ["remote_sessions", "bridge_records", "bridge_inbox", "bridge_events", "remote_events", "remote_driver_results"]


def _tool_name(tool: Any) -> str | None:
    return getattr(tool, "name", None) or (tool.get("name") if isinstance(tool, dict) else None)


def _stable_bridge_row(row: str) -> str:
    stable = re.sub(r"^[0-9a-f]{8}(?=\s)", "<bridge>", row, flags=re.I)
    return stable[:-19] + "<timestamp>" if len(stable) >= 19 and stable[-19:-15].isdigit() and stable[-15] == "-" else stable


def _json_safe(value: Any) -> Any:
    try:
        json.dumps(value)
        return value
    except Exception:
        if isinstance(value, dict):
            return {str(key): _json_safe(item) for key, item in value.items()}
        if isinstance(value, list):
            return [_json_safe(item) for item in value]
        return repr(value)
