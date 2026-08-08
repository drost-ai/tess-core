from __future__ import annotations

import json
import os
import re
from typing import Any


def buildTranscriptSnapshot(session: dict[str, Any], limit: int | None = None) -> list[dict[str, Any]]:
    messages = session.get("messages", [])
    if isinstance(limit, int):
        messages = messages[-limit:]
    tool_uses: dict[str, dict[str, str]] = {}
    ignored_tool_use_ids: set[str] = set()
    entries: list[dict[str, Any]] = []
    assistant_title = session.get("metadata", {}).get("assistantName") or session.get("name") or "Assistant"

    for message in messages:
        content = message.get("content")
        role = message.get("role")
        if isinstance(content, str):
            entries.append(
                makeTextEntry(
                    {
                        "tone": "assistant" if role == "assistant" else "user",
                        "title": assistant_title if role == "assistant" else "You",
                        "body": content,
                        "timestamp": "saved",
                    }
                )
            )
            continue

        if not isinstance(content, list):
            continue

        text_buffer = ""

        def flush_text() -> None:
            nonlocal text_buffer
            normalized = normalizeTranscriptBody(text_buffer)
            if not normalized:
                text_buffer = ""
                return
            entries.append(
                makeTextEntry(
                    {
                        "tone": "assistant" if role == "assistant" else "user",
                        "title": assistant_title if role == "assistant" else "You",
                        "body": normalized,
                        "timestamp": "saved",
                    }
                )
            )
            text_buffer = ""

        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text":
                text = str(block.get("text") or "")
                text_buffer = f"{text_buffer.rstrip()}\n\n{text.lstrip()}" if text_buffer else text
                continue
            flush_text()
            if block.get("type") == "tool_use":
                block_id = str(block.get("id") or "")
                name = str(block.get("name") or "")
                if name == "SetStatus":
                    ignored_tool_use_ids.add(block_id)
                    continue
                if block_id:
                    tool_uses[block_id] = {"toolName": name, "label": formatTranscriptToolLabel(name, block.get("input"))}
                continue
            if block.get("type") == "tool_result":
                tool_use_id = str(block.get("tool_use_id") or "")
                if tool_use_id in ignored_tool_use_ids:
                    continue
                tool_use = tool_uses.get(tool_use_id)
                label = (tool_use or {}).get("label")
                entries.append(
                    makeToolEntry(
                        {
                            "tone": "tool",
                            "title": label or "Tool",
                            "toolName": (tool_use or {}).get("toolName") or inferToolNameFromLabel(label) or "Tool",
                            "output": block.get("content"),
                            "failed": block.get("is_error"),
                            "timestamp": "saved",
                        }
                    )
                )

        flush_text()

    return compactTranscriptEntries(entries)


def makeTextEntry(args: dict[str, Any]) -> dict[str, Any]:
    return {
        "tone": args.get("tone", "assistant"),
        "title": args.get("title", "Assistant"),
        "body": normalizeTranscriptBody(str(args.get("body", ""))),
        "timestamp": args.get("timestamp", "saved"),
    }


def makeToolEntry(args: dict[str, Any]) -> dict[str, Any]:
    normalized_output = normalizeToolOutput(args.get("output"))
    title = str(args.get("title") or "Tool")
    tool_name = str(args.get("toolName") or inferToolNameFromLabel(title) or "Tool")
    failed = bool(args.get("failed"))
    return {
        "tone": args.get("tone") or "tool",
        "title": title,
        "body": renderTranscriptValue(normalized_output),
        "timestamp": args.get("timestamp", "saved"),
        "payload": {
            "type": "tool_result",
            "toolName": tool_name,
            "label": title,
            "outputs": [normalized_output],
            "failed": failed,
        },
    }


