from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Any


async def pathExists(targetPath: str) -> bool:
    return os.path.exists(targetPath)


async def execText(command: str, args: list[str] | None = None, cwd: str | None = None) -> str:
    result = subprocess.run([command, *(args or [])], cwd=cwd, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
    return result.stdout


async def findCommandInPath(command: str) -> str | None:
    return shutil.which(command)


def getNpmCommand() -> str:
    return "npm.cmd" if os.name == "nt" else "npm"


def quoteShellArg(value: str) -> str:
    import shlex

    return shlex.quote(value)


def buildShellCommand(command: str, args: list[str] | None = None) -> str:
    return " ".join([quoteShellArg(command), *[quoteShellArg(arg) for arg in (args or [])]])


async def spawnDetached(command: str, args: list[str] | None = None, cwd: str | None = None) -> int:
    proc = subprocess.Popen([command, *(args or [])], cwd=cwd, start_new_session=True)
    return proc.pid


async def newestMtimeInTree(rootPath: str) -> float | None:
    newest = None
    for root, _dirs, files in os.walk(rootPath):
        for file in files:
            try:
                mtime = os.path.getmtime(os.path.join(root, file))
            except OSError:
                continue
            newest = mtime if newest is None else max(newest, mtime)
    return newest


async def maybeExecIntoTmux(args: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
    return {"executed": False, "reason": "tmux handoff is shell-specific in the Python port"}


async def detectTmuxAvailability() -> dict[str, Any]:
    return {"available": bool(shutil.which("tmux")), "path": shutil.which("tmux")}


def resolveTmuxLaunchContextFromEnv() -> dict[str, Any] | None:
    if not os.environ.get("TMUX"):
        return None
    return {"tmuxMode": "attached"}


async def detectIdeTargets() -> list[dict[str, Any]]:
    targets = []
    for name in ("code", "cursor"):
        path = shutil.which(name)
        if path:
            targets.append({"name": name, "command": path})
    return targets


async def maybeAutoConnectIde(cwd: str) -> dict[str, Any]:
    targets = await detectIdeTargets()
    return {"connected": False, "target": targets[0] if targets else None}


async def resolveChromeCompanionServer() -> dict[str, Any]:
    return {"available": False}


async def detectChromeCompanionStatus() -> dict[str, Any]:
    return {"enabled": False, "supported": False, "nativeHostModeSupported": False}


async def runChromeCompanionMcpServer() -> None:
    raise RuntimeError("Chrome companion MCP server is not bundled in the Python port.")


async def runChromeNativeHost() -> None:
    raise RuntimeError("Chrome native host is not bundled in the Python port.")


async def inspectInstallState() -> dict[str, Any]:
    return {"installed": True, "python": True}


async def runRepoInstall(args: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
    return {"ok": True, "actions": []}


async def runRepoUpdate() -> dict[str, Any]:
    return {"ok": True, "actions": []}


async def collectDoctorReport(cwd: str) -> dict[str, Any]:
    return {"cwd": cwd, "checks": [{"name": "python", "ok": True}], "ok": True}
