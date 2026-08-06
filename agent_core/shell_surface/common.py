from __future__ import annotations

from typing import Any
from ..session.message_origin import isGenuineUserRequest as _is_genuine_user_request


def _split_subcommand(arg_text: str) -> tuple[str, str]:
    parts = arg_text.split(None, 1)
    return (parts[0].lower(), parts[1].strip() if len(parts) > 1 else "") if parts else ("", "")


async def _maybe_await(value: Any) -> Any:
    if hasattr(value, "__await__"):
        return await value
    return value


def _call_value(hooks: dict[str, Any], name: str) -> Any:
    fn = hooks.get(name) if isinstance(hooks, dict) else None
    return fn() if callable(fn) else None


def _emit_runtime(hooks: dict[str, Any], event: str, data: dict[str, Any]) -> None:
    fn = hooks.get("emitRuntimeEvent") if isinstance(hooks, dict) else None
    if callable(fn):
        fn(event, data)


def _resolve_monitor(monitors: list[dict[str, Any]], query: str) -> dict[str, Any] | None:
    normalized = query.strip().lower()
    return next((monitor for monitor in monitors if str(monitor.get("id", "")).lower() == normalized), None) or next((monitor for monitor in monitors if normalized in str(monitor.get("id", "")).lower()), None)


def _resolve_task_record(records: list[dict[str, Any]], query: str) -> dict[str, Any] | None:
    normalized = query.strip().lower()
    return next((record for record in records if str(record.get("id", "")).lower() == normalized), None) or next((record for record in records if normalized in str(record.get("id", "")).lower()), None)


def _session_matches(session: dict[str, Any], query: str) -> bool:
    normalized = query.strip().lower()
    values = [session.get("id"), session.get("name"), session.get("cwd"), session.get("model"), _first_user_text(session)]
    return any(normalized in str(value).lower() for value in values if value)


def _first_user_text(session: dict[str, Any]) -> str:
    if isinstance(session.get("firstUserText"), str):
        return session["firstUserText"]
    for message in session.get("messages") or []:
        if not _is_genuine_user_request(message):
            continue
        content = message.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            text = " ".join(str(block.get("text", "")) for block in content if isinstance(block, dict) and block.get("type") == "text")
            if text.strip():
                return text
    return ""


def _short_id(value: str) -> str:
    return value[:12] if len(value) > 12 else value


def squashWhitespace(value: Any) -> str:
    return str(value or "").replace("\n", " ").replace("\t", " ").strip()


def truncateAscii(value: str, limit: int) -> str:
    return value if len(value) <= limit else f"{value[: max(0, limit - 1)]}..."


def _indent_block(value: str, prefix: str) -> str:
    return "\n".join(f"{prefix}{line}" if line else prefix.rstrip() for line in value.splitlines())


def _handled(message: Any) -> dict[str, Any]:
    return {"outcome": "handled", "message": message}


def _info(hooks: dict[str, Any], title: str, message: str) -> dict[str, Any]:
    fn = hooks.get("info") if isinstance(hooks, dict) else None
    if callable(fn):
        fn(title, message)
    return {"outcome": "handled", "title": title, "message": message}


def _error(hooks: dict[str, Any], message: str) -> dict[str, Any]:
    fn = hooks.get("error") if isinstance(hooks, dict) else None
    if callable(fn):
        fn(message)
    return {"outcome": "handled", "isError": True, "message": message}


def _call(hooks: dict[str, Any], name: str) -> None:
    fn = hooks.get(name) if isinstance(hooks, dict) else None
    if callable(fn):
        fn()


async def _persist(hooks: dict[str, Any]) -> None:
    fn = hooks.get("persist") if isinstance(hooks, dict) else None
    if callable(fn):
        result = fn()
        if hasattr(result, "__await__"):
            await result


def _compact_tokens(*values: Any) -> list[str]:
    return [str(value).lower() for value in values if value]


def formatMonitorRecord(record: dict[str, Any]) -> str:
    lines = [
        f"{_short_id(record.get('id', ''))}  {record.get('status')}  task {_short_id(record.get('taskId', ''))}",
        f"goal         {truncateAscii(squashWhitespace(record.get('goal', '')), 160)}",
        f"created      {str(record.get('createdAt', ''))[:19]}",
        f"interval     {record.get('checkIntervalMs')}ms check - {record.get('heartbeatIntervalMs')}ms heartbeat",
    ]
    if record.get("lastAssistantUpdateAt"):
        lines.append(f"last update  {str(record.get('lastAssistantUpdateAt'))[:19]}")
    if record.get("lastObservedTaskStatus"):
        lines.append(f"task status  {record.get('lastObservedTaskStatus')}")
    if record.get("failureReason"):
        lines.append(f"reason       {truncateAscii(squashWhitespace(record.get('failureReason')), 160)}")
    return "\n".join(lines)
