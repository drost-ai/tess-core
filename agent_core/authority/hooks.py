from __future__ import annotations

def _knob(name: str):
    from ..config._knobs import knobValue

    return knobValue(name)


import asyncio
import hashlib
import json
import os
import signal
from typing import Any, Mapping

from .permission_rules import matchPermissionRule, parsePermissionRule
from .repository_trust import approveRepositoryContent, checkRepositoryApproval
from ..sandbox import prepareSandboxedArgv
from ..storage import get_project_root, load_settings


HOOK_EVENTS = ("PreToolUse", "PostToolUse", "Stop", "SessionEnd")
_SETTINGS_SCOPES = ("user", "project", "local")


def _normalize_hook(raw: Any, event: str, scope: str, approved: bool) -> dict[str, Any] | None:
    if not isinstance(raw, Mapping) or not isinstance(raw.get("command"), str) or not raw["command"].strip():
        return None
    timeout = raw.get("timeout")
    timeout_seconds = float(timeout) if isinstance(timeout, (int, float)) and timeout > 0 else (int(_knob("agent.hook_timeout_ms")) / 1000)
    matcher = raw.get("matcher") if isinstance(raw.get("matcher"), str) and raw["matcher"].strip() else None
    return {
        "event": event,
        "matcher": matcher.strip() if matcher else None,
        "command": raw["command"],
        "timeoutMs": round(timeout_seconds * 1000),
        "blocking": event == "PreToolUse" and raw.get("blocking") is True,
        "scope": scope,
        "approved": approved,
    }


