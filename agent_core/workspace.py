from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from typing import Any

from .storage import findGitRoot


async def captureWorkspaceSnapshot(cwd: str, options: dict[str, Any] | None = None) -> dict[str, Any]:
    git_root = findGitRoot(cwd)
    branch = None
    if git_root:
        try:
            branch = subprocess.run(["git", "branch", "--show-current"], cwd=git_root, text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL).stdout.strip() or None
        except Exception:
            branch = None
    root = git_root or cwd
    return {
        "workspaceId": os.path.abspath(root).replace(os.sep, "_"),
        "workspaceName": os.path.basename(root),
        "workspaceRoot": root,
        "cwd": cwd,
        "mode": "repo" if git_root else "directory",
        "gitRoot": git_root,
        "gitBranch": branch,
    }


async def hydrateSessionWorkspaceMetadata(session: dict[str, Any], options: dict[str, Any] | None = None) -> None:
    snapshot = await captureWorkspaceSnapshot(session["cwd"], options)
    metadata = session.setdefault("metadata", {})
    for key in ("workspaceId", "workspaceName", "workspaceRoot", "mode", "gitRoot", "gitBranch"):
        meta_key = "workspaceMode" if key == "mode" else key
        if snapshot.get(key) is not None:
            metadata[meta_key] = snapshot[key]


async def createGitWorktree(args: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
    args = {**(args or {}), **kwargs}
    cwd = args["cwd"]
    branch = args.get("branch") or args.get("name") or "agent-worktree"
    path = args.get("path") or os.path.join(os.path.dirname(cwd), branch)
    subprocess.run(["git", "worktree", "add", "-b", branch, path], cwd=cwd, check=True)
    return {"path": path, "branch": branch}


def createWorkspaceController(args: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
    args = {**(args or {}), **kwargs}
    return {"cwd": args.get("cwd"), "createGitWorktree": createGitWorktree}


def parsePullRequestReference(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    match = re.search(r"(?:pull/|#)(\d+)|\b(\d+)\b", value)
    result: dict[str, Any] = {}
    if match:
        result["prNumber"] = int(match.group(1) or match.group(2))
    if "http://" in value or "https://" in value:
        result["prUrl"] = value.strip()
    return result


def summarizeWorkspaceCapabilities(snapshot: dict[str, Any]) -> dict[str, Any]:
    return {
        "hasGit": bool(snapshot.get("gitRoot")),
        "canCreateWorktree": bool(snapshot.get("gitRoot")),
        "hasWorktree": snapshot.get("mode") == "worktree",
        "tmuxAvailable": False,
        "ideTargets": [],
        "chromeSupported": False,
    }
