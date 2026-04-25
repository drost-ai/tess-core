from __future__ import annotations

from typing import Any


def summarizeOutputManifest(text: str, filePath: str | None = None) -> dict[str, Any]:
    return {"filePath": filePath, "bytes": len(text.encode()), "lineCount": len(text.splitlines()), "truncated": False, "excerpt": text[:1200]}


def createTaskExecutionDescriptor(record: dict[str, Any], output: str = "", activities: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {"id": record.get("taskId") or record.get("id"), "kind": "task", "title": record.get("title") or record.get("command") or "Task", "status": record.get("status", "unknown"), "cwd": record.get("cwd"), "command": record.get("command"), "startedAt": record.get("startedAt"), "finishedAt": record.get("finishedAt"), "output": summarizeOutputManifest(output, record.get("outputFile")), "activities": activities or []}


def createWorkspaceExecutionDescriptor(args: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
    args = {**(args or {}), **kwargs}
    return {"id": args.get("workspaceId") or args.get("cwd"), "kind": "workspace", "title": args.get("workspaceName") or "Workspace", "status": "completed", "cwd": args.get("cwd"), "metadata": args}


def createPluginActivityDescriptors(plugins: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{"kind": "plugin", "status": "ok", "summary": plugin.get("manifest", {}).get("id", "plugin")} for plugin in plugins]


def createMcpActivityDescriptors(servers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{"kind": "mcp", "status": "ok" if server.get("state") == "connected" else "warn", "summary": server.get("name", "mcp")} for server in servers]
