from __future__ import annotations

import os
from typing import Any

from .commands import _runner_spawn_detached, _runner_which

async def detectIdeTargets(args: dict[str, Any] | None = None, **kwargs: Any) -> list[dict[str, Any]]:
    args = {**(args or {}), **kwargs}
    runner = args.get("runner") or {}
    env = args.get("env") or os.environ
    targets: dict[str, dict[str, Any]] = {}
    env_target = _detect_env_ide_target(env)
    if env_target:
        targets[env_target["id"]] = env_target
    for target_id, display, commands in (("vscode", "VS Code", ["code"]), ("cursor", "Cursor", ["cursor"]), ("windsurf", "Windsurf", ["windsurf"])):
        if target_id in targets:
            continue
        for command in commands:
            path = await _runner_which(runner, command)
            if path:
                targets[target_id] = {"id": target_id, "displayName": display, "command": path, "source": "path"}
                break
    return list(targets.values())

async def maybeAutoConnectIde(cwd: str, args: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
    args = {**(args or {}), **kwargs}
    targets = await detectIdeTargets(args)
    if not targets:
        return {"status": "skipped", "reason": "No supported IDE targets were detected."}
    if len(targets) > 1:
        return {"status": "skipped", "reason": f"Multiple IDE targets were detected: {', '.join(target['displayName'] for target in targets)}."}
    target = targets[0]
    if target.get("source") == "env":
        return {"status": "connected", "target": target}
    runner = args.get("runner") or {}
    try:
        await _runner_spawn_detached(runner, target["command"], ["-r", cwd], cwd)
    except Exception as exc:
        return {"status": "skipped", "reason": f"Failed to launch {target['displayName']}: {exc}"}
    return {"status": "connected", "target": target}

def _detect_env_ide_target(env: dict[str, str]) -> dict[str, Any] | None:
    term_program = (env.get("TERM_PROGRAM") or "").lower()
    if term_program == "vscode":
        is_cursor = bool(env.get("CURSOR_TRACE_ID"))
        return {"id": "cursor" if is_cursor else "vscode", "name": "cursor" if is_cursor else "code", "displayName": "Cursor" if is_cursor else "VS Code", "command": "cursor" if is_cursor else "code", "source": "env"}
    if term_program == "cursor":
        return {"id": "cursor", "name": "cursor", "displayName": "Cursor", "command": "cursor", "source": "env"}
    if term_program == "windsurf":
        return {"id": "windsurf", "name": "windsurf", "displayName": "Windsurf", "command": "windsurf", "source": "env"}
    if env.get("CURSOR_TRACE_ID"):
        return {"id": "cursor", "name": "cursor", "displayName": "Cursor", "command": "cursor", "source": "env"}
    return None
