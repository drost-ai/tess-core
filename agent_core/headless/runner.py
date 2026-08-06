from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from dataclasses import dataclass
from typing import Any, Sequence, TextIO

from ..agent import runAgentPrompt
from ..runtime.agent_bindings import createAgentRuntimeBindings, isTaskSupervisorGateway
from ..config import resolveConfig
from ..runtime.bootstrap import bootstrapRuntime
from ..session.persistence import resolveSession
from ..session.runtime import activeSessionPayload
from ..storage import scopedRuntimeHome, writeBackSessionV1
from .stream import JsonStreamSink


@dataclass
class HeadlessOptions:
    prompt: str
    cwd: str
    runtime_home: str | None = None
    session: str | None = None
    provider: str | None = None
    model: str | None = None
    effort: str | None = None
    permission_mode: str | None = None
    tools: list[str] | None = None
    allowed_tools: list[str] | None = None
    disallowed_tools: list[str] | None = None
    input_format: str = "text"
    output_format: str = "text"
    max_turns: int = 1
    session_persistence: bool = True


async def run_headless(options: HeadlessOptions, *, stdout: TextIO = sys.stdout) -> int:
    if options.max_turns < 1:
        raise ValueError("--max-turns must be at least 1")
    prompt = _decode_prompt(options.prompt, options.input_format)
    config = await resolveConfig(_config_options(options), options.cwd)
    session_options = {"resume": options.session} if options.session else {}
    session = await resolveSession(session_options, config, {"cwd": options.cwd, "originalCwd": options.cwd})
    runtime = await bootstrapRuntime({"session": session, "config": config, "cwd": options.cwd})
    stream_enabled = options.output_format == "stream-json"
    sink = JsonStreamSink(stdout if stream_enabled else None)
    runtime["subscribeEvents"](sink.handle_runtime_envelope)
    runtime["emitEvent"]("system.init", _system_init_payload(session, config, runtime, options))
    runtime["emitEvent"]("session.activated", activeSessionPayload(session, config))

    provider_override = _scripted_provider_override()
    daemon = runtime.get("daemon")
    runtime_context = createAgentRuntimeBindings(
        {
            "tools": runtime["tools"],
            "emitEvent": runtime["emitEvent"],
            "daemon": {"state": "connected", "client": daemon}
            if isTaskSupervisorGateway(daemon)
            else {
                "state": "unavailable",
                "reason": {
                    "code": "headless.daemon_unavailable",
                    "message": "The task supervisor daemon is unavailable in this headless run.",
                },
            },
            "refreshMcpToolRegistry": runtime["refreshMcpToolRegistry"],
            "sessionBinding": runtime["getActiveBinding"](),
            "questionPolicy": {"interactive": False, "askQuestion": lambda *_args, **_kwargs: ""},
            "print_mode": True,
            "printMode": True,
        }
    )
    result: dict[str, Any]
    exit_code = 0
    try:
        runtime["startTurn"]({"prompt": prompt, "model": session.get("model") or config.get("model")})
        result = await runAgentPrompt(
            {
                "session": session,
                "config": config,
                "runtime": runtime_context,
                "prompt": prompt,
                **({"providerOverride": provider_override} if provider_override else {}),
            }
        )
        if sink.permission_denied:
            result = {**result, "stopReason": "permission_denied", "error": "Permission denied in non-interactive mode."}
            exit_code = 2
        elif _is_error_stop_reason(result.get("stopReason")):
            exit_code = 1
        finish = "cancelled" if result.get("stopReason") == "cancelled" else "completed"
        runtime["finishTurn"]({"kind": finish, "stopReason": result.get("stopReason"), "model": result.get("model")})
    except Exception as exc:
        runtime["failTurn"](exc)
        result = _error_result(exc, session)
        exit_code = 1
    finally:
        final = _final_result(result, session, exit_code)
        runtime["emitEvent"]("result", final)
        if options.output_format == "json":
            stdout.write(json.dumps(final, ensure_ascii=False, separators=(",", ":")) + "\n")
            stdout.flush()
        elif options.output_format == "text" and final.get("text"):
            stdout.write(str(final["text"]).rstrip() + "\n")
            stdout.flush()
        await _close_runtime(runtime)
    return exit_code


