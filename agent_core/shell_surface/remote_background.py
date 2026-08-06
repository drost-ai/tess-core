from __future__ import annotations

import os
from .._utils import now_iso
from ..collaboration.remote import (
    formatBridgeTail,
    formatRemoteSessionCandidate,
    sendBridgeMessage,
    updateRemoteSession,
)
from ..daemon import ensureTaskSupervisorDaemon, stopTaskSupervisorDaemon
from ..storage import (
    listBridges,
    listChildAgents,
    listRemoteSessions,
    listSessionMonitors,
    listSessionTaskRecords,
    loadBridge,
    loadDaemonState,
    loadRemoteSession,
    loadSession,
)
from .agents import _is_active_child_agent, _is_related_child_agent, formatChildAgentRecord
from .common import (
    _call,
    _call_value,
    _compact_tokens,
    _error,
    _handled,
    _info,
    _maybe_await,
    _persist,
    _short_id,
    _split_subcommand,
    collapseWhitespace,
    truncate,
)
from .common import formatMonitorRecord
from .tasks import formatTaskInspectReport
from typing import Any
from ..collaboration.bridge.runtime import closeCurrentBridgeGeneration as _closeCurrentBridgeGeneration


async def buildDaemonReport() -> str:
    daemon = await loadDaemonState()
    if not daemon:
        return "Daemon not running."
    return "\n".join(f"{key:<13} {daemon.get(key)}" for key in ("phase", "pid", "socketPath", "supervisorId", "epoch", "attachedClients", "watchedSessions", "activeTasks", "updatedAt"))


