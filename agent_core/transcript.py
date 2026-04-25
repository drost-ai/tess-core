from __future__ import annotations

import json
from typing import Any


def buildTranscriptSnapshot(session: dict[str, Any], limit: int | None = None) -> list[dict[str, Any]]:
    messages = session.get("messages", [])
    if limit:
        messages = messages[-limit:]
    entries = []
    for message in messages:
        content = message.get("content")
        if isinstance(content, str):
            entries.append(makeTextEntry({"tone": message.get("role"), "body": content}))
        elif isinstance(content, list):
            for block in content:
                if block.get("type") == "text":
                    entries.append(makeTextEntry({"tone": message.get("role"), "body": block.get("text", "")}))
                elif block.get("type") in ("tool_use", "tool_result"):
                    entries.append(makeToolEntry({"name": block.get("name") or block.get("tool_use_id"), "input": block.get("input"), "output": block.get("content"), "error": block.get("is_error")}))
    return compactTranscriptEntries(entries)


def makeTextEntry(args: dict[str, Any]) -> dict[str, Any]:
    return {"tone": args.get("tone", "assistant"), "body": normalizeTranscriptBody(str(args.get("body", "")))}


def makeToolEntry(args: dict[str, Any]) -> dict[str, Any]:
    return {"tone": "tool", "label": formatTranscriptToolLabel(str(args.get("name", "tool")), args.get("input")), "body": renderTranscriptValue(args.get("output")), "error": bool(args.get("error"))}


def mergeTranscriptEntry(entries: list[dict[str, Any]], entry: dict[str, Any]) -> list[dict[str, Any]]:
    if entries and entries[-1].get("tone") == entry.get("tone") == "assistant":
        entries[-1]["body"] = f"{entries[-1].get('body', '')}\n{entry.get('body', '')}".strip()
        return entries
    return [*entries, entry]


def compactTranscriptEntries(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for entry in entries:
        out = mergeTranscriptEntry(out, entry)
    return out


def normalizeTranscriptBody(value: str) -> str:
    return value.strip()


def renderTranscriptValue(value: Any) -> str:
    if isinstance(value, str):
        parsed = parseMaybeJson(value)
        if parsed is not value:
            return renderCompactValue(normalizeToolOutput(parsed))
        return value
    return renderCompactValue(value)


def renderCompactValue(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return json.dumps(value, indent=2, ensure_ascii=False)


def parseMaybeJson(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except Exception:
        return value


def normalizeToolOutput(value: Any) -> Any:
    return value


def isReadPayload(value: Any) -> bool:
    return isinstance(value, dict) and value.get("type") == "text" and isinstance(value.get("file"), dict)


def isGlobPayload(value: Any) -> bool:
    return isinstance(value, dict) and value.get("type") == "glob"


def isGrepPayload(value: Any) -> bool:
    return isinstance(value, dict) and value.get("type") == "grep"


def formatTranscriptToolLabel(name: str, input: Any) -> str:
    if input is None:
        return name
    if isinstance(input, dict):
        if input.get("file_path"):
            return f"{name} {input['file_path']}"
        if input.get("command"):
            return f"{name} {input['command']}"
    return name
