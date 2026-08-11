from __future__ import annotations

import os
from ..platform_support import collectDoctorReport
from ..session.transcript import buildTranscriptSnapshot
from ..storage import (
    list_child_agents,
    list_session_monitors,
    list_session_task_records,
    load_bridge,
    load_remote_session,
)
from .agents import _is_active_child_agent, _is_related_child_agent
from .common import (
    _error,
    _first_user_text,
    _handled,
    _indent_block,
    _info,
    _persist,
    _short_id,
    squashWhitespace,
    truncateAscii,
)
from .debug_bundle import createDebugBundle
from .remote_background import _is_active_task_record, buildBackgroundReport
from .shared import AVAILABLE_TUI_THEMES
from typing import Any


async def _handle_transcript(arg_text: str, session: dict[str, Any], hooks: dict[str, Any]) -> dict[str, Any]:
    arg_text = arg_text.strip()
    limit = None
    if arg_text:
        try:
            limit = max(1, int(arg_text))
        except ValueError:
            return _error(hooks, "Usage: /transcript [limit]")
    return _info(hooks, "Transcript", formatTranscriptEntries(buildTranscriptSnapshot(session, limit)))


async def _handle_debug(
    arg_text: str,
    session: dict[str, Any],
    config: dict[str, Any],
    hooks: dict[str, Any],
    title: str = "Debug",
) -> dict[str, Any]:
    if not session.get("id"):
        return _error(hooks, "A persisted session id is required to create a debug bundle.")
    await _persist(hooks)
    try:
        report = await createDebugBundle(
            {
                "sessionId": session["id"],
                "cwd": session.get("cwd") or config.get("cwd") or os.getcwd(),
                "outputDir": arg_text.strip() or None,
                "session": session,
            }
        )
    except Exception as exc:
        return _error(hooks, str(exc))
    return _info(hooks, title, formatDebugBundleReport(report))


async def _handle_diagnostics(arg_text: str, session: dict[str, Any], config: dict[str, Any], hooks: dict[str, Any]) -> dict[str, Any]:
    if arg_text.strip():
        return _error(hooks, "Usage: /diagnostics")
    report = await collectDoctorReport(session.get("cwd") or config.get("cwd") or os.getcwd())
    return _info(hooks, "Diagnostics", formatDiagnosticsReport(report))


async def _handle_collaboration(arg_text: str, session: dict[str, Any], hooks: dict[str, Any]) -> dict[str, Any]:
    if arg_text.strip():
        return _error(hooks, "Usage: /collaboration")
    return _info(hooks, "Collaboration", await buildCollaborationStatusReport(session))


def formatTranscriptEntries(entries: list[dict[str, Any]]) -> str:
    if not entries:
        return "No transcript entries for this session."
    sections = []
    for entry in entries:
        header = f"{entry.get('timestamp', 'saved')}  {entry.get('tone')}  {entry.get('title')}"
        payload = entry.get("payload") if isinstance(entry.get("payload"), dict) else {}
        details = []
        if payload:
            details.append(f"tool         {payload.get('toolName') or 'Tool'}")
            details.append(f"failed       {'yes' if payload.get('failed') else 'no'}")
        body = str(entry.get("body") or "")
        if body:
            details.append(_indent_block(body, "  "))
        sections.append("\n".join([header, *details]))
    return "\n\n".join(sections)


def formatDebugBundleReport(report: dict[str, Any]) -> str:
    summary = report.get("summary") or {}
    return "\n".join(
        [
            f"session       {report.get('sessionId')}",
            f"bundle        {report.get('bundleDir') or report.get('path')}",
            f"manifest      {report.get('manifestPath')}",
            f"created       {report.get('createdAt')}",
            f"files         {len(report.get('includedFiles') or report.get('files') or [])}",
            f"agents        {summary.get('childAgentCount', 0)}",
            f"monitors      {summary.get('monitorCount', 0)}",
            f"plans         {summary.get('planApprovalCount', 0)}",
            f"tasks         {summary.get('taskCount', 0)}",
            f"compaction    {'yes' if summary.get('hasCompaction') else 'no'}",
            f"remote        {'yes' if summary.get('hasRemote') else 'no'}",
            f"bridge        {'yes' if summary.get('hasBridge') else 'no'}",
            f"transcript    {'yes' if summary.get('hasTranscriptLog') else 'no'}",
        ]
    )


