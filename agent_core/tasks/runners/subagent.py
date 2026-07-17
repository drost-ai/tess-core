from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from typing import Any

from ...agent import runAgentPrompt
from ...agent_authority import assertMatchingPersistedChildAuthorities, enforceSessionAgentAuthority
from ...config import resolveConfig
from ...storage import configureRuntimeHome, loadChildAgent, loadSession, saveSession
from ..subagents import finalizeBackgroundSubagentFailureFromEnv, finalizeBackgroundSubagentSuccessFromEnv
from ...tools import createBuiltinTools


async def run_background_subagent(args: dict[str, Any]) -> dict[str, Any]:
    session, result, payload = await _execute_background_subagent(args)
    await finalizeBackgroundSubagentSuccessFromEnv(session, result)
    await saveSession(session)
    return payload


async def _execute_background_subagent(args: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    runtime_home = os.environ.get("AGENT_CORE_HOME")
    if runtime_home:
        configureRuntimeHome(runtime_home)
    session = await loadSession(args["sessionId"])
    if not session:
        raise RuntimeError(f"Subagent session not found: {args['sessionId']}")
    child_record = await loadChildAgent(args["agentId"])
    assertMatchingPersistedChildAuthorities(
        (session.get("metadata") or {}).get("effectiveAgentAuthority"),
        (child_record or {}).get("effectiveAgentAuthority"),
    )
    config = await resolveConfig(_session_config_options(session, runtime_home), session["cwd"])
    _apply_session_overrides(config, session)
    await enforceSessionAgentAuthority(session=session, config=config)
    events = []

    def emit_event(event: str, data: dict[str, Any]) -> None:
        events.append({"event": event, "data": data})

    runtime = {
        "interactive": False,
        "askQuestion": lambda *_args: "",
        "emitEvent": emit_event,
        "tools": createBuiltinTools(),
    }
    result = await runAgentPrompt({"session": session, "config": config, "prompt": "", "runtime": runtime})
    payload = {"ok": True, "session_id": session["id"], "agent_id": args["agentId"], "task_id": args["taskId"], "result": {"text": result.get("text"), "stopReason": result.get("stopReason"), "toolCount": result.get("toolCount"), "usage": result.get("usage")}, "events": events[-20:]}
    return session, result, payload


def main() -> None:
    parser = argparse.ArgumentParser(description="agent-core Python background subagent runner")
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--agent-id", required=True)
    parser.add_argument("--task-id", required=True)
    parsed = parser.parse_args()
    try:
        session, result, payload = asyncio.run(_execute_background_subagent({"sessionId": parsed.session_id, "agentId": parsed.agent_id, "taskId": parsed.task_id}))
        print(json.dumps(payload, indent=2), flush=True)
        asyncio.run(finalizeBackgroundSubagentSuccessFromEnv(session, result))
        asyncio.run(saveSession(session))
    except Exception as exc:
        asyncio.run(finalizeBackgroundSubagentFailureFromEnv(exc))
        print(json.dumps({"ok": False, "error": str(exc), "session_id": parsed.session_id, "agent_id": parsed.agent_id, "task_id": parsed.task_id}, indent=2), file=sys.stderr)
        raise


def _apply_session_overrides(config: dict[str, Any], session: dict[str, Any]) -> None:
    for key in ("provider", "providerProfileName", "model", "effort", "permissionMode", "systemPrompt"):
        if session.get(key) is not None:
            config[key] = session[key]
    config["providerKind"] = config.get("provider")


def _session_config_options(session: dict[str, Any], runtime_home: str | None) -> dict[str, Any]:
    metadata = session.get("metadata") if isinstance(session.get("metadata"), dict) else {}
    settings = (metadata.get("agentCoreSettingsPath") or metadata.get("settingsPath")) if isinstance(metadata, dict) else None
    options = {
        "runtimeHome": runtime_home,
        "provider": session.get("providerProfileName") or session.get("provider"),
        "model": session.get("model"),
        "settingSources": ",".join(
            ((metadata.get("effectiveAgentAuthority") or {}).get("executableSources") or {}).get("settingsSources")
            or ["user"]
        ),
    }
    if settings:
        options["settings"] = settings
    return options


if __name__ == "__main__":
    main()
