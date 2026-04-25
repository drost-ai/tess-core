from __future__ import annotations

import re
from typing import Any


def _without_none(value: dict[str, Any]) -> dict[str, Any]:
    return {key: item for key, item in value.items() if item is not None}


def summarizeOutputManifest(text: str, filePath: str | None = None, maxExcerptChars: int = 1200) -> dict[str, Any]:
    return _without_none(
        {
            "filePath": filePath,
            "bytes": len(text.encode()),
            "lineCount": 0 if text == "" else len(re.split(r"\r?\n", text)),
            "truncated": len(text) > maxExcerptChars,
            "excerpt": text[:maxExcerptChars],
        }
    )


def createTaskExecutionDescriptor(record: dict[str, Any], output: str = "", activities: list[dict[str, Any]] | None = None) -> dict[str, Any]:
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
            "output": summarizeOutputManifest(output, record.get("outputFile")),
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


def createWorkspaceExecutionDescriptor(args: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
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


def createPluginActivityDescriptors(plugins: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{"kind": "plugin", "status": "ok", "summary": plugin.get("manifest", {}).get("id", "plugin")} for plugin in plugins]


def createMcpActivityDescriptors(servers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{"kind": "mcp", "status": "ok" if server.get("state") == "connected" else "warn", "summary": server.get("name", "mcp")} for server in servers]
