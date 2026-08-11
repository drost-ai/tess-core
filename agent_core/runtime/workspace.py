from __future__ import annotations

import asyncio
import os
import re
import subprocess
import uuid
from typing import Any

from ..storage import find_git_root


async def captureWorkspaceSnapshot(cwd: str, options: dict[str, Any] | None = None) -> dict[str, Any]:
    options = options or {}
    git_root = find_git_root(cwd)
    branch = None
    if git_root:
        try:
            branch = subprocess.run(["git", "branch", "--show-current"], cwd=cwd, text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL).stdout.strip() or None
        except Exception:
            branch = None
    root = git_root or cwd
    snapshot = {
        "workspaceId": _build_workspace_id(root),
        "workspaceName": os.path.basename(root),
        "workspaceRoot": root,
        "cwd": cwd,
        "mode": _resolve_workspace_mode(cwd=cwd, gitRoot=git_root, worktreePath=options.get("worktreePath"), originalCwd=options.get("originalCwd")),
        "gitRoot": git_root,
        "gitBranch": branch,
        "worktreePath": options.get("worktreePath"),
        "integrations": options.get("integrations"),
    }
    return {key: value for key, value in snapshot.items() if value is not None}


async def hydrateSessionWorkspaceMetadata(session: dict[str, Any], options: dict[str, Any] | None = None) -> None:
    snapshot = await captureWorkspaceSnapshot(session["cwd"], options)
    metadata = session.setdefault("metadata", {})
    for key in ("workspaceId", "workspaceName", "workspaceRoot", "mode", "gitRoot", "gitBranch", "prNumber", "prUrl"):
        meta_key = "workspaceMode" if key == "mode" else key
        metadata[meta_key] = snapshot.get(key)


