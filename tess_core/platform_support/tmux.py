from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

from .commands import _runner_exec, _runner_exec_text, _runner_which, build_shell_command

async def maybe_exec_into_tmux(args: dict[str, Any] | None = None) -> dict[str, Any]:
    """Hand the launch over to a tmux session when the ``tmux`` option asks for one.

    Returns ``{"handled": False}``, with an ``error`` when it was asked to and could not.
    """
    args = dict(args or {})
    options = args.get("options") or {}
    launch = args.get("launch") or {}
    env = args.get("env") or os.environ
    runner = args.get("runner") or {}
    if not options.get("tmux") or env.get("TESS_TMUX_HANDOFF") == "1":
        return {"handled": False}
    if not launch.get("worktreePath"):
        return {"handled": False, "error": "`--tmux` requires `--worktree` so the assistant has a dedicated workspace to launch into."}
    tmux_binary = await _runner_which(runner, "tmux")
    if not tmux_binary:
        return {"handled": False, "error": "tmux is not installed or not on PATH."}
    requested_mode = options.get("tmux") if isinstance(options.get("tmux"), str) else "default"
    tmux_mode = "classic" if requested_mode == "classic" else "default"
    session_name = _sanitize_tmux_name(f"tess-{Path(launch['worktreePath']).name}") or "tess"
    window_name = _sanitize_tmux_name(Path(launch["worktreePath"]).name) or "tess"
    child_argv = _strip_tmux_launch_flags(args.get("argv") or [])
    command = build_shell_command(
        sys.executable,
        ["-m", "tess_core.process.daemon_worker", *child_argv],
        {
            "TESS_TMUX_HANDOFF": "1",
            "TESS_LAUNCH_CWD": launch.get("cwd") or os.getcwd(),
            "TESS_LAUNCH_ORIGINAL_CWD": launch.get("originalCwd") or launch.get("cwd") or os.getcwd(),
            "TESS_LAUNCH_WORKTREE_PATH": launch["worktreePath"],
            "TESS_TMUX_SESSION_NAME": session_name,
            "TESS_TMUX_WINDOW_NAME": window_name,
            "TESS_TMUX_MODE": tmux_mode,
        },
    )
    tmux_args = ["new-window", "-n", window_name, "-c", launch.get("cwd") or os.getcwd(), command] if env.get("TMUX") else ["new-session", "-A", "-s", session_name, "-c", launch.get("cwd") or os.getcwd(), command]
    await _runner_exec(runner, tmux_binary, tmux_args, launch.get("cwd"))
    event_name = "platform.tmux_window_created" if env.get("TMUX") else "platform.tmux_attached"
    event = {"event": event_name, "data": {"mode": tmux_mode, "tmux_session_name": session_name, "tmux_window_name": window_name, "cwd": launch.get("cwd") or os.getcwd()}}
    return {"handled": True, "event": event}

async def detect_tmux_availability(args: dict[str, Any] | None = None) -> dict[str, Any]:
    args = dict(args or {})
    runner = args.get("runner") or {}
    binary = await _runner_which(runner, "tmux")
    if not binary:
        return {"available": False}
    try:
        version = (await _runner_exec_text(runner, binary, ["-V"])).strip() or None
    except Exception:
        version = None
    return {"available": True, "binaryPath": binary, "version": version}

def resolve_tmux_launch_context_from_env(env: dict[str, str] | None = None) -> dict[str, Any] | None:
    """The launch context a tmux hand-off left in the environment, or ``None`` when there is none."""
    env = env or os.environ
    cwd = env.get("TESS_LAUNCH_CWD")
    original = env.get("TESS_LAUNCH_ORIGINAL_CWD")
    if not cwd or not original:
        return None
    return {"cwd": cwd, "originalCwd": original, "worktreePath": env.get("TESS_LAUNCH_WORKTREE_PATH"), "tmuxSessionName": env.get("TESS_TMUX_SESSION_NAME"), "tmuxWindowName": env.get("TESS_TMUX_WINDOW_NAME"), "tmuxMode": env.get("TESS_TMUX_MODE")}

def _strip_tmux_launch_flags(argv: list[str]) -> list[str]:
    output = []
    skip_next = False
    for i, arg in enumerate(argv):
        if skip_next:
            skip_next = False
            continue
        if arg in {"--tmux", "-w", "--worktree"}:
            if i + 1 < len(argv) and not argv[i + 1].startswith("-"):
                skip_next = True
            continue
        if arg.startswith("--tmux=") or arg.startswith("--worktree="):
            continue
        output.append(arg)
    return output

def _sanitize_tmux_name(value: str) -> str:
    import re

    return re.sub(r"(^-+|-+$)", "", re.sub(r"[^a-z0-9_-]+", "-", value.strip().lower()))[:48]
