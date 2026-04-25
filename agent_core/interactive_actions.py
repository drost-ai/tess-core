from __future__ import annotations

from typing import Any

from .config import normalizeModelName
from .transcript import buildTranscriptSnapshot


AVAILABLE_TUI_THEMES = ["tess", "copper", "ocean", "forest"]


def listSlashCommands(args: dict[str, Any] | None = None, **kwargs: Any) -> list[dict[str, Any]]:
    return [
        {"name": "/help", "description": "Show available commands"},
        {"name": "/model", "description": "Show or switch model"},
        {"name": "/permissions", "description": "Show or switch permission mode"},
        {"name": "/transcript", "description": "Render transcript"},
    ]


async def handleSlashCommand(args: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
    args = {**(args or {}), **kwargs}
    command = (args.get("command") or args.get("input") or "").strip()
    if command in ("/help", "help"):
        return {"outcome": "handled", "message": "\n".join(f"{item['name']} - {item['description']}" for item in listSlashCommands())}
    if command.startswith("/transcript"):
        return {"outcome": "handled", "entries": buildTranscriptSnapshot(args.get("session", {}))}
    return {"outcome": "pass"}


def compactModelName(model: str) -> str:
    return model.replace("claude-", "").replace("-2025-08-07", "")


def normalizeModel(inputModel: str) -> str:
    return normalizeModelName(inputModel)


def formatUsageSummary(usage: dict[str, Any] | None) -> str:
    if not usage:
        return "0 turns, 0 tools, 0 input, 0 output"
    return f"{usage.get('turns', 0)} turns, {usage.get('toolCalls', 0)} tools, {usage.get('inputTokens', 0)} input, {usage.get('outputTokens', 0)} output"


def formatUsageBreakdown(usage: dict[str, Any] | None) -> str:
    if not usage:
        return "No usage recorded."
    return "\n".join(f"{key}: {value}" for key, value in usage.items())
