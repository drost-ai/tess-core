from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from .storage import getProjectRoot, getStateRoot, loadAuth, loadMcpServers, loadPlugins
from .workspace import captureWorkspaceSnapshot, summarizeWorkspaceCapabilities


REPO_ROOT = Path(__file__).resolve().parents[1]


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


def buildShellCommand(command: str, args: list[str] | None = None, env: dict[str, str] | None = None) -> str:
    prefix = ""
    if env:
        prefix = " ".join(f"{quoteShellArg(key)}={quoteShellArg(value)}" for key, value in env.items()) + " "
    return prefix + " ".join([quoteShellArg(command), *[quoteShellArg(arg) for arg in (args or [])]])


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
    args = {**(args or {}), **kwargs}
    options = args.get("options") or {}
    launch = args.get("launch") or {}
    env = args.get("env") or os.environ
    runner = args.get("runner") or {}
    if not options.get("tmux") or env.get("TESS_TMUX_HANDOFF") == "1":
        return {"handled": False, "executed": False}
    if not launch.get("worktreePath"):
        return {"handled": False, "executed": False, "error": "`--tmux` requires `--worktree` so the assistant has a dedicated workspace to launch into."}
    tmux_binary = await _runner_which(runner, "tmux")
    if not tmux_binary:
        return {"handled": False, "executed": False, "error": "tmux is not installed or not on PATH."}
    requested_mode = options.get("tmux") if isinstance(options.get("tmux"), str) else "default"
    tmux_mode = "classic" if requested_mode == "classic" else "default"
    session_name = _sanitize_tmux_name(f"tess-{Path(launch['worktreePath']).name}") or "tess"
    window_name = _sanitize_tmux_name(Path(launch["worktreePath"]).name) or "tess"
    child_argv = _strip_tmux_launch_flags(args.get("argv") or [])
    command = buildShellCommand(
        sys.executable,
        ["-m", "agent_core.daemon_worker", *child_argv],
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
    return {"handled": True, "executed": True, "event": event}


async def detectTmuxAvailability(args: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
    args = {**(args or {}), **kwargs}
    runner = args.get("runner") or {}
    binary = await _runner_which(runner, "tmux")
    if not binary:
        return {"available": False, "binaryPath": None, "path": None}
    try:
        version = (await _runner_exec_text(runner, binary, ["-V"])).strip() or None
    except Exception:
        version = None
    return {"available": True, "binaryPath": binary, "path": binary, "version": version}


def resolveTmuxLaunchContextFromEnv(env: dict[str, str] | None = None) -> dict[str, Any] | None:
    env = env or os.environ
    cwd = env.get("TESS_LAUNCH_CWD")
    original = env.get("TESS_LAUNCH_ORIGINAL_CWD")
    if not cwd or not original:
        return {"tmuxMode": "attached"} if env.get("TMUX") else None
    return {"cwd": cwd, "originalCwd": original, "worktreePath": env.get("TESS_LAUNCH_WORKTREE_PATH"), "tmuxSessionName": env.get("TESS_TMUX_SESSION_NAME"), "tmuxWindowName": env.get("TESS_TMUX_WINDOW_NAME"), "tmuxMode": env.get("TESS_TMUX_MODE")}


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
                targets[target_id] = {"id": target_id, "name": command, "displayName": display, "command": path, "source": "path"}
                break
    return list(targets.values())


async def maybeAutoConnectIde(cwd: str, args: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
    args = {**(args or {}), **kwargs}
    targets = await detectIdeTargets(args)
    if not targets:
        return {"status": "skipped", "connected": False, "reason": "No supported IDE targets were detected."}
    if len(targets) > 1:
        return {"status": "skipped", "connected": False, "reason": f"Multiple IDE targets were detected: {', '.join(target['displayName'] for target in targets)}.", "targets": targets}
    target = targets[0]
    if target.get("source") == "env":
        return {"status": "connected", "connected": True, "target": target}
    runner = args.get("runner") or {}
    try:
        await _runner_spawn_detached(runner, target["command"], ["-r", cwd], cwd)
    except Exception as exc:
        return {"status": "skipped", "connected": False, "reason": f"Failed to launch {target['displayName']}: {exc}", "target": target}
    return {"status": "connected", "connected": True, "target": target}


async def resolveChromeCompanionServer(args: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
    status = await detectChromeCompanionStatus(args, **kwargs)
    return {"available": bool(status.get("supported") and status.get("browserBinary")), "status": status}


async def detectChromeCompanionStatus(args: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
    args = {**(args or {}), **kwargs}
    runner = args.get("runner") or {}
    system = args.get("platform") or sys.platform
    browser = await _find_chrome_binary(runner, system)
    supported = system.startswith("darwin") or system.startswith("linux") or system.startswith("win")
    return {"enabled": bool(browser), "supported": supported, "browserBinary": browser, "nativeHostModeSupported": supported, "platform": system}


async def runChromeCompanionMcpServer() -> None:
    raise RuntimeError("Chrome companion MCP server is not bundled in the Python port.")


async def runChromeNativeHost() -> None:
    raise RuntimeError("Chrome native host is not bundled in the Python port.")


async def inspectInstallState(args: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
    args = {**(args or {}), **kwargs}
    runner = args.get("runner") or {}
    pyproject = REPO_ROOT / "pyproject.toml"
    version = _read_pyproject_version(pyproject)
    package_dir = REPO_ROOT / "agent_core"
    global_bin = await _runner_which(runner, "agent-core") or await _runner_which(runner, "tess")
    mode = "repo-only" if package_dir.exists() else "uninstalled"
    return {"repoRoot": str(REPO_ROOT), "repoVersion": version, "distExists": package_dir.exists(), "distStale": False, "globalBinPath": global_bin, "mode": mode, "installed": package_dir.exists(), "python": True}


async def runRepoInstall(args: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
    state = await inspectInstallState(args, **kwargs)
    return {"ok": True, "installState": state, "steps": [{"step": "python_package", "status": "skipped", "detail": "Python package is used directly from this checkout."}], "actions": []}


async def runRepoUpdate(args: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
    state = await inspectInstallState(args, **kwargs)
    return {"ok": True, "installStateBefore": state, "installStateAfter": state, "steps": [{"step": "git_pull", "status": "skipped", "detail": "Automatic update is not run by the Python platform layer."}], "upstreamStatus": "skipped", "actions": []}


async def collectDoctorReport(cwd: str, args: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
    args = {**(args or {}), **kwargs}
    state_root = getStateRoot()
    project_root = getProjectRoot(cwd)
    auth = await loadAuth()
    install = await inspectInstallState(args)
    tmux = await detectTmuxAvailability(args)
    ide_targets = await detectIdeTargets(args)
    chrome = await detectChromeCompanionStatus(args)
    workspace = await captureWorkspaceSnapshot(cwd)
    capabilities = summarizeWorkspaceCapabilities(workspace)
    mcp_records = []
    plugin_records = []
    for scope in ("user", "project", "local"):
        mcp_records.extend(await loadMcpServers(scope, cwd))
        plugin_records.extend(await loadPlugins(scope, cwd))
    checks = [
        _doctor_check("auth", "pass" if _has_auth(auth) else "warn", "Provider authentication is configured." if _has_auth(auth) else "No provider credential is configured.", "Set provider credentials or login before running model calls."),
        _doctor_check("package", "pass" if install.get("distExists") else "fail", "Python package files are present." if install.get("distExists") else "Python package files are missing.", "Run from the repository root or reinstall the package."),
        _doctor_check("tmux", "pass" if tmux.get("available") else "warn", "tmux is available." if tmux.get("available") else "tmux is not available on PATH.", "Install tmux to use tmux startup flows.", {"binaryPath": tmux.get("binaryPath"), "version": tmux.get("version")}),
        _doctor_check("ide", "pass" if len(ide_targets) == 1 else "warn", f"Exactly one IDE target is available: {ide_targets[0]['displayName']}." if len(ide_targets) == 1 else "No supported IDE targets were detected." if not ide_targets else f"Multiple IDE targets are available: {', '.join(target['displayName'] for target in ide_targets)}.", "Expose exactly one supported IDE CLI on PATH for predictable IDE auto-connect.", {"targets": ide_targets}),
        _doctor_check("chrome", "pass" if chrome.get("supported") else "warn", "Chrome companion mode is supported on this platform." if chrome.get("supported") else "Chrome companion mode is not supported on this platform.", "Use Chrome companion mode only on supported platforms.", chrome),
        _doctor_check("git", "pass" if capabilities.get("hasGit") else "warn", "A git project root is available." if capabilities.get("hasGit") else "This directory is not inside a git repository.", "Run inside a git repository for worktree and PR-aware flows.", {"projectRoot": project_root}),
        _doctor_check("workspace", "pass" if capabilities.get("hasGit") or workspace.get("mode") else "warn", f"Workspace {workspace.get('workspaceName')} is running in {workspace.get('mode')} mode.", "Use worktree mode when isolated execution is required.", {"workspaceId": workspace.get("workspaceId"), "workspaceRoot": workspace.get("workspaceRoot"), "mode": workspace.get("mode")}),
        _doctor_check("extensions", "pass", f"Loaded config includes {len(mcp_records)} MCP server declarations and {len(plugin_records)} plugin records.", details={"mcpCount": len(mcp_records), "enabledPluginCount": len([record for record in plugin_records if record.get('enabled')]), "totalPluginCount": len(plugin_records)}),
    ]
    return {"healthy": all(check["status"] != "fail" for check in checks), "ok": all(check["status"] != "fail" for check in checks), "cwd": cwd, "projectRoot": project_root, "stateRoot": state_root, "install": install, "checks": checks}


async def _runner_which(runner: dict[str, Any], command: str) -> str | None:
    fn = runner.get("which") if isinstance(runner, dict) else None
    if callable(fn):
        result = fn(command)
        if hasattr(result, "__await__"):
            result = await result
        return result
    return shutil.which(command)


async def _runner_exec(runner: dict[str, Any], command: str, args: list[str], cwd: str | None = None) -> str:
    fn = runner.get("exec") if isinstance(runner, dict) else None
    if callable(fn):
        result = fn(command, args, cwd)
        if hasattr(result, "__await__"):
            result = await result
        return result or ""
    return await execText(command, args, cwd)


async def _runner_exec_text(runner: dict[str, Any], command: str, args: list[str]) -> str:
    fn = runner.get("execText") if isinstance(runner, dict) else None
    if callable(fn):
        result = fn(command, args)
        if hasattr(result, "__await__"):
            result = await result
        return result or ""
    return await _runner_exec(runner, command, args)


async def _runner_spawn_detached(runner: dict[str, Any], command: str, args: list[str], cwd: str | None = None) -> int:
    fn = runner.get("spawnDetached") if isinstance(runner, dict) else None
    if callable(fn):
        result = fn(command, args, cwd)
        if hasattr(result, "__await__"):
            result = await result
        return int(result or 0)
    return await spawnDetached(command, args, cwd)


async def _find_chrome_binary(runner: dict[str, Any], system: str) -> str | None:
    for command in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser"):
        found = await _runner_which(runner, command)
        if found:
            return found
    if system.startswith("darwin"):
        app = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
        if os.path.exists(app):
            return app
    return None


def _detect_env_ide_target(env: dict[str, str]) -> dict[str, Any] | None:
    term_program = (env.get("TERM_PROGRAM") or "").lower()
    if term_program == "vscode":
        is_cursor = bool(env.get("CURSOR_TRACE_ID"))
        return {"id": "cursor" if is_cursor else "vscode", "name": "cursor" if is_cursor else "code", "displayName": "Cursor" if is_cursor else "VS Code", "command": "cursor" if is_cursor else "code", "source": "env"}
    if term_program == "cursor":
        return {"id": "cursor", "name": "cursor", "displayName": "Cursor", "command": "cursor", "source": "env"}
    return None


def _strip_tmux_launch_flags(argv: list[str]) -> list[str]:
    output = []
    skip_next = False
    for arg in argv:
        if skip_next:
            skip_next = False
            continue
        if arg == "--tmux":
            continue
        if arg.startswith("--tmux="):
            continue
        output.append(arg)
    return output


def _sanitize_tmux_name(value: str) -> str:
    import re

    return re.sub(r"(^-+|-+$)", "", re.sub(r"[^a-z0-9_-]+", "-", value.strip().lower()))[:48]


def _read_pyproject_version(path: Path) -> str:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return "0.1.0"
    import re

    match = re.search(r'^version\s*=\s*"([^"]+)"', text, re.MULTILINE)
    return match.group(1) if match else "0.1.0"


def _has_auth(auth: dict[str, Any] | None) -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN") or os.environ.get("OPENAI_API_KEY") or (auth or {}).get("anthropicApiKey") or (auth or {}).get("anthropicAuthToken") or (auth or {}).get("openaiToken") or (auth or {}).get("apiKey") or (auth or {}).get("authToken"))


def _doctor_check(id: str, status: str, summary: str, remediation: str | None = None, details: dict[str, Any] | None = None) -> dict[str, Any]:
    record = {"id": id, "name": id, "status": status, "ok": status == "pass", "summary": summary}
    if remediation:
        record["remediation"] = remediation
    if details is not None:
        record["details"] = details
    return record
