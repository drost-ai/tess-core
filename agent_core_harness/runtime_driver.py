from __future__ import annotations

import asyncio
import json
import os
from typing import Any

import agent_core._facade as core
from agent_core.contracts import ToolContext

from .model import ScenarioAssertionError
from .paths import REPO_ROOT
from .runtime_bindings import bind_agent_runtime
from .sandbox import ScenarioSandbox


async def bootstrap_runtime(data: dict[str, Any], sandbox: ScenarioSandbox, state: dict[str, Any]) -> list[str]:
    session, config = await ensure_session_config(data, sandbox, state)
    events = state.setdefault("runtime_events", [])
    runtime = await core.bootstrap_runtime({"session": session, "config": config, "cwd": str(sandbox.workspace), "daemonEnabled": False})
    runtime["subscribeEvents"](lambda envelope: events.append(_json_safe(_validated_envelope(envelope))))
    runtime["emitEvent"]("session.activated", core.active_session_payload(session, config))
    state["runtime"] = runtime
    state.setdefault("driver_results", []).append(
        {
            "driver": "bootstrap_runtime",
            "status": "passed",
            "toolNames": [tool.name for tool in runtime.get("tools", [])],
            "slashCommandNames": [command.get("name") for command in runtime.get("slashCommands", [])],
        }
    )
    return ["runtime_state", "driver_results"]


async def run_agent(data: dict[str, Any], sandbox: ScenarioSandbox, state: dict[str, Any]) -> list[str]:
    session, config = await ensure_session_config(data, sandbox, state)
    runtime = await ensure_runtime(data, sandbox, state)
    provider = ScriptedAgentProvider(data.get("providerResponses") or data.get("responses") or [])
    try:
        result = await core.runAgentPrompt(
            {
                "session": session,
                "config": config,
                "runtime": bind_agent_runtime(session, runtime),
                "prompt": data.get("prompt", ""),
                "providerOverride": {"send": provider.send},
            }
        )
        status = "passed"
    except Exception as exc:
        if not data.get("expectFailure"):
            raise ScenarioAssertionError(f"run_agent failed: {exc}", category="driver")
        result = {"error": str(exc)}
        status = "failed"
    state.setdefault("agent_requests", []).extend(provider.requests)
    state.setdefault("driver_results", []).append({"driver": "run_agent", "status": status, "result": _json_safe(result), "requestCount": len(provider.requests)})
    return ["driver_results", "runtime_events", "transcript"]


async def run_subprocess(data: dict[str, Any], sandbox: ScenarioSandbox, state: dict[str, Any]) -> list[str]:
    command = data.get("command")
    if not isinstance(command, list) or not command or any(not isinstance(item, str) for item in command):
        raise ScenarioAssertionError("run_subprocess.command must be a non-empty string list", category="harness")
    env = {**os.environ, **(data.get("env") or {})}
    env["PYTHONPATH"] = str(REPO_ROOT) if not env.get("PYTHONPATH") else f"{REPO_ROOT}{os.pathsep}{env['PYTHONPATH']}"
    proc = await asyncio.create_subprocess_exec(
        *command,
        cwd=str(sandbox.workspace),
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=float(data.get("timeoutSeconds") or 10))
    except asyncio.TimeoutError:
        proc.kill()
        stdout, stderr = await proc.communicate()
        result = {
            "driver": "run_subprocess",
            "status": "timeout",
            "command": command,
            "returnCode": proc.returncode,
            "stdout": stdout.decode("utf-8", errors="replace"),
            "stderr": stderr.decode("utf-8", errors="replace"),
            "timedOut": True,
        }
        state.setdefault("subprocess_results", []).append(result)
        state.setdefault("driver_results", []).append(result)
        if data.get("expectFailure"):
            return ["subprocess_results", "driver_results"]
        raise ScenarioAssertionError(f"subprocess timed out: {command}", category="driver")
    result = {
        "driver": "run_subprocess",
        "status": "passed" if proc.returncode == int(data.get("expectCode", 0)) else "failed",
        "command": command,
        "returnCode": proc.returncode,
        "stdout": stdout.decode("utf-8", errors="replace"),
        "stderr": stderr.decode("utf-8", errors="replace"),
    }
    state.setdefault("subprocess_results", []).append(result)
    state.setdefault("driver_results", []).append(result)
    if result["status"] == "failed" and not data.get("expectFailure"):
        raise ScenarioAssertionError(f"subprocess exited {proc.returncode}: {command}", category="driver")
    return ["subprocess_results", "driver_results"]