def _hook_set_hash(definitions: Any) -> str:
    return hashlib.sha256(
        json.dumps(definitions or {}, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()


async def approveHooks(cwd: str, scope: str) -> None:
    if scope not in {"project", "local"}:
        raise ValueError("Hook approval scope must be project or local")
    settings = await load_settings(scope, cwd)
    await approveRepositoryContent(
        cwd=cwd,
        family="hooks",
        scope=scope,
        contentDigest=_hook_set_hash(settings.get("hooks")),
    )


async def _hook_set_approved(cwd: str, scope: str, hooks: Any) -> bool:
    return not hooks or await checkRepositoryApproval(
        cwd=cwd,
        family="hooks",
        scope=scope,
        contentDigest=_hook_set_hash(hooks),
    )


async def loadHooks(
    args: Mapping[str, Any] | None = None,
    *,
    cwd: str | None = None,
    sources: list[str] | None = None,
) -> list[dict[str, Any]]:
    values = dict(args or {})
    selected_cwd = str(values.get("cwd", cwd))
    selected_sources = values.get("sources", sources)
    loaded: list[dict[str, Any]] = []
    for scope in _SETTINGS_SCOPES:
        if selected_sources and scope not in selected_sources:
            continue
        settings = await load_settings(scope, selected_cwd)
        hooks = settings.get("hooks") if isinstance(settings.get("hooks"), Mapping) else {}
        approved = scope == "user" or await _hook_set_approved(selected_cwd, scope, hooks)
        for event in HOOK_EVENTS:
            for raw in hooks.get(event) or []:
                normalized = _normalize_hook(raw, event, scope, approved)
                if normalized:
                    loaded.append(normalized)
    return loaded


async def listUnapprovedHookScopes(args: Mapping[str, Any]) -> list[str]:
    hooks = await loadHooks(args)
    return list(dict.fromkeys(hook["scope"] for hook in hooks if not hook["approved"]))


def _hook_matches(hook: Mapping[str, Any], tool_name: str, project_root: str, call: Mapping[str, Any]) -> bool:
    matcher = hook.get("matcher")
    if not matcher:
        return True
    rules = [
        rule
        for token in str(matcher).split("|")
        if (rule := parsePermissionRule(token.strip(), "allow", "inline", True))
    ]
    return bool(
        matchPermissionRule(
            rules,
            {"toolName": tool_name, **dict(call)},
            effects=("allow",),
            projectRoot=project_root,
        )
    )


async def _run_hook(hook: Mapping[str, Any], payload: Mapping[str, Any], fire: Mapping[str, Any]) -> dict[str, Any]:
    shell = os.environ.get("SHELL") or ("cmd.exe" if os.name == "nt" else "/bin/sh")
    argv = [shell, "/c" if os.name == "nt" else "-lc", str(hook["command"])]
    prepared = prepareSandboxedArgv(
        argv,
        config=fire.get("config") or {},
        session={"cwd": fire["cwd"]},
        cwd=fire["cwd"],
        emit=fire.get("emit"),
    )
    environment = os.environ.copy()
    environment.update(
        {
            "AGENT_EVENT": str(hook["event"]),
            "AGENT_TOOL_NAME": str(payload.get("toolName") or ""),
            "AGENT_TOOL_PATH": str((payload.get("toolInput") or {}).get("file_path") or ""),
            "AGENT_HOOK_PAYLOAD": json.dumps(payload, separators=(",", ":"), ensure_ascii=False),
        }
    )
    process = await asyncio.create_subprocess_exec(
        *prepared["argv"],
        cwd=fire["cwd"],
        env=environment,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    timed_out = False
    try:
        stdout, stderr = await asyncio.wait_for(
            process.communicate(),
            timeout=max(0.001, int(hook["timeoutMs"]) / 1000),
        )
    except asyncio.TimeoutError:
        timed_out = True
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            process.terminate()
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=2)
        except asyncio.TimeoutError:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                process.kill()
            stdout, stderr = await process.communicate()
    return {
        "exitCode": process.returncode,
        "stdout": stdout.decode("utf-8", errors="replace"),
        "stderr": stderr.decode("utf-8", errors="replace"),
        "timedOut": timed_out,
    }


def _interpret_blocking(result: Mapping[str, Any]) -> dict[str, Any]:
    if result.get("timedOut"):
        return {"deny": True, "reason": "hook timed out"}
    if result.get("exitCode") == 0:
        trimmed = str(result.get("stdout") or "").strip()
        if trimmed.startswith("{"):
            try:
                parsed = json.loads(trimmed)
                if parsed.get("decision") == "deny":
                    return {"deny": True, "reason": parsed.get("reason") or "blocked by hook"}
            except json.JSONDecodeError:
                pass
        return {"deny": False}
    if result.get("exitCode") == 2:
        return {"deny": True, "reason": str(result.get("stderr") or "").strip() or "blocked by hook (exit 2)"}
    return {
        "deny": True,
        "reason": str(result.get("stderr") or "").strip() or f"hook failed (exit {result.get('exitCode', 'unknown')})",
    }


async def fireToolHooks(args: Mapping[str, Any]) -> dict[str, Any]:
    fire = args["fire"]
    authority = fire.get("authority") if isinstance(fire.get("authority"), Mapping) else None
    hooks = list(authority.get("hooks") or []) if authority else await loadHooks(
        cwd=fire["cwd"],
        sources=(fire.get("config") or {}).get("settingsSources"),
    )
    hooks = [hook for hook in hooks if hook.get("event") == args["event"] and hook.get("approved")]
    outcome: dict[str, Any] = {"decision": "continue", "ran": []}
    project_root = (authority or {}).get("permissionRuleProjectRoot") or get_project_root(fire["cwd"])
    tool_input = args.get("toolInput") if isinstance(args.get("toolInput"), Mapping) else {}
    call = {
        "command": tool_input.get("command") if isinstance(tool_input.get("command"), str) else None,
        "path": next(
            (tool_input.get(key) for key in ("file_path", "notebook_path", "path") if isinstance(tool_input.get(key), str)),
            None,
        ),
        "url": tool_input.get("url") if isinstance(tool_input.get("url"), str) else None,
    }
    payload = {
        "event": args["event"],
        "toolName": args["toolName"],
        "toolInput": dict(tool_input),
        "sessionId": fire["sessionId"],
        "cwd": fire["cwd"],
        "transcriptPath": fire.get("transcriptPath"),
    }
    emit = args.get("emit") or (lambda *_: None)
    for hook in hooks:
        if not _hook_matches(hook, args["toolName"], project_root, call):
            continue
        started = asyncio.get_running_loop().time()
        try:
            result = await _run_hook(hook, payload, {**dict(fire), "emit": emit})
        except Exception as error:
            result = {"exitCode": None, "stdout": "", "stderr": str(error), "timedOut": False}
        blocked = False
        reason = None
        if hook.get("blocking"):
            interpreted = _interpret_blocking(result)
            blocked, reason = interpreted["deny"], interpreted.get("reason")
        row = {
            "command": hook["command"],
            "scope": hook["scope"],
            "exitCode": result.get("exitCode"),
            "durationMs": round((asyncio.get_running_loop().time() - started) * 1000),
            "blocked": blocked,
        }
        outcome["ran"].append(row)
        emit("hook.completed", {**row, "event": args["event"]})
        if blocked:
            outcome.update({"decision": "deny", "reason": reason})
            break
    return outcome


