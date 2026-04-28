from __future__ import annotations

import asyncio
import stat
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from ._utils import now_iso
from .storage import getProjectRoot, getStateRoot, loadAuth, loadDaemonState, loadMcpServers, loadPlugins
from .workspace import captureWorkspaceSnapshot, summarizeWorkspaceCapabilities


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CHROME_NATIVE_HOST_NAME = "com.drost_ai.tess"
DEFAULT_CHROME_EXTENSION_ORIGIN = "chrome-extension://aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/"


async def pathExists(targetPath: str) -> bool:
    return os.path.exists(targetPath)


async def execText(command: str, args: list[str] | None = None, cwd: str | None = None) -> str:
    result = subprocess.run([command, *(args or [])], cwd=cwd, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
    return result.stdout.strip()


async def findCommandInPath(command: str) -> str | None:
    return shutil.which(command)


def getNpmCommand() -> str:
    return "npm.cmd" if os.name == "nt" else "npm"


def quoteShellArg(value: str) -> str:
    if re.match(r"^[A-Za-z0-9_./:@%+=,-]+$", value):
        return value
    return "'" + value.replace("'", "'\"'\"'") + "'"


def buildShellCommand(command: str, args: list[str] | None = None, env: dict[str, str] | None = None) -> str:
    prefix = ""
    if env:
        env_items = [(key, value) for key, value in env.items() if isinstance(value, str) and value]
        prefix = (" ".join(f"{key}={quoteShellArg(value)}" for key, value in env_items) + " ") if env_items else ""
    return prefix + " ".join([quoteShellArg(command), *[quoteShellArg(arg) for arg in (args or [])]])


async def spawnDetached(command: str, args: list[str] | None = None, cwd: str | None = None) -> None:
    proc = subprocess.Popen([command, *(args or [])], cwd=cwd, start_new_session=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    proc.poll()
    return None


async def newestMtimeInTree(rootPath: str) -> float | None:
    if not os.path.exists(rootPath):
        return None
    newest = None
    for root, _dirs, files in os.walk(rootPath):
        _dirs[:] = [directory for directory in _dirs if directory not in {"node_modules", ".git"}]
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
    return {"handled": True, "event": event}


def applyPlatformIntegrationMetadata(
    session: dict[str, Any],
    args: dict[str, Any] | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    args = {**(args or {}), **kwargs}
    metadata = session.setdefault("metadata", {})
    timestamp = args.get("updatedAt") or now_iso()
    tmux = args.get("tmux") or args.get("tmuxResult")
    if isinstance(tmux, dict):
        event = tmux.get("event") if isinstance(tmux.get("event"), dict) else {}
        data = event.get("data") if isinstance(event.get("data"), dict) else tmux.get("data") if isinstance(tmux.get("data"), dict) else {}
        if data.get("tmux_session_name"):
            metadata["tmuxSessionName"] = data.get("tmux_session_name")
        if data.get("tmux_window_name"):
            metadata["tmuxWindowName"] = data.get("tmux_window_name")
        if data.get("mode"):
            metadata["tmuxMode"] = data.get("mode")
        if event.get("event"):
            metadata["tmuxLastEvent"] = event.get("event")
        if tmux.get("handled") is not None:
            metadata["tmuxHandoffHandled"] = bool(tmux.get("handled"))
        metadata["tmuxUpdatedAt"] = timestamp
    ide = args.get("ide") or args.get("ideResult")
    if isinstance(ide, dict):
        target = ide.get("target") if isinstance(ide.get("target"), dict) else {}
        metadata["ideConnectionStatus"] = ide.get("status")
        if target.get("displayName"):
            metadata["ideName"] = target.get("displayName")
        if target.get("id"):
            metadata["ideTargetId"] = target.get("id")
        if target.get("command"):
            metadata["ideCommand"] = target.get("command")
        if target.get("source"):
            metadata["ideSource"] = target.get("source")
        if ide.get("reason"):
            metadata["ideConnectionReason"] = ide.get("reason")
        metadata["ideUpdatedAt"] = timestamp
    chrome = args.get("chrome") or args.get("chromeResult")
    if isinstance(chrome, dict):
        status = chrome.get("status") if isinstance(chrome.get("status"), dict) else chrome
        metadata["chromeIntegrationEnabled"] = bool(chrome.get("enabled") or status.get("enabled") or chrome.get("server"))
        metadata["chromeIntegrationMode"] = "mcp" if chrome.get("server") else metadata.get("chromeIntegrationMode") or ("native" if status.get("nativeHostInstalled") else "status")
        metadata["chromePlatform"] = status.get("platform")
        metadata["chromeBrowserBinary"] = status.get("browserBinary")
        metadata["chromeNativeHostModeSupported"] = status.get("nativeHostModeSupported")
        native_host = status.get("nativeHostManifest") if isinstance(status.get("nativeHostManifest"), dict) else {}
        if native_host.get("manifestPath"):
            metadata["chromeNativeHostManifestPath"] = native_host.get("manifestPath")
        metadata["chromeUpdatedAt"] = timestamp
    return metadata


async def detectTmuxAvailability(args: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
    args = {**(args or {}), **kwargs}
    runner = args.get("runner") or {}
    binary = await _runner_which(runner, "tmux")
    if not binary:
        return {"available": False}
    try:
        version = (await _runner_exec_text(runner, binary, ["-V"])).strip() or None
    except Exception:
        version = None
    return {"available": True, "binaryPath": binary, "version": version}


def resolveTmuxLaunchContextFromEnv(env: dict[str, str] | None = None) -> dict[str, Any] | None:
    env = env or os.environ
    cwd = env.get("TESS_LAUNCH_CWD")
    original = env.get("TESS_LAUNCH_ORIGINAL_CWD")
    if not cwd or not original:
        return None
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


async def resolveChromeCompanionServer(args: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
    status = await detectChromeCompanionStatus(args, **kwargs)
    enabled = bool(status.get("supported"))
    server = (
        {
            "name": "chrome_companion",
            "transport": "stdio",
            "commandOrUrl": sys.executable,
            "args": ["-m", "agent_core.chrome_companion", "mcp"],
            "updatedAt": now_iso(),
        }
        if enabled
        else None
    )
    return {"available": bool(status.get("supported") and status.get("browserBinary")), "enabled": enabled, "status": status, "server": server}


async def detectChromeCompanionStatus(args: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
    args = {**(args or {}), **kwargs}
    runner = args.get("runner") or {}
    system = args.get("platform") or sys.platform
    browser = await _find_chrome_binary(runner, system)
    supported = system.startswith("darwin") or system.startswith("linux") or system.startswith("win")
    status = {"enabled": bool(browser), "supported": supported, "browserBinary": browser, "nativeHostModeSupported": supported, "platform": system}
    if args.get("includeNativeHostManifest"):
        native_host = await inspectChromeNativeHostManifest({**args, "platform": system})
        status["nativeHostManifest"] = native_host
        status["nativeHostInstalled"] = native_host.get("installed")
    return status


def getChromeNativeHostManifestPath(args: dict[str, Any] | None = None, **kwargs: Any) -> str | None:
    args = {**(args or {}), **kwargs}
    if args.get("manifestPath"):
        return str(args["manifestPath"])
    system = args.get("platform") or sys.platform
    host_name = args.get("hostName") or DEFAULT_CHROME_NATIVE_HOST_NAME
    home = Path(args.get("home") or Path.home())
    if system.startswith("darwin"):
        return str(home / "Library" / "Application Support" / "Google" / "Chrome" / "NativeMessagingHosts" / f"{host_name}.json")
    if system.startswith("linux"):
        return str(home / ".config" / "google-chrome" / "NativeMessagingHosts" / f"{host_name}.json")
    if system.startswith("win"):
        local = os.environ.get("LOCALAPPDATA")
        base = Path(args.get("home") or local) if args.get("home") or local else None
        return str(base / "Google" / "Chrome" / "NativeMessagingHosts" / f"{host_name}.json") if base else None
    return None


async def buildChromeNativeHostManifest(args: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
    args = {**(args or {}), **kwargs}
    host_path = args.get("nativeHostPath") or await _chrome_native_host_wrapper_path(args)
    origins = args.get("allowedOrigins") or args.get("allowed_origins") or [DEFAULT_CHROME_EXTENSION_ORIGIN]
    return {
        "name": args.get("hostName") or DEFAULT_CHROME_NATIVE_HOST_NAME,
        "description": args.get("description") or "Tess local Chrome companion native host.",
        "path": str(host_path),
        "type": "stdio",
        "allowed_origins": [str(origin) for origin in origins if str(origin).strip()],
    }


async def inspectChromeNativeHostManifest(args: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
    args = {**(args or {}), **kwargs}
    system = args.get("platform") or sys.platform
    supported = system.startswith("darwin") or system.startswith("linux") or system.startswith("win")
    manifest_path = getChromeNativeHostManifestPath(args)
    expected = await buildChromeNativeHostManifest(args)
    manifest = None
    issues: list[str] = []
    if not supported:
        issues.append(f"Unsupported platform: {system}")
    if not manifest_path:
        issues.append("No native-host manifest path is available.")
    elif os.path.exists(manifest_path):
        try:
            with open(manifest_path, "r", encoding="utf-8") as handle:
                manifest = json.load(handle)
        except Exception as exc:
            issues.append(f"Manifest is not valid JSON: {exc}")
    else:
        issues.append("Native-host manifest is not installed.")
    if isinstance(manifest, dict):
        if manifest.get("name") != expected.get("name"):
            issues.append("Manifest host name does not match.")
        if manifest.get("type") != "stdio":
            issues.append("Manifest type must be stdio.")
        if not isinstance(manifest.get("allowed_origins"), list) or not manifest.get("allowed_origins"):
            issues.append("Manifest has no allowed_origins.")
        host_path = manifest.get("path")
        if not isinstance(host_path, str) or not host_path:
            issues.append("Manifest has no native host path.")
        elif not os.path.exists(host_path):
            issues.append("Native host executable is missing.")
    installed = bool(supported and manifest_path and isinstance(manifest, dict) and not issues)
    return {
        "supported": supported,
        "installed": installed,
        "manifestPath": manifest_path,
        "manifestExists": bool(manifest_path and os.path.exists(manifest_path)),
        "hostPath": (manifest or expected).get("path") if isinstance(manifest or expected, dict) else None,
        "hostExists": bool((manifest or expected).get("path") and os.path.exists((manifest or expected).get("path"))) if isinstance(manifest or expected, dict) else False,
        "manifest": manifest,
        "expectedManifest": expected,
        "issues": issues,
        "platform": system,
    }


async def installChromeNativeHostManifest(args: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
    args = {**(args or {}), **kwargs}
    status = await inspectChromeNativeHostManifest(args)
    if not status.get("supported") or not status.get("manifestPath"):
        return {**status, "status": "skipped", "reason": "; ".join(status.get("issues") or ["Native-host manifests are not supported."])}
    manifest = status["expectedManifest"]
    host_path = manifest["path"]
    if args.get("dryRun"):
        return {**status, "status": "planned", "manifest": manifest}
    if not args.get("nativeHostPath"):
        await _write_chrome_native_host_wrapper(host_path)
    os.makedirs(os.path.dirname(status["manifestPath"]), exist_ok=True)
    with open(status["manifestPath"], "w", encoding="utf-8") as handle:
        json.dump(manifest, handle, indent=2)
        handle.write("\n")
    installed = await inspectChromeNativeHostManifest({**args, "nativeHostPath": host_path})
    return {**installed, "status": "installed" if installed.get("installed") else "degraded", "manifest": manifest}


async def runChromeCompanionMcpServer(input_stream: Any | None = None, output_stream: Any | None = None) -> None:
    input_stream = input_stream or sys.stdin.buffer
    output_stream = output_stream or sys.stdout.buffer
    buffer = b""
    while True:
        chunk = await asyncio.to_thread(_read_stream_chunk, input_stream)
        if not chunk:
            return
        buffer += chunk
        while buffer:
            parsed = _try_read_rpc_message(buffer)
            if not parsed:
                break
            payload, buffer = parsed
            response = await handleChromeCompanionRpcPayload(payload)
            if response is not None:
                _write_rpc_message(output_stream, response)


async def runChromeNativeHost(input_stream: Any | None = None, output_stream: Any | None = None) -> None:
    input_stream = input_stream or sys.stdin.buffer
    output_stream = output_stream or sys.stdout.buffer
    while True:
        header = await asyncio.to_thread(_read_exact_stream, input_stream, 4)
        if not header:
            return
        length = int.from_bytes(header, "little")
        payload_bytes = await asyncio.to_thread(_read_exact_stream, input_stream, length)
        if payload_bytes is None:
            return
        payload = _parse_json_object(payload_bytes.decode("utf-8")) or {}
        response = {"type": "pong", "version": "0.1.0"} if payload.get("type") == "ping" else {"type": "status", "status": await detectChromeCompanionStatus()}
        _write_native_host_message(output_stream, response)


async def handleChromeCompanionRpcPayload(payload: str) -> dict[str, Any] | None:
    value = _parse_json_object(payload)
    if not value:
        return None
    method = value.get("method") if isinstance(value.get("method"), str) else ""
    request_id = value.get("id")
    if method == "notifications/initialized":
        return None
    if method == "initialize":
        return {"jsonrpc": "2.0", "id": request_id, "result": {"protocolVersion": "2024-11-05", "serverInfo": {"name": "tess-chrome-companion", "version": "0.1.0"}, "capabilities": {"tools": {}, "resources": {}}}}
    if method == "ping":
        return {"jsonrpc": "2.0", "id": request_id, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": request_id, "result": {"tools": [{"name": "chrome_status", "description": "Return local Chrome companion status and browser availability.", "inputSchema": {"type": "object", "properties": {}}}]}}
    if method == "tools/call":
        params = value.get("params") if isinstance(value.get("params"), dict) else {}
        name = params.get("name") if isinstance(params.get("name"), str) else ""
        if name != "chrome_status":
            return {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32602, "message": f"Unknown tool: {name}"}}
        return {"jsonrpc": "2.0", "id": request_id, "result": {"content": [{"type": "text", "text": json.dumps(await detectChromeCompanionStatus(), indent=2)}]}}
    if method == "resources/list":
        return {"jsonrpc": "2.0", "id": request_id, "result": {"resources": [{"uri": "tess://chrome/status", "name": "chrome_status", "mimeType": "application/json", "description": "Current local Chrome companion status."}]}}
    if method == "resources/read":
        params = value.get("params") if isinstance(value.get("params"), dict) else {}
        uri = params.get("uri") if isinstance(params.get("uri"), str) else ""
        if uri != "tess://chrome/status":
            return {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32602, "message": f"Unknown resource: {uri}"}}
        return {"jsonrpc": "2.0", "id": request_id, "result": {"contents": [{"uri": uri, "mimeType": "application/json", "text": json.dumps(await detectChromeCompanionStatus(), indent=2)}]}}
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32601, "message": f"Method not implemented: {method}"}}


async def inspectInstallState(args: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
    args = {**(args or {}), **kwargs}
    runner = args.get("runner") or {}
    repo_root = Path(args.get("repoRoot") or REPO_ROOT)
    pyproject = repo_root / "pyproject.toml"
    version = _read_pyproject_version(pyproject)
    bundled_version = _read_optional_pyproject_version(Path(args.get("bundledPyprojectPath") or repo_root / "package" / "pyproject.toml"))
    package_dir = Path(args.get("distPath") or repo_root / "agent_core")
    source_dir = Path(args.get("sourcePath") or repo_root / "agent_core")
    dist_exists = package_dir.exists()
    dist_stale = await _is_dist_stale(source_dir, package_dir) if dist_exists else True
    global_bin = args.get("globalBinPath") or await _runner_which(runner, "agent-core") or await _runner_which(runner, "tess")
    repo_real = _safe_realpath(repo_root)
    global_package_path = args.get("globalPackagePath") or await _resolve_global_python_package_path(args, runner, repo_root, global_bin)
    global_package_real = _safe_realpath(global_package_path) if global_package_path else None
    mode = "repo-only" if dist_exists else "uninstalled"
    if global_package_real and repo_real and os.path.normcase(global_package_real) == os.path.normcase(repo_real):
        mode = "linked-global"
    elif global_package_path:
        mode = "global-copy"
    return {
        "repoRoot": str(repo_root),
        "repoVersion": version,
        "bundledReleasedVersion": bundled_version,
        "distExists": dist_exists,
        "distStale": dist_stale,
        "distPath": str(package_dir),
        "sourcePath": str(source_dir),
        "globalPackagePath": global_package_path,
        "globalPackageRealPath": global_package_real,
        "globalBinPath": global_bin,
        "mode": mode,
        "installed": dist_exists,
        "python": True,
    }


async def runRepoInstall(args: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
    args = {**(args or {}), **kwargs}
    runner = args.get("runner") or {}
    repo_root = str(Path(args.get("repoRoot") or REPO_ROOT))
    target = args.get("target") or "latest"
    if target not in {"latest", "stable", "0.1.0"}:
        raise RuntimeError(f'This repo-backed install flow does not support target "{target}". Use "latest" or "stable".')
    state = await inspectInstallState(args)
    steps: list[dict[str, Any]] = []
    actions: list[dict[str, Any]] = []
    python_command = args.get("python") or sys.executable
    should_link = bool(args.get("force")) or state.get("mode") != "linked-global"
    if should_link:
        command_args = ["-m", "pip", "install", "-e", repo_root]
        await _runner_exec(runner, python_command, command_args, repo_root)
        steps.append({"step": "link", "status": "run", "detail": "Installed this checkout in editable mode as the active Python package."})
        actions.append({"type": "exec", "command": python_command, "args": command_args, "cwd": repo_root})
    else:
        steps.append({"step": "link", "status": "skipped", "detail": "`agent-core` is already linked to this checkout."})
    return {"ok": True, "installState": await inspectInstallState(args), "steps": steps, "actions": actions}


async def runRepoUpdate(args: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
    args = {**(args or {}), **kwargs}
    runner = args.get("runner") or {}
    repo_root = str(Path(args.get("repoRoot") or REPO_ROOT))
    state_before = await inspectInstallState(args)
    steps = []
    upstream_status = "skipped"
    upstream_detail = "No upstream git update was attempted."
    git_binary = await _runner_which(runner, "git")
    if git_binary and await _is_git_repo(runner, git_binary, repo_root):
        dirty = await _git_is_dirty(runner, git_binary, repo_root)
        if dirty:
            upstream_detail = "Working tree is dirty, so `git pull --ff-only` was skipped."
        elif await _has_git_upstream(runner, git_binary, repo_root):
            before_head = (await _runner_exec(runner, git_binary, ["rev-parse", "HEAD"], repo_root)).strip()
            await _runner_exec(runner, git_binary, ["pull", "--ff-only"], repo_root)
            after_head = (await _runner_exec(runner, git_binary, ["rev-parse", "HEAD"], repo_root)).strip()
            if before_head == after_head:
                upstream_status = "up-to-date"
                upstream_detail = "Git upstream was already up to date."
            else:
                upstream_status = "updated"
                upstream_detail = "Pulled the latest fast-forward changes from git upstream."
                steps.append({"step": "git_pull", "status": "run", "detail": upstream_detail})
        else:
            upstream_detail = "No git upstream is configured for this checkout."
    else:
        upstream_detail = "This checkout is not a git repo with an available git binary."
    install = await runRepoInstall({**args, "force": False, "target": "latest"})
    steps.extend(install.get("steps") or [])
    state_after = await inspectInstallState(args)
    return {"ok": True, "installStateBefore": state_before, "installStateAfter": state_after, "steps": steps, "upstreamStatus": upstream_status, "upstreamDetail": upstream_detail, "actions": []}


async def collectDoctorReport(cwd: str, args: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
    args = {**(args or {}), **kwargs}
    state_root = getStateRoot()
    project_root = getProjectRoot(cwd)
    auth = await loadAuth()
    install = await inspectInstallState(args)
    tmux = await detectTmuxAvailability(args)
    ide_targets = await detectIdeTargets(args)
    chrome = await detectChromeCompanionStatus({**args, "includeNativeHostManifest": True})
    daemon = await loadDaemonState()
    workspace = await captureWorkspaceSnapshot(cwd)
    capabilities = summarizeWorkspaceCapabilities(workspace)
    mcp_records = []
    plugin_records = []
    for scope in ("user", "project", "local"):
        mcp_records.extend(await loadMcpServers(scope, cwd))
        plugin_records.extend(await loadPlugins(scope, cwd))
    checks = [
        _doctor_check("auth", "pass" if _has_auth(auth) else "warn", "Provider authentication is configured." if _has_auth(auth) else "No provider credential is configured.", "Set provider credentials or login before running model calls."),
        _doctor_check("dist", "pass" if install.get("distExists") and not install.get("distStale") else "warn" if install.get("distExists") else "fail", "Python package files are present and current." if install.get("distExists") and not install.get("distStale") else "Python package files are present but may need reinstall." if install.get("distExists") else "Python package files are missing.", "Run `python -m pip install -e .` from this repo."),
        _doctor_check("global_link", "pass" if install.get("mode") == "linked-global" else "warn", "`agent-core` is linked to this repo." if install.get("mode") == "linked-global" else "`agent-core` is not linked to this checkout as the active command.", "Run `python -m pip install -e .` from this repo.", {"mode": install.get("mode"), "globalBinPath": install.get("globalBinPath"), "globalPackagePath": install.get("globalPackagePath")}),
        _doctor_check("package", "pass" if install.get("distExists") else "fail", "Python package files are present." if install.get("distExists") else "Python package files are missing.", "Run from the repository root or reinstall the package."),
        _doctor_check("tmux", "pass" if tmux.get("available") else "warn", "tmux is available." if tmux.get("available") else "tmux is not available on PATH.", "Install tmux to use tmux startup flows.", {"binaryPath": tmux.get("binaryPath"), "version": tmux.get("version")}),
        _doctor_check("ide", "pass" if len(ide_targets) == 1 else "warn", f"Exactly one IDE target is available: {ide_targets[0]['displayName']}." if len(ide_targets) == 1 else "No supported IDE targets were detected." if not ide_targets else f"Multiple IDE targets are available: {', '.join(target['displayName'] for target in ide_targets)}.", "Expose exactly one supported IDE CLI on PATH for predictable IDE auto-connect.", {"targets": ide_targets}),
        _doctor_check("chrome", "pass" if chrome.get("supported") else "warn", "Chrome companion mode is supported on this platform." if chrome.get("supported") else "Chrome companion mode is not supported on this platform.", "Use Chrome companion mode only on supported platforms.", chrome),
        _doctor_check(
            "chrome_native_host",
            "pass" if (chrome.get("nativeHostManifest") or {}).get("installed") else "warn",
            "Chrome native-host manifest is installed." if (chrome.get("nativeHostManifest") or {}).get("installed") else "Chrome native-host manifest is not installed.",
            "Run the native-host install helper when Chrome extension bridge mode is required.",
            chrome.get("nativeHostManifest") or {},
        ),
        _doctor_check("daemon", _daemon_check_status(daemon), _daemon_check_message(daemon), "Start the task supervisor daemon when background task, monitor, or bridge features are needed.", {"phase": (daemon or {}).get("phase"), "pid": (daemon or {}).get("pid"), "socketPath": (daemon or {}).get("socketPath"), "updatedAt": (daemon or {}).get("updatedAt")}),
        _doctor_check("git", "pass" if capabilities.get("hasGit") else "warn", "A git project root is available." if capabilities.get("hasGit") else "This directory is not inside a git repository.", "Run inside a git repository for worktree and PR-aware flows.", {"projectRoot": project_root}),
        _doctor_check("workspace", "pass" if capabilities.get("hasGit") or workspace.get("mode") else "warn", f"Workspace {workspace.get('workspaceName')} is running in {workspace.get('mode')} mode.", "Use worktree mode when isolated execution is required.", {"workspaceId": workspace.get("workspaceId"), "workspaceRoot": workspace.get("workspaceRoot"), "mode": workspace.get("mode")}),
        _doctor_check("extensions", "pass", f"Loaded config includes {len(mcp_records)} MCP server declarations and {len(plugin_records)} plugin records.", details={"mcpCount": len(mcp_records), "enabledPluginCount": len([record for record in plugin_records if record.get('enabled')]), "totalPluginCount": len(plugin_records)}),
    ]
    return {"healthy": all(check["status"] != "fail" for check in checks), "ok": all(check["status"] != "fail" for check in checks), "cwd": cwd, "projectRoot": project_root, "stateRoot": state_root, "install": install, "checks": checks}


def _daemon_check_status(daemon: dict[str, Any] | None) -> str:
    if not daemon:
        return "warn"
    pid = daemon.get("pid")
    if isinstance(pid, int) and not _is_pid_alive(pid):
        return "warn"
    return "pass" if daemon.get("phase") in {"running", "recovering"} or daemon.get("status") == "running" else "warn"


def _daemon_check_message(daemon: dict[str, Any] | None) -> str:
    if not daemon:
        return "Task supervisor daemon is not currently running."
    pid = daemon.get("pid")
    if isinstance(pid, int) and not _is_pid_alive(pid):
        return f"Task supervisor daemon state references stale pid {pid}."
    phase = daemon.get("phase") or daemon.get("status") or "unknown"
    return f"Task supervisor daemon state is {phase}."


def _is_pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


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


async def _is_dist_stale(source_path: Path, dist_path: Path) -> bool:
    dist_mtime = await newestMtimeInTree(str(dist_path))
    if not dist_mtime:
        return True
    source_mtime = await newestMtimeInTree(str(source_path))
    if not source_mtime:
        return False
    return source_mtime > dist_mtime


async def _resolve_global_python_package_path(
    args: dict[str, Any],
    runner: dict[str, Any],
    repo_root: Path,
    global_bin: str | None,
) -> str | None:
    if not global_bin and not args.get("discoverGlobalPackage"):
        return None
    python_command = args.get("python") or sys.executable
    package_name = args.get("packageName") or "agent-core-python"
    try:
        output = await _runner_exec(runner, python_command, ["-m", "pip", "show", package_name], str(repo_root))
    except Exception:
        return None
    fields = _parse_pip_show(output)
    direct_url = fields.get("editable-project-location") or fields.get("location")
    return direct_url or None


def _parse_pip_show(output: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for line in str(output or "").splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        fields[key.strip().lower()] = value.strip()
    return fields


async def _chrome_native_host_wrapper_path(args: dict[str, Any]) -> str:
    system = args.get("platform") or sys.platform
    suffix = ".cmd" if system.startswith("win") else ""
    root = Path(args.get("stateRoot") or getStateRoot()) / "chrome-native-host"
    return str(root / f"{args.get('hostName') or DEFAULT_CHROME_NATIVE_HOST_NAME}{suffix}")


async def _write_chrome_native_host_wrapper(host_path: str) -> None:
    os.makedirs(os.path.dirname(host_path), exist_ok=True)
    if host_path.endswith(".cmd"):
        body = f'@echo off\r\n"{sys.executable}" -m agent_core.chrome_companion native %*\r\n'
    else:
        body = f'#!/bin/sh\nexec {quoteShellArg(sys.executable)} -m agent_core.chrome_companion native "$@"\n'
    with open(host_path, "w", encoding="utf-8", newline="" if host_path.endswith(".cmd") else None) as handle:
        handle.write(body)
    if not host_path.endswith(".cmd"):
        current = os.stat(host_path).st_mode
        os.chmod(host_path, current | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _safe_realpath(path: str | Path | None) -> str | None:
    if not path:
        return None
    try:
        return os.path.realpath(str(path))
    except Exception:
        return str(path)


async def _is_git_repo(runner: dict[str, Any], git_binary: str, cwd: str) -> bool:
    try:
        await _runner_exec(runner, git_binary, ["rev-parse", "--show-toplevel"], cwd)
        return True
    except Exception:
        return False


async def _has_git_upstream(runner: dict[str, Any], git_binary: str, cwd: str) -> bool:
    try:
        await _runner_exec(runner, git_binary, ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"], cwd)
        return True
    except Exception:
        return False


async def _git_is_dirty(runner: dict[str, Any], git_binary: str, cwd: str) -> bool:
    porcelain = await _runner_exec(runner, git_binary, ["status", "--porcelain"], cwd)
    return bool(str(porcelain).strip())


def _read_stream_chunk(stream: Any) -> bytes:
    reader = getattr(stream, "read1", None)
    chunk = reader(4096) if callable(reader) else stream.read(4096)
    if isinstance(chunk, str):
        return chunk.encode("utf-8")
    return chunk or b""


def _read_exact_stream(stream: Any, size: int) -> bytes | None:
    buffer = b""
    while len(buffer) < size:
        chunk = stream.read(size - len(buffer))
        if isinstance(chunk, str):
            chunk = chunk.encode("utf-8")
        if not chunk:
            return None
        buffer += chunk
    return buffer


def _try_read_rpc_message(buffer: bytes) -> tuple[str, bytes] | None:
    header_index = buffer.find(b"\r\n\r\n")
    if header_index == -1:
        return None
    header = buffer[:header_index].decode("utf-8", errors="replace")
    match = re.search(r"Content-Length:\s*(\d+)", header, re.IGNORECASE)
    if not match:
        return "", buffer[header_index + 4 :]
    length = int(match.group(1))
    start = header_index + 4
    if len(buffer) < start + length:
        return None
    return buffer[start : start + length].decode("utf-8"), buffer[start + length :]


def _write_rpc_message(stream: Any, value: dict[str, Any]) -> None:
    payload = json.dumps(value).encode("utf-8")
    stream.write(f"Content-Length: {len(payload)}\r\n\r\n".encode("utf-8") + payload)
    flush = getattr(stream, "flush", None)
    if callable(flush):
        flush()


def _parse_json_object(value: str) -> dict[str, Any] | None:
    try:
        parsed = json.loads(value)
    except Exception:
        return None
    return parsed if isinstance(parsed, dict) else None


def _write_native_host_message(stream: Any, message: dict[str, Any]) -> None:
    payload = json.dumps(message).encode("utf-8")
    stream.write(len(payload).to_bytes(4, "little") + payload)
    flush = getattr(stream, "flush", None)
    if callable(flush):
        flush()


async def _find_chrome_binary(runner: dict[str, Any], system: str) -> str | None:
    commands = ("chrome", "chrome.exe") if system.startswith("win") else ("google-chrome", "chromium", "chrome", "google-chrome-stable", "chromium-browser")
    for command in commands:
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
    if term_program == "windsurf":
        return {"id": "windsurf", "name": "windsurf", "displayName": "Windsurf", "command": "windsurf", "source": "env"}
    if env.get("CURSOR_TRACE_ID"):
        return {"id": "cursor", "name": "cursor", "displayName": "Cursor", "command": "cursor", "source": "env"}
    return None


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


def _read_pyproject_version(path: Path) -> str:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return "0.1.0"
    import re

    match = re.search(r'^version\s*=\s*"([^"]+)"', text, re.MULTILINE)
    return match.group(1) if match else "0.1.0"


def _read_optional_pyproject_version(path: Path) -> str | None:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    import re

    match = re.search(r'^version\s*=\s*"([^"]+)"', text, re.MULTILINE)
    return match.group(1) if match else None


def _has_auth(auth: dict[str, Any] | None) -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN") or os.environ.get("OPENAI_API_KEY") or (auth or {}).get("anthropicApiKey") or (auth or {}).get("anthropicAuthToken") or (auth or {}).get("openaiToken") or (auth or {}).get("apiKey") or (auth or {}).get("authToken"))


def _doctor_check(id: str, status: str, summary: str, remediation: str | None = None, details: dict[str, Any] | None = None) -> dict[str, Any]:
    record = {"id": id, "name": id, "status": status, "ok": status == "pass", "summary": summary}
    if remediation:
        record["remediation"] = remediation
    if details is not None:
        record["details"] = details
    return record