async def run_slash_command(data: dict[str, Any], sandbox: ScenarioSandbox, state: dict[str, Any]) -> list[str]:
    session, config = await ensure_session_config(data, sandbox, state)
    hooks = SlashHooks(state)
    result = await core.handleSlashCommand({"command": data.get("command", ""), "session": session, "config": config, "hooks": hooks.as_dict()})
    record = {"driver": "run_slash_command", "status": "failed" if result.get("isError") else "passed", "command": data.get("command", ""), "result": _json_safe(result), "hooks": hooks.snapshot()}
    if record["status"] == "failed" and not data.get("expectFailure"):
        raise ScenarioAssertionError(f"slash command failed: {data.get('command')}", category="driver")
    state.setdefault("slash_results", []).append(record)
    state.setdefault("driver_results", []).append(record)
    return ["slash_results", "driver_results"]


async def call_tool(data: dict[str, Any], sandbox: ScenarioSandbox, state: dict[str, Any]) -> list[str]:
    session, config = await ensure_session_config(data, sandbox, state)
    runtime = await ensure_runtime(data, sandbox, state)
    name = str(data.get("name") or "")
    tool = next((entry for entry in runtime.get("tools", []) if entry.name == name), None)
    if not tool:
        raise ScenarioAssertionError(f"Tool not found: {name}", category="driver")
    input_data = dict(data.get("input") or {})
    try:
        result = await tool.handler(input_data, _tool_context(sandbox, state, session, config, runtime))
        status = "failed" if result.get("isError") else "passed"
    except Exception as exc:
        if not data.get("expectFailure"):
            raise ScenarioAssertionError(f"tool {name} failed: {exc}", category="driver")
        result = {"content": str(exc), "isError": True}
        status = "failed"
    record = {"driver": "call_tool", "tool": name, "status": status, "input": input_data, "result": _json_safe(result)}
    state.setdefault("tool_results", []).append(record)
    state.setdefault("driver_results", []).append(record)
    if data.get("appendTranscript", True):
        _append_tool_transcript(session, name, input_data, result)
        await core.save_session(session)
    return ["tool_results", "driver_results", "transcript", "tool_artifacts"]


def assert_driver_result(state: dict[str, Any], data: dict[str, Any]) -> None:
    records = [record for record in state.get("driver_results", []) if not data.get("driver") or record.get("driver") == data["driver"]]
    if not records:
        raise ScenarioAssertionError(f"No driver result found for {data.get('driver')!r}")
    record = records[int(data.get("index", len(records) - 1))]
    for key in ("status", "returnCode"):
        if key in data and record.get(key) != data[key]:
            raise ScenarioAssertionError(f"driver result {key} expected {data[key]!r}, got {record.get(key)!r}")
    if "stdoutContains" in data and data["stdoutContains"] not in str(record.get("stdout", "")):
        raise ScenarioAssertionError(f"stdout did not contain {data['stdoutContains']!r}")
    if "messageContains" in data:
        message = str((record.get("result") or {}).get("message") or (record.get("result") or {}).get("content") or "")
        if data["messageContains"] not in message:
            raise ScenarioAssertionError(f"driver message did not contain {data['messageContains']!r}")
    if "toolNamesInclude" in data:
        missing = sorted(set(data["toolNamesInclude"]) - set(record.get("toolNames") or []))
        if missing:
            raise ScenarioAssertionError(f"runtime tools missing: {missing}")


def assert_runtime_event(state: dict[str, Any], data: dict[str, Any]) -> None:
    events = state.get("runtime_events") or []
    names = [event.get("event") for event in events]
    missing = [name for name in data.get("eventsInclude", []) if name not in names]
    if missing:
        raise ScenarioAssertionError(f"runtime events missing: {missing}")


async def close_runtime_driver(state: dict[str, Any]) -> None:
    runtime = state.get("runtime")
    if runtime and runtime.get("close"):
        result = runtime["close"]()
        if hasattr(result, "__await__"):
            await result


