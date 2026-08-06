from __future__ import annotations

import os
from .utils import _truncate
from typing import Any


def _summarize_health_check_task(meta: dict[str, Any]) -> str:
    if meta.get("lastError"):
        return f"{'Check unhealthy' if meta.get('status') == 'unhealthy' else 'Last check failed'}: {meta.get('lastError')}"
    if isinstance(meta.get("lastStatusCode"), int):
        return f"Last HTTP status {meta.get('lastStatusCode')}"
    if isinstance(meta.get("lastExitCode"), int):
        return f"Last exit code {meta.get('lastExitCode')}"
    return "Scheduled health check active."


def _summarize_background_command_task(meta: dict[str, Any], status: str, output_cursor: int, output_preview: str | None) -> str | None:
    if meta.get("status") == "reconciling" or status == "reconciling":
        return "Recovered after daemon restart. Process still appears alive, but lifecycle tracking is degraded."
    if status in {"backgrounded", "running"}:
        return f"Background command is active. Latest output: {output_preview}" if output_preview else f"{_format_relative_path(str(meta.get('cwd') or ''))} · {output_cursor} bytes output"
    if status == "completed":
        return f"Background command completed successfully. Latest output: {output_preview}" if output_preview else "Background command completed successfully."
    if status == "stopped":
        return f"Background command was stopped. Latest output: {output_preview}" if output_preview else "Background command was stopped."
    if meta.get("error"):
        return str(meta.get("error"))
    if isinstance(meta.get("exitCode"), int):
        return f"Background command failed with exit code {meta.get('exitCode')}."
    return output_preview


def _build_background_command_result_summary(meta: dict[str, Any], output_preview: str | None) -> str | None:
    for value in (meta.get("resultSummary"), meta.get("error"), output_preview):
        if isinstance(value, str) and value.strip():
            return value.strip()
    if isinstance(meta.get("exitCode"), int):
        return f"Exited with code {meta.get('exitCode')}."
    return None


def _describe_background_command_detail_status(meta: dict[str, Any], status: str, output_preview: str | None) -> str:
    if meta.get("status") == "reconciling" or status == "reconciling":
        return "recovered_after_daemon_restart"
    if status in {"backgrounded", "running"}:
        return "streaming_output" if output_preview else "launching"
    if status == "completed":
        return "result_ready"
    if status == "stopped":
        return "stopped"
    if status == "failed":
        return "failed"
    return status


def _summarize_detached_main_session_task(
    status: str,
    latest_assistant: str | None,
    latest_user: str | None,
    output_preview: str | None,
) -> str | None:
    if status == "reconciling":
        return "Recovered after daemon restart. The detached session is still alive, but lifecycle tracking is degraded."
    if status in {"backgrounded", "running"}:
        if latest_assistant:
            return f"This session is continuing in the background. Latest assistant state: {latest_assistant}"
        if latest_user:
            return f"This session is continuing in the background. Current work: {latest_user}"
        return f"This session is continuing in the background. Runtime output: {output_preview}" if output_preview else "This session is continuing in the background."
    if status == "completed":
        return f"Detached session completed. Latest assistant result: {latest_assistant}" if latest_assistant else "Detached session completed."
    if status == "stopped":
        return f"Detached session stopped. Latest assistant state: {latest_assistant}" if latest_assistant else "Detached session stopped."
    if status == "failed":
        return f"Detached session failed. Latest assistant state: {latest_assistant}" if latest_assistant else "Detached session failed."
    return latest_assistant or latest_user or output_preview


def _build_detached_main_session_result_summary(status: str, latest_assistant: str | None, latest_user: str | None) -> str | None:
    if latest_assistant:
        return latest_assistant
    if status in {"backgrounded", "running"} and latest_user:
        return f"Continuing work on: {latest_user}"
    return None


def _describe_detached_main_session_detail_status(status: str, latest_assistant: str | None) -> str:
    if status == "reconciling":
        return "recovered_after_daemon_restart"
    if status in {"backgrounded", "running"}:
        return "continuing_session" if latest_assistant else "session_starting"
    if status == "completed":
        return "result_ready"
    if status == "stopped":
        return "stopped"
    if status == "failed":
        return "failed"
    return status


def _format_relative_path(value: str) -> str:
    home = os.environ.get("HOME")
    if home and value.startswith(home):
        return f"~{os.sep}{os.path.relpath(value, home)}"
    return value


def _summarize_recovered_subagent(child: dict[str, Any], result_summary: Any) -> str:
    if child.get("waitingReason"):
        return str(child["waitingReason"])
    if result_summary:
        return str(result_summary)
    if child.get("status") == "completed":
        return "Subagent completed while the daemon was detached from the worker process."
    if child.get("status") in {"failed", "truncated"}:
        return "Subagent ended with a failure while the daemon was detached from the worker process."
    return "Subagent state was recovered after the daemon reattached to persisted worker records."


def _summarize_monitor(monitor: dict[str, Any]) -> str:
    parts = []
    if monitor.get("lastObservedTaskStatus"):
        parts.append(f"task {monitor.get('lastObservedTaskStatus')}")
    if monitor.get("lastObservedOutputSize") is not None:
        parts.append(f"{monitor.get('lastObservedOutputSize')} bytes observed")
    return " · ".join(parts) if parts else str(monitor.get("goal") or "")


def _summarize_child_agent_task(child: dict[str, Any], latest_assistant: str | None, latest_user: str | None, result_summary: Any) -> str | None:
    if child.get("waitingReason"):
        return str(child["waitingReason"])
    if child.get("status") in {"backgrounded", "running"} and latest_assistant:
        return f"Worker is active. Latest state: {latest_assistant}"
    if child.get("status") in {"backgrounded", "running"} and latest_user:
        return f"Worker is active on: {latest_user}"
    if child.get("status") == "completed" and result_summary:
        return f"Worker completed. {result_summary}"
    if child.get("status") in {"failed", "truncated"} and result_summary:
        return f"Worker ended with a problem. {result_summary}"
    if child.get("status") in {"cancelled", "stopped"} and result_summary:
        return f"Worker stopped. {result_summary}"
    return result_summary or child.get("lastSummary") or child.get("error") or child.get("stopReason")


def _describe_child_agent_detail_status(child: dict[str, Any]) -> str | None:
    if child.get("recoveryState") == "recovered_after_daemon_restart":
        return "recovered_after_daemon_restart"
    if child.get("recoveryState") == "completed_while_detached":
        return "completed_while_detached"
    return child.get("status") if child.get("waitingReason") else child.get("completionReason") or child.get("status")


def _summarize_latest_message(session: dict[str, Any], role: str) -> str | None:
    max_length = 220 if role == "assistant" else 180
    for message in reversed(session.get("messages") or []):
        if message.get("role") != role:
            continue
        text = _message_text(message.get("content")).strip()
        if text:
            return _truncate(text, max_length)
    return None


def _message_text(content: Any) -> str:
    if isinstance(content, str):
        return " ".join(content.split())
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict):
                if block.get("type") == "text":
                    parts.append(str(block.get("text") or ""))
                elif isinstance(block.get("content"), str):
                    parts.append(block["content"])
        return " ".join(" ".join(parts).split())
    return ""


def _summarize_command(command: Any) -> str:
    text = str(command or "task").strip()
    return text if len(text) <= 72 else f"{text[:71].rstrip()}..."