def headless_main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.command == "session-write-back-v1":
        try:
            with scopedRuntimeHome(args.runtime_home):
                report = asyncio.run(writeBackSessionV1(args.session_id))
            sys.stdout.write(json.dumps(report, ensure_ascii=False, separators=(",", ":")) + "\n")
            return 0
        except Exception as exc:
            sys.stderr.write(f"agent_core session-write-back-v1 failed: {exc}\n")
            return 1
    if args.command != "run":
        parser.print_help()
        return 2
    prompt = _read_prompt_arg(args.prompt, args.input_format)
    options = HeadlessOptions(
        prompt=prompt,
        cwd=os.path.abspath(args.cwd or os.getcwd()),
        runtime_home=args.runtime_home,
        session=args.session,
        provider=args.provider,
        model=args.model,
        effort=args.effort,
        permission_mode=args.permission_mode,
        tools=_split_csv(args.tools),
        allowed_tools=_split_csv(args.allowed_tools),
        disallowed_tools=_split_csv(args.disallowed_tools),
        input_format=args.input_format,
        output_format=args.output_format,
        max_turns=args.max_turns,
        session_persistence=not args.no_session_persistence,
    )
    try:
        return asyncio.run(run_headless(options))
    except Exception as exc:
        sys.stderr.write(f"agent_core run failed: {exc}\n")
        return 1


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m agent_core")
    sub = parser.add_subparsers(dest="command")
    run = sub.add_parser("run", help="Run one non-interactive agent turn.")
    run.add_argument("-p", "--prompt", help="Prompt text, or '-' to read stdin.")
    run.add_argument("--cwd", help="Workspace directory.")
    run.add_argument("--runtime-home", dest="runtime_home", help="Agent-core runtime home.")
    run.add_argument("--session", help="Resume a saved session by id or search query.")
    run.add_argument("--provider", help="Provider or provider profile name.")
    run.add_argument("--model", help="Model name.")
    run.add_argument("--effort", help="Reasoning effort level.")
    run.add_argument("--permission-mode", dest="permission_mode", help="Permission mode.")
    run.add_argument("--tools", help="Comma-separated enabled tool names.")
    run.add_argument("--allowed-tools", dest="allowed_tools", help="Comma-separated allow-list.")
    run.add_argument("--disallowed-tools", dest="disallowed_tools", help="Comma-separated deny-list.")
    run.add_argument("--input-format", choices=["text", "json"], default="text")
    run.add_argument("--output-format", choices=["text", "json", "stream-json"], default="text")
    run.add_argument("--max-turns", type=int, default=1)
    run.add_argument("--no-session-persistence", action="store_true")
    write_back = sub.add_parser(
        "session-write-back-v1",
        help="Rewrite one schema-v2 session head as a legacy schema-v1 full record.",
    )
    write_back.add_argument("session_id", help="Session id to write back.")
    write_back.add_argument("--runtime-home", dest="runtime_home", required=True)
    return parser


def _config_options(options: HeadlessOptions) -> dict[str, Any]:
    out: dict[str, Any] = {"outputFormat": options.output_format, "inputFormat": options.input_format}
    for key, value in {
        "runtimeHome": options.runtime_home,
        "provider": options.provider,
        "model": options.model,
        "effort": options.effort,
        "permissionMode": options.permission_mode,
        "tools": options.tools,
        "allowedTools": options.allowed_tools,
        "disallowedTools": options.disallowed_tools,
        "sessionPersistence": options.session_persistence,
    }.items():
        if value is not None:
            out[key] = value
    return out


def _read_prompt_arg(prompt: str | None, input_format: str) -> str:
    if prompt == "-":
        return sys.stdin.read()
    if prompt is not None:
        return prompt
    if not sys.stdin.isatty():
        return sys.stdin.read()
    return "{}" if input_format == "json" else ""


