from __future__ import annotations

import os
from typing import Any

from ..runtime.workspace import capture_workspace_snapshot, summarize_workspace_capabilities
from ..storage import get_project_root, get_state_root, list_live_daemon_states, load_auth, load_mcp_servers, load_plugins
from .chrome import detect_chrome_companion_status
from .ide import detect_ide_targets
from .install import inspect_install_state
from .session_hygiene import build_session_hygiene_report
from .tmux import detect_tmux_availability

async def collect_doctor_report(cwd: str, args: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
    """Collect a diagnostic report for a workspace.

    Covers the install, stored credentials, the task daemon, detected tools, session
    hygiene, MCP servers and plugins.
    """
    args = {**(args or {}), **kwargs}
    state_root = get_state_root()
    project_root = get_project_root(cwd)
    auth = await load_auth()
    install = await inspect_install_state(args)
    tmux = await detect_tmux_availability(args)
    ide_targets = await detect_ide_targets(args)
    chrome = await detect_chrome_companion_status({**args, "includeNativeHostManifest": True})
    live_daemons = await list_live_daemon_states()
    daemon = live_daemons[0] if live_daemons else None
    workspace = await capture_workspace_snapshot(cwd)
    capabilities = summarize_workspace_capabilities(workspace)
    session_hygiene = await build_session_hygiene_report()
    mcp_records = []
    plugin_records = []
    for scope in ("user", "project", "local"):
        mcp_records.extend(await load_mcp_servers(scope, cwd))
        plugin_records.extend(await load_plugins(scope, cwd))
    checks = [
        _doctor_check("auth", "pass" if _has_auth(auth) else "warn", "Provider authentication is configured." if _has_auth(auth) else "No provider credential is configured.", "Set provider credentials or login before running model calls."),
        _doctor_check("dist", "pass" if install.get("distExists") and not install.get("distStale") else "warn" if install.get("distExists") else "fail", "Python package files are present and current." if install.get("distExists") and not install.get("distStale") else "Python package files are present but may need reinstall." if install.get("distExists") else "Python package files are missing.", "Run `python -m pip install -e .` from this repo."),
        _doctor_check("global_link", "pass" if install.get("mode") == "linked-global" else "warn", "`tess-core` is linked to this repo." if install.get("mode") == "linked-global" else "`tess-core` is not linked to this checkout as the active command.", "Run `python -m pip install -e .` from this repo.", {"mode": install.get("mode"), "globalBinPath": install.get("globalBinPath"), "globalPackagePath": install.get("globalPackagePath")}),
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
        _doctor_check(
            "session_hygiene",
            "warn"
            if session_hygiene.get("damagedTimestampCount")
            or session_hygiene.get("staleMarkerSessionCount")
            or session_hygiene.get("malformedRecords")
            or session_hygiene.get("oversizedRecords")
            else "pass",
            (
                f"Session store has {session_hygiene.get('sessionCount', 0)} valid records, "
                f"{session_hygiene.get('staleMarkerSessionCount', 0)} stale marker sessions, and "
                f"{session_hygiene.get('damagedTimestampCount', 0)} proven timestamp repair candidates."
            ),
            "Run repair_session_timestamps() for an exact dry-run; stop the daemon before explicitly applying repairs.",
            session_hygiene,
        ),
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

def _has_auth(auth: dict[str, Any] | None) -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN") or os.environ.get("OPENAI_API_KEY") or (auth or {}).get("anthropicApiKey") or (auth or {}).get("anthropicAuthToken") or (auth or {}).get("openaiToken") or (auth or {}).get("apiKey") or (auth or {}).get("authToken"))

def _doctor_check(id: str, status: str, summary: str, remediation: str | None = None, details: dict[str, Any] | None = None) -> dict[str, Any]:
    record = {"id": id, "name": id, "status": status, "ok": status == "pass", "summary": summary}
    if remediation:
        record["remediation"] = remediation
    if details is not None:
        record["details"] = details
    return record
