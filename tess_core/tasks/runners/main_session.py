from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from typing import Any

from ...agent import run_agent_prompt
from ...runtime.agent_bindings import create_agent_runtime_bindings
from ...config import resolve_config
from ...runtime.generation import create_runtime_session_binding
from ...storage import configure_runtime_home, load_session, save_session
from ...tools import create_builtin_tools


async def run_background_main_session(args: dict[str, Any]) -> dict[str, Any]:
    runtime_home = os.environ.get("TESS_CORE_HOME")
    if runtime_home:
        configure_runtime_home(runtime_home)
    session = await load_session(args["sessionId"])
    if not session:
        raise RuntimeError(f"Session not found: {args['sessionId']}")
    config = await resolve_config(_session_config_options(session, runtime_home), session["cwd"])
    _apply_session_overrides(config, session)
    events = []

    def emit_event(event: str, data: dict[str, Any]) -> None:
        events.append({"event": event, "data": data})

    runtime = create_agent_runtime_bindings({
        "emitEvent": emit_event,
        "tools": create_builtin_tools(),
        "daemon": {"state": "unavailable", "reason": {"code": "background.daemon_unavailable", "message": "No task daemon is attached to this background runner."}},
        "refreshMcpToolRegistry": lambda: None,
        "sessionBinding": create_runtime_session_binding(session["id"], 1, 1),
        "questionPolicy": {"interactive": False, "askQuestion": lambda *_args: ""},
    })
    result = await run_agent_prompt({"session": session, "config": config, "prompt": args["prompt"], "runtime": runtime})
    await save_session(session)
    return {
        "ok": True,
        "session_id": session["id"],
        "task_id": args["taskId"],
        "result": {
            "text": result.get("text"),
            "stopReason": result.get("stopReason"),
            "toolCount": result.get("toolCount"),
            "usage": result.get("usage"),
        },
        "events": events[-20:],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="tess-core Python background main-session runner")
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--prompt", required=True)
    parsed = parser.parse_args()
    try:
        result = asyncio.run(run_background_main_session({"sessionId": parsed.session_id, "taskId": parsed.task_id, "prompt": parsed.prompt}))
        print(json.dumps(result, indent=2))
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc), "session_id": parsed.session_id, "task_id": parsed.task_id}, indent=2), file=sys.stderr)
        raise


def _apply_session_overrides(config: dict[str, Any], session: dict[str, Any]) -> None:
    for key in ("provider", "providerProfileName", "model", "effort", "permissionMode", "systemPrompt"):
        if session.get(key) is not None:
            config[key] = session[key]
    config["providerKind"] = config.get("provider")


def _session_config_options(session: dict[str, Any], runtime_home: str | None) -> dict[str, Any]:
    metadata = session.get("metadata") if isinstance(session.get("metadata"), dict) else {}
    settings = (metadata.get("tessCoreSettingsPath") or metadata.get("settingsPath")) if isinstance(metadata, dict) else None
    options = {
        "runtimeHome": runtime_home,
        "provider": session.get("providerProfileName") or session.get("provider"),
        "model": session.get("model"),
    }
    if metadata.get("tessCoreProviderRegistryPath") is not None:
        options["providerRegistry"] = metadata["tessCoreProviderRegistryPath"]
        options["settingSources"] = metadata.get("tessCoreSettingsSources", [])
        if metadata.get("tessCoreProviderAuthPath") is not None:
            options["providerAuthPath"] = metadata["tessCoreProviderAuthPath"]
    if settings:
        options["settings"] = settings
    return options


if __name__ == "__main__":
    main()