def _decode_prompt(raw: str, input_format: str) -> str:
    if input_format != "json":
        return raw
    payload = json.loads(raw or "{}")
    if isinstance(payload, dict):
        value = payload.get("prompt") or payload.get("message") or payload.get("text") or ""
        return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)


def _split_csv(value: str | None) -> list[str] | None:
    if value is None:
        return None
    return [item.strip() for item in value.split(",") if item.strip()]


def _system_init_payload(session: dict[str, Any], config: dict[str, Any], runtime: dict[str, Any], options: HeadlessOptions) -> dict[str, Any]:
    profile = config.get("providerProfile") if isinstance(config.get("providerProfile"), dict) else {}
    return {
        "stream_version": 1,
        "session_id": session.get("id"),
        "cwd": session.get("cwd") or config.get("cwd"),
        "provider": config.get("provider"),
        "provider_profile": config.get("providerProfileName"),
        "model": session.get("model") or config.get("model"),
        "permission_mode": session.get("permissionMode") or config.get("permissionMode"),
        "tools": [tool.name for tool in runtime.get("tools", [])],
        "allowed_tools": config.get("allowedTools") or [],
        "sandbox": config.get("sandbox") or {},
        "provider_capabilities": profile.get("capabilities") or {},
        "input_format": options.input_format,
        "output_format": options.output_format,
        "max_turns": options.max_turns,
    }


def _scripted_provider_override() -> dict[str, Any] | None:
    error = os.environ.get("AGENT_CORE_HEADLESS_PROVIDER_ERROR")
    raw = os.environ.get("AGENT_CORE_HEADLESS_PROVIDER_RESPONSES")
    fixture = os.environ.get("AGENT_CORE_HEADLESS_PROVIDER_RESPONSES_FILE")
    if not error and not raw and not fixture:
        return None
    responses = _load_scripted_responses(raw, fixture)
    state = {"index": 0}

    async def send(request: dict[str, Any]) -> dict[str, Any]:
        if error:
            raise RuntimeError(error)
        response = responses[min(state["index"], len(responses) - 1)]
        state["index"] += 1
        return json.loads(json.dumps(response))

    return {"send": send}


def _load_scripted_responses(raw: str | None, fixture: str | None) -> list[dict[str, Any]]:
    if fixture:
        with open(fixture, "r", encoding="utf-8") as handle:
            raw = handle.read()
    payload = json.loads(raw or "[]")
    if isinstance(payload, dict):
        return [payload]
    if isinstance(payload, list) and payload and all(isinstance(item, dict) for item in payload):
        return payload
    return [_text_response("ok")]


def _text_response(text: str) -> dict[str, Any]:
    return {"model": "gpt-5.4", "stopReason": "end_turn", "usage": {"input_tokens": 1, "output_tokens": 1}, "content": [{"type": "text", "text": text}]}


def _is_error_stop_reason(stop_reason: Any) -> bool:
    return stop_reason in {"cancelled", "error", "failed", "permission_denied"}


def _error_result(exc: BaseException, session: dict[str, Any]) -> dict[str, Any]:
    return {
        "text": "",
        "toolCount": 0,
        "usage": {},
        "model": session.get("model"),
        "estimatedCostUsd": 0.0,
        "stopReason": "error",
        "error": str(exc),
    }


def _final_result(result: dict[str, Any], session: dict[str, Any], exit_code: int) -> dict[str, Any]:
    return {
        "success": exit_code == 0,
        "exitCode": exit_code,
        "sessionId": session.get("id"),
        "text": result.get("text", ""),
        "model": result.get("model") or session.get("model"),
        "stopReason": result.get("stopReason"),
        "usage": result.get("usage") or {},
        "toolCount": result.get("toolCount", 0),
        "estimatedCostUsd": result.get("estimatedCostUsd", 0.0),
        **({"error": result.get("error")} if result.get("error") else {}),
    }


async def _close_runtime(runtime: dict[str, Any]) -> None:
    close = runtime.get("close")
    if not close:
        return
    result = close()
    if hasattr(result, "__await__"):
        await result