async def createGitWorktree(args: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
    args = {**(args or {}), **kwargs}
    cwd = args["cwd"]
    git_root = find_git_root(cwd)
    if not git_root:
        raise RuntimeError("Current directory is not inside a git repository.")
    requested_name = args.get("requestedName") or args.get("name") or args.get("branch") or "tess"
    safe_name = _slugify_worktree_name(str(requested_name)) or f"tess-{uuid.uuid4().hex[:6]}"
    suffix = uuid.uuid4().hex[:6]
    target_path = args.get("path") or os.path.join(os.path.dirname(git_root), f"{os.path.basename(git_root)}-{safe_name}-{suffix}")
    branch_name = args.get("branchName") or f"tess/{safe_name}-{suffix}"
    if os.path.exists(target_path):
        raise RuntimeError(f"Worktree target already exists: {target_path}")
    if _git_branch_exists(git_root, branch_name):
        raise RuntimeError(f"Worktree branch already exists: {branch_name}")
    subprocess.run(["git", "worktree", "add", "-b", branch_name, target_path, "HEAD"], cwd=git_root, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
    return {"gitRoot": git_root, "targetPath": target_path, "branchName": branch_name, "requestedName": safe_name}


def createWorkspaceController(args: dict[str, Any] | None = None, **kwargs: Any) -> Any:
    args = {**(args or {}), **kwargs}
    session = args.get("session") or {"cwd": args.get("cwd"), "metadata": {"originalCwd": args.get("cwd")}}
    return _WorkspaceController(session, args.get("config") or {})


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
    integrations = snapshot.get("integrations") if isinstance(snapshot.get("integrations"), dict) else {}
    tmux = integrations.get("tmux") if isinstance(integrations.get("tmux"), dict) else {}
    ide = integrations.get("ide") if isinstance(integrations.get("ide"), dict) else {}
    chrome = integrations.get("chrome") if isinstance(integrations.get("chrome"), dict) else {}
    return {
        "hasGit": bool(snapshot.get("gitRoot")),
        "canCreateWorktree": bool(snapshot.get("gitRoot")),
        "hasWorktree": snapshot.get("mode") == "worktree",
        "tmuxAvailable": bool(tmux.get("available")),
        "ideTargets": ide.get("availableTargets") or [],
        "chromeSupported": bool(chrome.get("supported")),
    }


class _WorkspaceController:
    def __init__(self, session: dict[str, Any], config: dict[str, Any]):
        self.activeSession = session
        self.config = config
        self.snapshot = self._snapshot_from_session(session)

    def getSnapshot(self) -> dict[str, Any]:
        return self.snapshot

    def setSession(self, session: dict[str, Any]) -> None:
        self.activeSession = session
        self.snapshot = self._snapshot_from_session(session)

    async def refresh(self) -> dict[str, Any]:
        metadata = self.activeSession.setdefault("metadata", {})
        integrations = await _detect_workspace_integrations(self.activeSession)
        self.snapshot = await captureWorkspaceSnapshot(
            self.activeSession["cwd"],
            {
                "originalCwd": metadata.get("originalCwd"),
                "worktreePath": metadata.get("worktreePath"),
                "integrations": integrations,
            },
        )
        _apply_workspace_snapshot_to_session(self.activeSession, self.snapshot)
        return self.snapshot

    async def describe(self) -> dict[str, Any]:
        snapshot = await self.refresh()
        return {"snapshot": snapshot, "capabilities": summarizeWorkspaceCapabilities(snapshot), "integrations": snapshot.get("integrations") or {}}

    async def createWorktree(self, requestedName: str | None = None) -> dict[str, Any]:
        metadata = self.activeSession.setdefault("metadata", {})
        worktree = await createGitWorktree(cwd=self.activeSession["cwd"], name=requestedName)
        metadata["worktreePath"] = worktree["targetPath"]
        self.activeSession["cwd"] = worktree["targetPath"]
        snapshot = await self.refresh()
        return {"snapshot": snapshot, "worktree": worktree}

    async def exitWorktree(self) -> dict[str, Any]:
        metadata = self.activeSession.setdefault("metadata", {})
        self.activeSession["cwd"] = metadata.get("originalCwd")
        return await self.refresh()

    def _snapshot_from_session(self, session: dict[str, Any]) -> dict[str, Any]:
        metadata = session.setdefault("metadata", {})
        root = metadata.get("gitRoot") or session.get("cwd")
        return {
            "workspaceId": metadata.get("workspaceId") or _build_workspace_id(root),
            "workspaceName": metadata.get("workspaceName") or os.path.basename(root),
            "workspaceRoot": metadata.get("workspaceRoot") or root,
            "cwd": session.get("cwd"),
            "mode": metadata.get("workspaceMode")
            or _resolve_workspace_mode(cwd=session.get("cwd"), gitRoot=metadata.get("gitRoot"), worktreePath=metadata.get("worktreePath"), originalCwd=metadata.get("originalCwd")),
            "gitRoot": metadata.get("gitRoot"),
            "gitBranch": metadata.get("gitBranch"),
            "prNumber": metadata.get("prNumber"),
            "prUrl": metadata.get("prUrl"),
            "worktreePath": metadata.get("worktreePath"),
        }


def _apply_workspace_snapshot_to_session(session: dict[str, Any], snapshot: dict[str, Any]) -> None:
    metadata = session.setdefault("metadata", {})
    metadata["workspaceId"] = snapshot.get("workspaceId")
    metadata["workspaceMode"] = snapshot.get("mode")
    metadata["workspaceRoot"] = snapshot.get("workspaceRoot")
    metadata["workspaceName"] = snapshot.get("workspaceName")
    metadata["gitRoot"] = snapshot.get("gitRoot")
    metadata["gitBranch"] = snapshot.get("gitBranch")
    metadata["prNumber"] = snapshot.get("prNumber")
    metadata["prUrl"] = snapshot.get("prUrl")


async def _detect_workspace_integrations(session: dict[str, Any]) -> dict[str, Any]:
    from ..platform_support import detectChromeCompanionStatus, detectIdeTargets, detectTmuxAvailability

    metadata = session.setdefault("metadata", {})
    tmux, ide_targets, chrome = await asyncio.gather(
        detectTmuxAvailability(),
        detectIdeTargets(),
        detectChromeCompanionStatus(),
    )
    return {
        "tmux": {
            "available": tmux.get("available"),
            "sessionName": metadata.get("tmuxSessionName"),
            "windowName": metadata.get("tmuxWindowName"),
            "mode": metadata.get("tmuxMode"),
        },
        "ide": {
            "availableTargets": [target.get("displayName") for target in ide_targets],
            "connectedTarget": metadata.get("ideName"),
            "autoConnectable": len(ide_targets) == 1,
        },
        "chrome": {
            "enabled": bool(metadata.get("chromeIntegrationEnabled") or metadata.get("chromeIntegrationMode")),
            "supported": chrome.get("supported"),
            "browserBinary": chrome.get("browserBinary"),
            "nativeHostModeSupported": chrome.get("nativeHostModeSupported"),
            "mode": metadata.get("chromeIntegrationMode"),
        },
    }


def _resolve_workspace_mode(cwd: str | None, gitRoot: str | None = None, worktreePath: str | None = None, originalCwd: str | None = None, **_: Any) -> str:
    if cwd and worktreePath and os.path.abspath(cwd) == os.path.abspath(worktreePath):
        return "worktree"
    if gitRoot:
        return "repo"
    if cwd and originalCwd and worktreePath and os.path.abspath(originalCwd) != os.path.abspath(cwd):
        return "worktree"
    return "directory"


def _slugify_worktree_name(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.strip().lower()).strip("-")[:24]


def _git_branch_exists(git_root: str, branch_name: str) -> bool:
    result = subprocess.run(["git", "show-ref", "--verify", "--quiet", f"refs/heads/{branch_name}"], cwd=git_root, text=True)
    return result.returncode == 0


def _build_workspace_id(root_path: str | None) -> str:
    value = re.sub(r"[^a-zA-Z0-9]+", "_", root_path or "").strip("_").lower()
    return value or "workspace"