async def _handle_daemon(arg_text: str, session: dict[str, Any], config: dict[str, Any], hooks: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
    command = arg_text.strip().lower()
    controller = args.get("daemonController") or {}
    if command == "restart":
        restart_fn = controller.get("restart") if isinstance(controller, dict) else None
        if callable(restart_fn):
            await _maybe_await(restart_fn())
        else:
            await stopTaskSupervisorDaemon()
            await ensureTaskSupervisorDaemon({"cwd": session.get("cwd") or config.get("cwd") or os.getcwd()})
    return _info(hooks, "Daemon", await buildDaemonReport())


async def _handle_remote(arg_text: str, session: dict[str, Any], hooks: dict[str, Any]) -> dict[str, Any]:
    metadata = session.get("metadata", {})
    remote = await loadRemoteSession(metadata.get("remoteSessionId")) if metadata.get("remoteSessionId") else None
    bridge = await loadBridge(metadata.get("bridgeId")) if metadata.get("bridgeId") else None
    if not arg_text:
        if remote or bridge:
            return _info(hooks, "Remote", buildRemoteReport(session, remote, bridge))
        fallback = _call_value(hooks, "getRemoteReport")
        return _info(hooks, "Remote", str(fallback) if fallback is not None else buildRemoteFallbackReport(session))

    subcommand, query = _split_subcommand(arg_text)
    if subcommand == "list":
        return _info(hooks, "Remote", await buildRemoteListReport())
    if subcommand in {"inspect", "show"}:
        selection = await _resolve_remote_record(query, session, remote)
        if not selection:
            return _error(hooks, f"No remote session matched: {query or 'current'}")
        linked_bridge = await loadBridge(selection.get("bridgeId")) if selection.get("bridgeId") else bridge
        return _info(hooks, "Remote", formatRemoteSessionRecord(selection, linked_bridge))
    if subcommand in {"tail", "events"}:
        selected_bridge = await _resolve_bridge_record(query, session, bridge)
        if not selected_bridge:
            return _error(hooks, f"No bridge matched: {query or 'current'}")
        return _info(hooks, "Bridge", await formatBridgeTail(selected_bridge["id"]))
    if subcommand in {"queue", "send", "message"}:
        if not query:
            return _error(hooks, "Usage: /remote queue TEXT")
        selected_bridge = await _resolve_bridge_record("", session, bridge)
        if not selected_bridge and remote and remote.get("bridgeId"):
            selected_bridge = await loadBridge(remote["bridgeId"])
        if not selected_bridge:
            return _error(hooks, "No active bridge is available for this session.")
        try:
            message = await sendBridgeMessage({"bridgeId": selected_bridge["id"], "text": query, "source": "local"})
        except Exception as exc:
            return _error(hooks, str(exc))
        return _info(hooks, "Bridge", f"Queued {message.get('id')} for bridge {_short_id(selected_bridge['id'])}")
    if subcommand in {"close", "disconnect"}:
        selection = await _resolve_remote_record(query, session, remote)
        selected_bridge = await _resolve_bridge_record(query, session, bridge)
        if not selection and not selected_bridge:
            return _error(hooks, f"No remote or bridge matched: {query or 'current'}")
        await _close_remote_records(selection, selected_bridge)
        _call(hooks, "refresh")
        await _persist(hooks)
        return _info(hooks, "Remote", f"Closed {selection.get('id') if selection else selected_bridge.get('id')}")
    if subcommand not in {"attach", "resume"}:
        return _error(hooks, "Usage: /remote [list|inspect|tail|attach|resume|queue|close]")
    request_resume = hooks.get("requestResume") if isinstance(hooks, dict) else None
    if not callable(request_resume):
        return _error(hooks, "/remote attach is only available in the full-screen TUI.")
    target_session_id = await _resolve_remote_attach_target(query, session, remote)
    if not target_session_id:
        return _error(hooks, "No attachable remote session is available.")
    target_session = await loadSession(target_session_id)
    if not target_session:
        return _error(hooks, f"Remote session target not found: {target_session_id}")
    await _persist(hooks)
    request_resume(target_session)
    return _handled(f"resuming {target_session_id}")


async def _handle_background(arg_text: str, session: dict[str, Any], hooks: dict[str, Any]) -> dict[str, Any]:
    agents = [record for record in await listChildAgents() if _is_related_child_agent(session, record)]
    monitors = await listSessionMonitors(session.get("id"))
    tasks = [
        record
        for record in await listSessionTaskRecords(session.get("id"))
        if record.get("kind") in {"background_command", "main_session"}
    ]
    metadata = session.get("metadata", {})
    remote = await loadRemoteSession(metadata.get("remoteSessionId")) if metadata.get("remoteSessionId") else None
    bridge = await loadBridge(metadata.get("bridgeId")) if metadata.get("bridgeId") else None

    if not arg_text:
        return _info(hooks, "Background", buildBackgroundReport(session, agents, monitors, tasks, remote, bridge))

    subcommand, query = _split_subcommand(arg_text)
    if not subcommand or not query:
        return _error(hooks, "Usage: /background [inspect|attach|resume] <id>")
    selection = _resolve_background_selection(agents, monitors, tasks, remote, bridge, query)
    if not selection:
        return _error(hooks, f"No background item matched: {query}")

    if subcommand == "inspect":
        return _info(hooks, "Background", _format_background_selection(selection))
    if subcommand not in {"attach", "resume"}:
        return _error(hooks, "Usage: /background [inspect|attach|resume] <id>")

    request_resume = hooks.get("requestResume") if isinstance(hooks, dict) else None
    if not callable(request_resume):
        return _error(hooks, "/background attach is only available in the full-screen TUI.")
    target_session_id = _background_attach_target(selection)
    if not target_session_id:
        return _error(hooks, f"No attachable session is available for {selection['label']}.")
    target_session = await loadSession(target_session_id)
    if not target_session:
        return _error(hooks, f"Attach target not found: {target_session_id}")
    await _persist(hooks)
    request_resume(target_session)
    return _handled(f"resuming {target_session_id}")


def buildRemoteReport(
    session: dict[str, Any],
    remote: dict[str, Any] | None = None,
    bridge: dict[str, Any] | None = None,
) -> str:
    sections = [buildRemoteFallbackReport(session)]
    if remote:
        sections.append(formatRemoteSessionRecord(remote, bridge))
    if bridge and not remote:
        sections.append(
            "\n".join(
                [
                    f"{_short_id(bridge.get('id', ''))}  {bridge.get('status')}  {bridge.get('name') or 'bridge'}",
                    f"updated      {str(bridge.get('updatedAt', ''))[:19]}",
                    f"last event   {bridge.get('lastEventName') or 'n/a'}",
                    f"pending      {bridge.get('pendingMessages') or 0}",
                ]
            )
        )
    return "\n\n".join(sections)


def buildRemoteFallbackReport(session: dict[str, Any]) -> str:
    metadata = session.get("metadata", {})
    lines = [
        f"session        {session.get('id')}",
        f"cwd            {session.get('cwd')}",
        f"remote         {metadata.get('remoteSessionId') or 'none'}",
        f"bridge         {metadata.get('bridgeId') or 'none'}",
        f"channels       {', '.join(metadata.get('channelServers') or []) or 'none'}",
        f"tmux           {metadata.get('tmuxSessionName') or 'none'}",
        f"ide            {metadata.get('ideName') or 'none'}",
        f"chrome         {metadata.get('chromeIntegrationMode') or 'off'}",
    ]
    if metadata.get("remoteSessionTitle"):
        lines.insert(3, f"title          {metadata.get('remoteSessionTitle')}")
    return "\n".join(lines)


async def buildRemoteListReport() -> str:
    remotes = await listRemoteSessions()
    if not remotes:
        return "No remote sessions recorded."
    return "\n".join(formatRemoteSessionCandidate(record) for record in remotes[:12])


async def _resolve_remote_record(
    query: str,
    session: dict[str, Any],
    current_remote: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    normalized = query.strip().lower()
    if not normalized:
        return current_remote
    if current_remote and any(normalized in token for token in _background_remote_tokens(current_remote)):
        return current_remote
    for record in await listRemoteSessions():
        if any(normalized in token for token in _background_remote_tokens(record)):
            return record
    session_match = await loadSession(query)
    if session_match:
        for record in await listRemoteSessions():
            if record.get("sessionId") == session_match.get("id") or record.get("foregroundSessionId") == session_match.get("id"):
                return record
    metadata = session.get("metadata", {})
    if metadata.get("remoteSessionId") and normalized in str(metadata.get("remoteSessionId")).lower():
        return await loadRemoteSession(metadata["remoteSessionId"])
    return None


async def _resolve_bridge_record(
    query: str,
    session: dict[str, Any],
    current_bridge: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    normalized = query.strip().lower()
    if not normalized:
        return current_bridge
    if current_bridge and any(normalized in token for token in _compact_tokens(current_bridge.get("id"), current_bridge.get("name"), current_bridge.get("remoteSessionId"), current_bridge.get("sessionId"), current_bridge.get("status"))):
        return current_bridge
    remote = await _resolve_remote_record(query, session, None)
    if remote and remote.get("bridgeId"):
        bridge = await loadBridge(remote["bridgeId"])
        if bridge:
            return bridge
    for bridge in await listBridges():
        tokens = _compact_tokens(bridge.get("id"), bridge.get("name"), bridge.get("remoteSessionId"), bridge.get("sessionId"), bridge.get("status"))
        if any(normalized in token for token in tokens):
            return bridge
    metadata = session.get("metadata", {})
    if metadata.get("bridgeId") and normalized in str(metadata.get("bridgeId")).lower():
        return await loadBridge(metadata["bridgeId"])
    return None


async def _close_remote_records(remote: dict[str, Any] | None, bridge: dict[str, Any] | None) -> None:
    timestamp = now_iso()
    if bridge:
        await _closeCurrentBridgeGeneration(bridge)
    if remote and not bridge:
        await updateRemoteSession(
            remote["id"],
            {
                "status": "idle",
                "bridgeStatus": "closed" if bridge else remote.get("bridgeStatus"),
                "lastEventName": "remote.closed",
                "lastEventAt": timestamp,
                "lastEventSummary": "Remote shell record closed.",
                "unreadBridgeMessages": 0,
            },
        )


def buildBackgroundReport(
    session: dict[str, Any],
    agents: list[dict[str, Any]],
    monitors: list[dict[str, Any]],
    tasks: list[dict[str, Any]],
    remote: dict[str, Any] | None = None,
    bridge: dict[str, Any] | None = None,
) -> str:
    active_agents = [record for record in agents if _is_active_child_agent(record)]
    active_tasks = [record for record in tasks if _is_active_task_record(record)]
    lines = [
        f"session        {session.get('id')}",
        f"background     {len(session.get('metadata', {}).get('backgroundSessionIds') or [])}",
        f"tasks          {len(active_tasks)}/{len(tasks)}",
        f"workers        {len(active_agents)}/{len(agents)}",
        f"monitors       {sum(1 for record in monitors if record.get('status') == 'active')}/{len(monitors)}",
        f"remote         {_short_id(remote.get('id', '')) + ' ' + str(remote.get('status')) if remote else 'none'}",
    ]
    sections = ["\n".join(lines)]
    if active_tasks:
        sections.append("\n".join(["Tasks:", *[f"- {_format_background_line(formatTaskInspectReport(record))}" for record in active_tasks[:8]]]))
    if active_agents:
        sections.append("\n".join(["Workers:", *[f"- {_format_background_line(formatChildAgentRecord(record))}" for record in active_agents[:8]]]))
    if monitors:
        sections.append("\n".join(["Monitors:", *[f"- {_format_background_line(formatMonitorRecord(record))}" for record in monitors[:6]]]))
    if remote:
        sections.append("\n".join(["Remote:", *[f"- {line}" for line in formatRemoteSessionRecord(remote, bridge).splitlines()]]))
    sections.append("tip            /background inspect <id> or /background attach <id>")
    return "\n\n".join(sections)


async def _resolve_remote_attach_target(query: str, session: dict[str, Any], remote: dict[str, Any] | None) -> str | None:
    metadata = session.get("metadata", {})
    if not query:
        return (remote or {}).get("foregroundSessionId") or (remote or {}).get("sessionId")
    remote_id = str(metadata.get("remoteSessionId") or "")
    if remote_id and query.lower() in remote_id.lower():
        return (remote or {}).get("foregroundSessionId") or (remote or {}).get("sessionId")
    saved = await loadSession(query)
    return (saved or {}).get("id")


def _resolve_background_selection(
    agents: list[dict[str, Any]],
    monitors: list[dict[str, Any]],
    tasks: list[dict[str, Any]],
    remote: dict[str, Any] | None,
    bridge: dict[str, Any] | None,
    query: str,
) -> dict[str, Any] | None:
    normalized = query.strip().lower()
    ordered = [
        ("task", tasks, _background_task_tokens, lambda record: record.get("title")),
        ("agent", agents, _background_agent_tokens, lambda record: record.get("name") or record.get("agentType") or record.get("agentId")),
        ("monitor", monitors, _background_monitor_tokens, lambda record: record.get("goal")),
    ]
    for kind, records, tokenizer, labeler in ordered:
        for record in records:
            if normalized in tokenizer(record):
                return {"kind": kind, "label": labeler(record), "record": record}
    if remote and normalized in _background_remote_tokens(remote, bridge):
        return {"kind": "remote", "label": remote.get("title"), "record": remote, "bridge": bridge}
    for kind, records, tokenizer, labeler in ordered:
        for record in records:
            if any(normalized in token for token in tokenizer(record)):
                return {"kind": kind, "label": labeler(record), "record": record}
    if remote and any(normalized in token for token in _background_remote_tokens(remote, bridge)):
        return {"kind": "remote", "label": remote.get("title"), "record": remote, "bridge": bridge}
    return None


def _format_background_selection(selection: dict[str, Any]) -> str:
    if selection.get("kind") == "task":
        return formatTaskInspectReport(selection["record"])
    if selection.get("kind") == "agent":
        return formatChildAgentRecord(selection["record"])
    if selection.get("kind") == "monitor":
        return formatMonitorRecord(selection["record"])
    return formatRemoteSessionRecord(selection["record"], selection.get("bridge"))


def _background_attach_target(selection: dict[str, Any]) -> str | None:
    record = selection.get("record") or {}
    if selection.get("kind") == "agent":
        return record.get("sessionId")
    if selection.get("kind") == "task":
        return record.get("attachableSessionId")
    if selection.get("kind") == "remote":
        return record.get("foregroundSessionId") or record.get("sessionId")
    return None


def _background_task_tokens(record: dict[str, Any]) -> list[str]:
    return _compact_tokens(
        record.get("id"),
        record.get("sourceId"),
        record.get("kind"),
        record.get("status"),
        record.get("title"),
        record.get("summary"),
        record.get("detailStatus"),
        record.get("attachableSessionId"),
        record.get("command"),
    )


def _background_agent_tokens(record: dict[str, Any]) -> list[str]:
    return _compact_tokens(
        record.get("agentId"),
        record.get("sessionId"),
        record.get("taskId"),
        record.get("name"),
        record.get("agentType"),
        record.get("teamName"),
        record.get("status"),
    )


def _background_monitor_tokens(record: dict[str, Any]) -> list[str]:
    return _compact_tokens(record.get("id"), record.get("taskId"), record.get("goal"), record.get("status"))


def _background_remote_tokens(record: dict[str, Any], bridge: dict[str, Any] | None = None) -> list[str]:
    return _compact_tokens(
        record.get("id"),
        record.get("sessionId"),
        record.get("foregroundSessionId"),
        record.get("title"),
        record.get("description"),
        record.get("status"),
        record.get("bridgeId"),
        (bridge or {}).get("id"),
        (bridge or {}).get("name"),
    )


def formatRemoteSessionRecord(record: dict[str, Any], bridge: dict[str, Any] | None = None) -> str:
    lines = [
        f"{_short_id(record.get('id', ''))}  {record.get('status')}  {record.get('title')}",
        f"session      {_short_id(record.get('sessionId', ''))}",
        f"updated      {str(record.get('updatedAt') or '')[:19].replace('T', ' ')}",
        f"channels     {', '.join(record.get('channelServers') or []) or 'none'}",
    ]
    if record.get("foregroundSessionId"):
        lines.append(f"foreground   {_short_id(record.get('foregroundSessionId', ''))}")
    if record.get("lastEventName"):
        lines.append(f"last event   {record.get('lastEventName')}")
    if record.get("lastEventSummary"):
        lines.append(f"summary      {truncate(collapseWhitespace(record.get('lastEventSummary')), 180)}")
    if record.get("unreadBridgeMessages"):
        lines.append(f"queued       {record.get('unreadBridgeMessages')}")
    if bridge:
        lines.append(f"bridge       {_short_id(bridge.get('id', ''))}  {bridge.get('status')}")
        if bridge.get("lastEventName"):
            lines.append(f"bridge evt   {bridge.get('lastEventName')}")
        if bridge.get("pendingMessages"):
            lines.append(f"bridge q     {bridge.get('pendingMessages')}")
    return "\n".join(lines)


def _is_active_task_record(record: dict[str, Any]) -> bool:
    return record.get("status") in {"queued", "running", "backgrounded", "awaiting_input", "reconciling", "degraded"}


def _format_background_line(text: str) -> str:
    return " | ".join(text.splitlines())