def formatDiagnosticsReport(report: dict[str, Any]) -> str:
    checks = report.get("checks") or []
    lines = [
        f"healthy       {'yes' if report.get('healthy') else 'no'}",
        f"cwd           {report.get('cwd')}",
        f"state root    {report.get('stateRoot')}",
        "",
    ]
    for check in checks:
        message = check.get("message") or check.get("summary")
        suggestion = check.get("suggestion") or check.get("remediation")
        lines.append(f"{str(check.get('id')).ljust(14)} {str(check.get('status')).ljust(5)} {message}")
        if suggestion:
            lines.append(f"{'':14}       {suggestion}")
    return "\n".join(lines).rstrip()


async def buildCollaborationStatusReport(session: dict[str, Any]) -> str:
    agents = [record for record in await list_child_agents() if _is_related_child_agent(session, record)]
    monitors = await list_session_monitors(session.get("id"))
    tasks = await list_session_task_records(session.get("id"))
    metadata = session.get("metadata", {})
    remote = await load_remote_session(metadata.get("remoteSessionId")) if metadata.get("remoteSessionId") else None
    bridge = await load_bridge(metadata.get("bridgeId")) if metadata.get("bridgeId") else None
    awaiting = [record for record in agents if record.get("status") in {"awaiting_input"}]
    failed = [record for record in [*agents, *tasks, *monitors] if record.get("status") in {"failed", "crashed", "orphaned"}]
    sections = [
        "\n".join(
            [
                f"session        {session.get('id')}",
                f"workers        {len([record for record in agents if _is_active_child_agent(record)])}/{len(agents)}",
                f"awaiting       {len(awaiting)}",
                f"tasks          {len([record for record in tasks if _is_active_task_record(record)])}/{len(tasks)}",
                f"monitors       {sum(1 for record in monitors if record.get('status') == 'active')}/{len(monitors)}",
                f"failures       {len(failed)}",
                f"remote         {_short_id(remote.get('id', '')) + ' ' + str(remote.get('status')) if remote else 'none'}",
                f"bridge         {_short_id(bridge.get('id', '')) + ' ' + str(bridge.get('status')) if bridge else 'none'}",
            ]
        ),
        buildBackgroundReport(session, agents, monitors, [task for task in tasks if task.get("kind") in {"background_command", "main_session"}], remote, bridge),
    ]
    return "\n\n".join(section for section in sections if section)


async def _handle_review(arg_text: str, hooks: dict[str, Any]) -> dict[str, Any]:
    run_prompt = hooks.get("runPrompt") if isinstance(hooks, dict) else None
    if not callable(run_prompt):
        return _error(hooks, "/review is only available in an interactive runtime.")
    result = run_prompt({"prompt": buildReviewPrompt(arg_text), "agentName": "reviewer"})
    if hasattr(result, "__await__"):
        result = await result
    return _handled((result or {}).get("text") if isinstance(result, dict) else result or "")


def _handle_statusline(arg_text: str, hooks: dict[str, Any]) -> dict[str, Any]:
    get_visible = hooks.get("getStatusLineVisible") if isinstance(hooks, dict) else None
    set_visible = hooks.get("setStatusLineVisible") if isinstance(hooks, dict) else None
    if not callable(get_visible) or not callable(set_visible):
        return _error(hooks, "/statusline is only available in the interactive TUI.")
    if not arg_text:
        return _info(hooks, "Statusline", f"Status line is {'on' if get_visible() else 'off'}.")
    if arg_text not in {"on", "off"}:
        return _error(hooks, "Usage: /statusline [on|off]")
    set_visible(arg_text == "on")
    return _info(hooks, "Statusline", f"Status line {arg_text}.")


