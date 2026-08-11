from __future__ import annotations
from .._utils import drop_none as _without_none

import re
from typing import Any


def summarize_output_manifest(text: str, filePath: str | None = None, maxExcerptChars: int = 1200) -> dict[str, Any]:
    return _without_none(
        {
            "filePath": filePath,
            "bytes": len(text.encode()),
            "lineCount": 0 if text == "" else len(re.split(r"\r?\n", text)),
            "truncated": len(text) > maxExcerptChars,
            "excerpt": text[:maxExcerptChars],
        }
    )


def create_task_execution_descriptor(record: dict[str, Any], output: str = "", activities: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    task_id = record.get("taskId") or record.get("id")
    return _without_none(
        {
            "id": task_id,
            "kind": "task",
            "title": f"Background task {task_id}" if task_id else "Background task",
            "status": record.get("status", "unknown"),
            "cwd": record.get("cwd"),
            "command": record.get("command"),
            "startedAt": record.get("startedAt"),
            "finishedAt": record.get("finishedAt") or record.get("stoppedAt"),
            "output": summarize_output_manifest(output, record.get("outputFile")),
            "activities": activities or [],
            "metadata": _without_none(
                {
                    "pid": record.get("pid"),
                    "runner_pid": record.get("runnerPid"),
                    "output_file": record.get("outputFile"),
                }
            ),
        }
    )


def create_workspace_execution_descriptor(args: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
    args = {**(args or {}), **kwargs}
    workspace = args.get("workspace") if isinstance(args.get("workspace"), dict) else args
    action = args.get("action") or "refresh"
    branch = args.get("branch") or workspace.get("gitBranch")
    descriptor_id = workspace.get("workspaceId") or workspace.get("cwd") or "workspace"
    workspace_name = workspace.get("workspaceName") or "Workspace"
    workspace_mode = workspace.get("mode") or "workspace"
    if action == "enter":
        title = f"Entered workspace {workspace_name}"
    elif action == "exit":
        title = f"Returned to workspace {workspace_name}"
    else:
        title = f"Refreshed workspace {workspace_name}"
    metadata = _without_none(
        {
            "workspace_id": workspace.get("workspaceId"),
            "workspace_mode": workspace.get("mode"),
            "workspace_root": workspace.get("workspaceRoot"),
            "git_root": workspace.get("gitRoot"),
            "git_branch": branch,
            "worktree_path": workspace.get("worktreePath"),
        }
    )
    activity_metadata = _without_none({"branch": branch, "created": args.get("created")})
    return _without_none(
        {
            "id": f"{descriptor_id}:{action}",
            "kind": "workspace",
            "title": title,
            "status": "completed",
            "cwd": workspace.get("cwd"),
            "activities": [
                _without_none(
                    {
                        "kind": "workspace",
                        "status": "info",
                        "summary": f"{workspace_mode} workspace at {workspace.get('cwd')}",
                        "metadata": activity_metadata,
                    }
                )
            ],
            "metadata": metadata,
        }
    )


def create_plugin_activity_descriptors(plugins: list[dict[str, Any]]) -> list[dict[str, Any]]:
    descriptors = []
    for plugin in plugins:
        plugin_id = plugin.get("id") or plugin.get("manifest", {}).get("id") or "plugin"
        diagnostics = plugin.get("diagnostics") if isinstance(plugin.get("diagnostics"), list) else []
        status = "error" if any(diagnostic.get("severity") == "error" for diagnostic in diagnostics if isinstance(diagnostic, dict)) else "warn" if diagnostics else "ok"
        tools = plugin.get("tools") if isinstance(plugin.get("tools"), list) else []
        commands = plugin.get("commands") if isinstance(plugin.get("commands"), list) else []
        mcp_servers = plugin.get("mcpServers") if isinstance(plugin.get("mcpServers"), list) else plugin.get("mcp_servers") if isinstance(plugin.get("mcp_servers"), list) else []
        descriptors.append(
            _without_none(
                {
                    "kind": "plugin",
                    "status": status,
                    "summary": f"{plugin_id} provides {len(tools)} tools, {len(commands)} commands, and {len(mcp_servers)} MCP servers",
                    "subjectId": plugin_id,
                    "metadata": _without_none({"source": plugin.get("source"), "capabilities": plugin.get("capabilities")}),
                }
            )
        )
    return descriptors


def create_mcp_activity_descriptors(servers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    descriptors = []
    for server in servers:
        name = server.get("name") or "mcp"
        state = server.get("state") or "disconnected"
        connected = bool(server.get("connected"))
        status = "ok" if connected else "warn" if state == "degraded" else "error"
        tools = _count_descriptor_items(server.get("tools"))
        resources = _count_descriptor_items(server.get("resources"))
        descriptors.append(
            _without_none(
                {
                    "kind": "mcp",
                    "status": status,
                    "summary": f"{name} is {state} with {tools} tools and {resources} resources",
                    "subjectId": name,
                    "metadata": _without_none(
                        {
                            "transport": server.get("transport"),
                            "error": server.get("error"),
                            "capabilities": server.get("capabilities"),
                            "channel_registration": server.get("channelRegistration") or server.get("channel_registration"),
                        }
                    ),
                }
            )
        )
    return descriptors


def _count_descriptor_items(value: Any) -> int:
    if isinstance(value, int):
        return value
    if isinstance(value, list):
        return len(value)
    if isinstance(value, dict):
        return len(value)
    return 0