async def ensure_session_config(data: dict[str, Any], sandbox: ScenarioSandbox, state: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    if state.get("session") and state.get("config"):
        return state["session"], state["config"]
    config = await core.resolveConfig({"runtimeHome": str(sandbox.home), "permissionMode": data.get("permissionMode", "bypassPermissions")}, str(sandbox.workspace))
    session = core.create_empty_session(cwd=str(sandbox.workspace), provider=config["provider"], providerProfileName=config.get("providerProfileName"), model=config["model"], effort=config.get("effort"), systemPrompt=config["systemPrompt"], permissionMode=data.get("permissionMode", "bypassPermissions"))
    state["config"] = config
    state["session"] = session
    return session, config


async def ensure_runtime(data: dict[str, Any], sandbox: ScenarioSandbox, state: dict[str, Any]) -> dict[str, Any]:
    if state.get("runtime"):
        return state["runtime"]
    await bootstrap_runtime(data, sandbox, state)
    return state["runtime"]


class ScriptedAgentProvider:
    def __init__(self, responses: list[dict[str, Any]]) -> None:
        self.responses = responses or [_text_response("ok")]
        self.requests: list[dict[str, Any]] = []
        self.index = 0

    async def send(self, request: dict[str, Any]) -> dict[str, Any]:
        self.requests.append(_json_safe(request))
        response = self.responses[min(self.index, len(self.responses) - 1)]
        self.index += 1
        return _json_safe(response)


class SlashHooks:
    def __init__(self, state: dict[str, Any]) -> None:
        self.state = state
        self.infos: list[dict[str, str]] = []
        self.errors: list[str] = []
        self.persist_count = 0
        self.refresh_count = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "info": lambda title, message: self.infos.append({"title": str(title), "message": str(message)}),
            "error": lambda message: self.errors.append(str(message)),
            "persist": self._persist,
            "refresh": self._refresh,
            "emitRuntimeEvent": lambda event, data: self.state.setdefault("runtime_events", []).append({"event": event, "data": _json_safe(data)}),
        }

    def snapshot(self) -> dict[str, Any]:
        return {"infos": self.infos, "errors": self.errors, "persistCount": self.persist_count, "refreshCount": self.refresh_count}

    def _persist(self) -> None:
        self.persist_count += 1

    def _refresh(self) -> None:
        self.refresh_count += 1


def _tool_context(sandbox: ScenarioSandbox, state: dict[str, Any], session: dict[str, Any], config: dict[str, Any], runtime: dict[str, Any]) -> ToolContext:
    return {
        "cwd": str(sandbox.workspace),
        "allowedRoots": config.get("allowedRoots", [str(sandbox.workspace)]),
        "config": config,
        "session": session,
        "interactive": False,
        "outputJson": lambda event, data: state.setdefault("runtime_events", []).append({"event": event, "data": _json_safe(data)}),
        "askQuestion": lambda *_args: "",
        "persist_session": lambda: core.save_session(session),
        "updateCwd": lambda next_cwd: session.__setitem__("cwd", next_cwd),
        "getTools": lambda: runtime.get("tools", []),
        "daemon": runtime.get("daemon"),
    }


def _append_tool_transcript(session: dict[str, Any], name: str, input_data: dict[str, Any], result: dict[str, Any]) -> None:
    tool_id = f"harness_tool_{len(session.get('messages', [])) + 1}"
    session.setdefault("messages", []).append({"role": "assistant", "content": [{"type": "tool_use", "id": tool_id, "name": name, "input": input_data}]})
    session["messages"].append({"role": "user", "content": [{"type": "tool_result", "tool_use_id": tool_id, "is_error": result.get("isError"), "content": result.get("content", "")}]})


def _text_response(text: str) -> dict[str, Any]:
    return {"model": "gpt-5.4", "stopReason": "end_turn", "usage": {"input_tokens": 1, "output_tokens": 1}, "content": [{"type": "text", "text": text}]}


def _validated_envelope(envelope: Any) -> Any:
    from agent_core.contracts import RuntimeSubscriberEnvelope, validate

    errors = validate(envelope, RuntimeSubscriberEnvelope, strict=True)
    if errors:
        raise ScenarioAssertionError(
            "runtime event envelope contract violation: " + "; ".join(errors),
            category="contract",
        )
    return envelope


def _json_safe(value: Any) -> Any:
    try:
        json.dumps(value)
        return value
    except Exception:
        if isinstance(value, dict):
            return {str(key): _json_safe(item) for key, item in value.items()}
        if isinstance(value, list):
            return [_json_safe(item) for item in value]
        return repr(value)