def _handle_theme(arg_text: str, hooks: dict[str, Any]) -> dict[str, Any]:
    get_theme = hooks.get("getTheme") if isinstance(hooks, dict) else None
    set_theme = hooks.get("setTheme") if isinstance(hooks, dict) else None
    if not callable(get_theme) or not callable(set_theme):
        return _error(hooks, "/theme is only available in the interactive TUI.")
    if not arg_text:
        return _info(hooks, "Theme", f"Current theme: {get_theme()}\nAvailable: {', '.join(AVAILABLE_TUI_THEMES)}")
    if arg_text == "next":
        current = get_theme()
        index = AVAILABLE_TUI_THEMES.index(current) if current in AVAILABLE_TUI_THEMES else 0
        theme = AVAILABLE_TUI_THEMES[(index + 1) % len(AVAILABLE_TUI_THEMES)]
    else:
        theme = "tess" if arg_text.strip().lower() == "claude" else arg_text.strip().lower()
        if theme not in AVAILABLE_TUI_THEMES:
            return _error(hooks, f"Unknown theme: {arg_text}. Expected: {', '.join(AVAILABLE_TUI_THEMES)}")
    set_theme(theme)
    return _info(hooks, "Theme", f"Theme set to {theme}")


def _handle_mouse(arg_text: str, hooks: dict[str, Any]) -> dict[str, Any]:
    get_mode = hooks.get("getMouseMode") if isinstance(hooks, dict) else None
    set_mode = hooks.get("setMouseMode") if isinstance(hooks, dict) else None
    if not callable(get_mode) or not callable(set_mode):
        return _error(hooks, "/mouse is only available in the interactive TUI.")
    if not arg_text:
        current = get_mode()
        return _info(
            hooks,
            "Mouse",
            "Mouse capture is on. Wheel and clicks control the TUI."
            if current == "capture"
            else "Mouse capture is off. Native terminal selection is enabled.",
        )
    if arg_text not in {"on", "off", "toggle", "capture", "selection"}:
        return _error(hooks, "Usage: /mouse [on|off|toggle]")
    current = get_mode()
    next_mode = "selection" if arg_text == "toggle" and current == "capture" else "capture" if arg_text in {"toggle", "on", "capture"} else "selection"
    set_mode(next_mode)
    return _info(
        hooks,
        "Mouse",
        "Mouse capture on. Wheel and clicks control the TUI."
        if next_mode == "capture"
        else "Mouse capture off. Native terminal selection enabled.",
    )


def _handle_screen(arg_text: str, hooks: dict[str, Any]) -> dict[str, Any]:
    get_mode = hooks.get("getScreenMode") if isinstance(hooks, dict) else None
    set_mode = hooks.get("setScreenMode") if isinstance(hooks, dict) else None
    if not callable(get_mode) or not callable(set_mode):
        return _error(hooks, "/screen is only available in the interactive TUI.")
    if not arg_text:
        return _info(hooks, "Screen", f"Screen mode is {get_mode()}.\nAvailable: auto, fullscreen, inline")
    if arg_text not in {"auto", "fullscreen", "inline"}:
        return _error(hooks, "Usage: /screen [auto|fullscreen|inline]")
    set_mode(arg_text)
    return _info(hooks, "Screen", f"Screen mode set to {arg_text}.")



def formatSessionCandidate(session: dict[str, Any]) -> str:
    name = session.get("name") or _first_user_text(session) or "untitled"
    return f"{_short_id(session.get('id', ''))}  {session.get('updatedAt', '')[:19]}  {session.get('model', '')}  {truncateAscii(squashWhitespace(name), 90)}"


def buildReviewPrompt(arg_text: str) -> str:
    base = [
        "Review the current workspace and implementation context like a strict code reviewer.",
        "Focus on bugs, regressions, risky assumptions, behavioral mismatches, and missing tests.",
        "Return findings first with file references when possible.",
    ]
    if arg_text.strip():
        base.extend(["", f"Additional review scope: {arg_text.strip()}"])
    return "\n".join(base)