def mergeTranscriptEntry(entries: list[dict[str, Any]], entry: dict[str, Any]) -> list[dict[str, Any]]:
    if not entry.get("payload") or entry.get("tone") != "tool":
        return [*entries, entry]
    previous = entries[-1] if entries else None
    if not previous or not previous.get("payload") or previous.get("tone") != "tool":
        return [*entries, entry]
    if not _can_group_tool_entries(previous, entry):
        return [*entries, entry]
    merged_outputs = [*previous["payload"].get("outputs", []), *entry["payload"].get("outputs", [])]
    payload = {
        **previous["payload"],
        "label": previous["payload"].get("toolName"),
        "outputs": merged_outputs,
        "failed": bool(previous["payload"].get("failed")) or bool(entry["payload"].get("failed")),
    }
    merged = {
        **previous,
        "timestamp": entry.get("timestamp"),
        "title": previous["payload"].get("toolName"),
        "body": renderTranscriptValue(merged_outputs),
        "payload": payload,
    }
    return [*entries[:-1], merged]


def compactTranscriptEntries(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    current: list[dict[str, Any]] = []
    for entry in entries:
        current = mergeTranscriptEntry(current, entry)
    return current


def normalizeTranscriptBody(value: str) -> str:
    return re.sub(r"\n+$", "", re.sub(r"^\n+", "", value.replace("\r\n", "\n")))


def renderTranscriptValue(value: Any) -> str:
    if isinstance(value, str):
        return normalizeTranscriptBody(value)
    try:
        return json.dumps(value, indent=2, ensure_ascii=False)
    except Exception:
        return normalizeTranscriptBody(str(value))


def renderCompactValue(value: Any) -> str:
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    except Exception:
        return str(value)


def parseMaybeJson(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    trimmed = value.strip()
    if not trimmed or (not trimmed.startswith("{") and not trimmed.startswith("[")):
        return value
    try:
        return json.loads(trimmed)
    except Exception:
        return value


def normalizeToolOutput(value: Any) -> Any:
    return parseMaybeJson(value)


def isReadPayload(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    file_record = value.get("file")
    return (
        value.get("type") == "text"
        and isinstance(file_record, dict)
        and isinstance(file_record.get("filePath"), str)
        and isinstance(file_record.get("content"), str)
    )


def isGlobPayload(value: Any) -> bool:
    return isinstance(value, dict) and value.get("type") == "glob" and isinstance(value.get("matches"), list)


def isGrepPayload(value: Any) -> bool:
    return isinstance(value, dict) and value.get("type") == "grep" and isinstance(value.get("matches"), list)


def formatTranscriptToolLabel(name: str, input: Any) -> str:
    if not isinstance(input, dict):
        return name
    for key in ("file_path", "path", "cwd"):
        value = input.get(key)
        if isinstance(value, str) and value.strip():
            return f"{name}({os.path.basename(value)})"
    pattern = input.get("pattern")
    if isinstance(pattern, str) and pattern.strip():
        return f"{name}({pattern})"
    command = input.get("command")
    if isinstance(command, str) and command.strip():
        return f"{name}({_truncate_inline(command, 48)})"
    return name


def inferToolNameFromLabel(label: str | None) -> str | None:
    if not label:
        return None
    match = re.match(r"^([A-Za-z]+)", label.strip())
    return match.group(1) if match else None


def _can_group_tool_entries(left: dict[str, Any], right: dict[str, Any]) -> bool:
    left_payload = left.get("payload")
    right_payload = right.get("payload")
    if not isinstance(left_payload, dict) or not isinstance(right_payload, dict):
        return False
    if left_payload.get("failed") or right_payload.get("failed"):
        return False
    left_tool = _normalize_groupable_tool_name(str(left_payload.get("toolName") or ""))
    right_tool = _normalize_groupable_tool_name(str(right_payload.get("toolName") or ""))
    if not left_tool or left_tool != right_tool:
        return False
    validator = {"Read": isReadPayload, "Glob": isGlobPayload, "Grep": isGrepPayload}.get(left_tool)
    return bool(
        validator
        and all(validator(output) for output in left_payload.get("outputs", []))
        and all(validator(output) for output in right_payload.get("outputs", []))
    )


def _normalize_groupable_tool_name(tool_name: str) -> str | None:
    normalized = tool_name.strip().lower()
    if normalized == "read":
        return "Read"
    if normalized == "glob":
        return "Glob"
    if normalized == "grep":
        return "Grep"
    return None


def _truncate_inline(value: str, limit: int) -> str:
    collapsed = re.sub(r"\s+", " ", value).strip()
    if len(collapsed) <= limit:
        return collapsed
    return f"{collapsed[: limit - 1]}\u2026"


