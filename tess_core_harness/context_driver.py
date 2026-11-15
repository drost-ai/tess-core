from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import tess_core._facade as core
import tess_core.agent as agent_module
import tess_core.agent.context as agent_context_module

from .context_fixtures import ContextLimitFallbackClient, ScriptedCompactionClient, build_context_messages, retrieved_memory_payload
from .model import ScenarioAssertionError
from .runtime_bindings import bind_agent_runtime
from .sandbox import ScenarioSandbox


async def seed_context_session(data: dict[str, Any], sandbox: ScenarioSandbox, state: dict[str, Any]) -> list[str]:
    config = await core.resolve_config({"runtimeHome": str(sandbox.home), "permissionMode": data.get("permissionMode", "bypassPermissions")}, str(sandbox.workspace))
    session = core.create_empty_session(
        cwd=str(sandbox.workspace),
        provider=config["provider"],
        providerProfileName=config.get("providerProfileName"),
        model=config["model"],
        effort=config.get("effort"),
        systemPrompt=config["systemPrompt"],
        permissionMode=config.get("permissionMode", "bypassPermissions"),
    )
    profile = str(data.get("profile") or "plain")
    session["messages"] = build_context_messages(profile, int(data.get("messageCount") or 34), str(data.get("messageRoot") or "/tmp/workspace"))
    session["updatedAt"] = str(data.get("updatedAt") or "2999-01-01T00:00:00Z")
    session["todos"] = [{"content": "Finish context harness", "activeForm": "Implementing context harness", "status": "in_progress"}]
    if data.get("manualMemory", True):
        memory_path = sandbox.workspace / ".tess" / "project" / "memory.md"
        memory_path.parent.mkdir(parents=True, exist_ok=True)
        memory_path.write_text(str(data.get("manualMemoryText") or "Prefer focused parity tests"), encoding="utf-8")
    state["config"] = config
    state["session"] = session
    alias = str(data.get("alias") or "current")
    state.setdefault("context_session_aliases", {})[alias] = session
    state.setdefault("context_sessions", []).append(_session_summary(alias, profile, session))
    state.setdefault("context_driver_results", []).append({"driver": "seed_context_session", "status": "passed", "alias": alias, "profile": profile})
    return ["session", "context_sessions", "context_driver_results"]


async def run_context_compaction(data: dict[str, Any], _sandbox: ScenarioSandbox, state: dict[str, Any]) -> list[str]:
    session = _current_session(state)
    client = ScriptedCompactionClient(str(data.get("mode") or "accept"))
    result = await core.run_compaction({"session": session, "config": state.get("config") or {}, "trigger": data.get("trigger", "manual"), "force": data.get("force", True), "modelClient": client})
    alias = str(data.get("resultAlias") or data.get("alias") or data.get("mode") or "compaction")
    record = {"driver": "run_context_compaction", "alias": alias, "mode": client.mode, "status": result.get("status"), "result": _compact_result(result), "clientCalls": client.calls}
    state.setdefault("compaction_results", []).append(record)
    await _capture_compaction_artifacts(state, alias, session, result)
    await _capture_durable_memory(state, session, data.get("memoryPrompt") or "Codex Spark compaction")
    if result.get("status") != "completed" and not data.get("expectFailure"):
        raise ScenarioAssertionError(f"Compaction did not complete: {result.get('reason') or result.get('detail')}", category="driver")
    return ["compaction_results", "compaction_artifacts", "durable_memory", "session"]


async def run_context_fallback(data: dict[str, Any], _sandbox: ScenarioSandbox, state: dict[str, Any]) -> list[str]:
    session = _current_session(state)
    client = ContextLimitFallbackClient(core)
    result = await core.run_model_backed_compaction_synthesis_with_fallback(
        {
            "session": session,
            "config": state.get("config") or {},
            "modelClient": client,
            "previousWorkingContext": None,
            "latestPlan": None,
            "sourceMessages": session.get("messages", [])[:6],
            "sourceStart": 0,
        }
    )
    record = {"driver": "run_context_fallback", "status": "passed", "qualityClassification": result.get("qualityClassification"), "clientCallCount": len(client.calls), "clientCalls": client.calls}
    state.setdefault("compaction_results", []).append(record)
    return ["compaction_results"]


async def apply_context_relief(data: dict[str, Any], sandbox: ScenarioSandbox, state: dict[str, Any]) -> list[str]:
    session = _current_session(state)
    profile = str(data.get("profile") or "tool_heavy")
    messages = build_context_messages(profile, int(data.get("messageCount") or 18), str(data.get("messageRoot") or "/tmp/workspace"))
    retrieved = retrieved_memory_payload() if data.get("retrievedMemory") else {"records": [], "tokenEstimate": 0}
    pressure = core.classify_context_pressure({"session": session, "messages": messages})
    relief = core.apply_context_relief_modes(
        {
            "session": session,
            "config": state.get("config") or {"provider": "openai"},
            "prompt": data.get("prompt") or "continue",
            "messages": messages,
            "retrievedMemory": retrieved,
            "reactiveAttempt": int(data.get("reactiveAttempt") or 0),
            "aggressive": data.get("aggressive"),
        }
    )
    summary = relief["summary"]
    record = {
        "driver": "apply_context_relief",
        "alias": str(data.get("alias") or profile),
        "profile": profile,
        "pressure": pressure,
        "summary": summary,
        "renderedPrompt": core.render_context_relief_prompt(summary),
        "beforeCount": len(messages),
        "afterCount": len(relief["messages"]),
    }
    state.setdefault("continuation_results", []).append(_json_safe(record))
    return ["continuation_results"]


async def project_context_events(data: dict[str, Any], _sandbox: ScenarioSandbox, state: dict[str, Any]) -> list[str]:
    session = _current_session(state)
    runtime_state = core.create_initial_runtime_state(session)
    raw_events = [
        {"event": "compaction.completed", "data": {"generation": 3, "trigger": "threshold", "compacted_message_count": 4, "working_context_token_estimate": 1200, "verification_verdict": "accept", "checkpoint_path": "/tmp/checkpoint.json"}},
        {"event": "context_relief.selected", "data": {"mode": "microcompact", "pressure_level": "high", "planned_modes": ["microcompact", "bad_mode"], "reactive_attempt": 1}},
        {"event": "context_relief.applied", "data": {"mode": "api_microcompact", "modes": [], "tokens_saved_estimate": 42, "dropped_message_count": 3, "compacted_message_count": 2, "detail": "trimmed", "pressure_level": "not-a-pressure", "reactive_attempt": 0, "reinjection_summary": "summary", "reinjection_section_count": 2}},
    ]
    projected = []
    for raw in raw_events:
        typed = core.create_runtime_event(raw)
        runtime_state = core.reduce_runtime_state(runtime_state, typed)
        projected.append({"event": typed["event"], "payload": typed["payload"]})
    state.setdefault("context_events", []).extend(projected)
    state["context_runtime_state"] = runtime_state
    return ["context_events", "context_runtime_state"]


async def run_reactive_context_retry(data: dict[str, Any], sandbox: ScenarioSandbox, state: dict[str, Any]) -> list[str]:
    result = await _run_agent_reactive_case(sandbox, state, cancel=False)
    state.setdefault("context_agent_results", []).append(result)
    state.setdefault("context_events", []).extend(result["events"])
    return ["context_agent_results", "context_events"]


async def run_reactive_context_cancellation(data: dict[str, Any], sandbox: ScenarioSandbox, state: dict[str, Any]) -> list[str]:
    result = await _run_agent_reactive_case(sandbox, state, cancel=True)
    state.setdefault("context_agent_results", []).append(result)
    state.setdefault("context_events", []).extend(result["events"])
    return ["context_agent_results", "context_events"]


def assert_context_result(state: dict[str, Any], data: dict[str, Any]) -> None:
    record = _select_context_record(state, data)
    body = json.dumps(record, sort_keys=True)
    if data.get("status") and record.get("status") != data["status"]:
        raise ScenarioAssertionError(f"context result status expected {data['status']!r}, got {record.get('status')!r}")
    if data.get("modesInclude"):
        modes = set((record.get("summary") or {}).get("appliedModes") or [])
        missing = sorted(set(data["modesInclude"]) - modes)
        if missing:
            raise ScenarioAssertionError(f"context relief modes missing: {missing}")
    if data.get("eventsInclude"):
        names = {event.get("event") for event in state.get("context_events") or []}
        missing = sorted(set(data["eventsInclude"]) - names)
        if missing:
            raise ScenarioAssertionError(f"context events missing: {missing}")
    if data.get("textContains") and data["textContains"] not in body:
        raise ScenarioAssertionError(f"context result did not contain {data['textContains']!r}")


def assert_compaction_artifact(state: dict[str, Any], data: dict[str, Any]) -> None:
    artifact = _artifact_for_alias(state, str(data.get("alias") or "compaction"))
    body = json.dumps(artifact, sort_keys=True)
    if data.get("qualityClassification") and artifact.get("manifest", {}).get("qualityClassification") != data["qualityClassification"]:
        raise ScenarioAssertionError("compaction artifact quality mismatch")
    if data.get("nextStepContains") and data["nextStepContains"] not in body:
        raise ScenarioAssertionError(f"compaction artifact missing next step {data['nextStepContains']!r}")
    if data.get("memoryContains"):
        memory_body = json.dumps(state.get("durable_memory") or [], sort_keys=True)
        if data["memoryContains"] not in memory_body:
            raise ScenarioAssertionError(f"durable memory missing {data['memoryContains']!r}")


def assert_context_runtime_state(state: dict[str, Any], data: dict[str, Any]) -> None:
    compaction = (state.get("context_runtime_state") or {}).get("compaction") or {}
    for key, expected in data.items():
        if compaction.get(key) != expected:
            raise ScenarioAssertionError(f"context runtime state {key} expected {expected!r}, got {compaction.get(key)!r}")


async def _run_agent_reactive_case(sandbox: ScenarioSandbox, state: dict[str, Any], *, cancel: bool) -> dict[str, Any]:
    original_measure = agent_context_module.measure_request_input_tokens
    config = state.get("config") or await core.resolve_config({"runtimeHome": str(sandbox.home), "permissionMode": "bypassPermissions"}, str(sandbox.workspace))
    session = core.create_empty_session(cwd=str(sandbox.workspace), provider=config["provider"], providerProfileName=config.get("providerProfileName"), model=config["model"], effort=config.get("effort"), systemPrompt=config["systemPrompt"], permissionMode="bypassPermissions")
    session["messages"] = build_context_messages("plain", 22, "/tmp/workspace")
    provider_requests: list[dict[str, Any]] = []
    events: list[tuple[str, dict[str, Any]]] = []
    signal = SimpleNamespace(aborted=False, reason=None)
    measure_calls = {"count": 0}

    async def fake_measure(_args: dict[str, Any]) -> dict[str, Any]:
        measure_calls["count"] += 1
        if cancel and measure_calls["count"] == 2:
            signal.aborted = True
            error = RuntimeError("cancelled during reactive relief rebuild")
            error.name = "AbortError"  # type: ignore[attr-defined]
            raise error
        return {"inputTokens": 1_000, "source": "fake"}

    async def send(request: dict[str, Any]) -> dict[str, Any]:
        provider_requests.append({"systemHasRelief": "<context_relief>" in str(request.get("system") or ""), "messageCount": len(request.get("messages") or [])})
        if len(provider_requests) == 1:
            raise core.create_provider_request_error(
                core.create_provider_failure_record(
                    {"provider": "openai", "providerLabel": "OpenAI", "phase": "request_start", "category": "client_request", "retryable": False, "attempt": 1, "hadAssistantOutput": False, "hadToolUse": False, "rawMessage": "maximum context length exceeded"}
                )
            )
        return {"model": request["model"], "content": [{"type": "text", "text": "recovered"}], "usage": {}, "stopReason": "end"}

    try:
        agent_context_module.measure_request_input_tokens = fake_measure
        result = await core.run_agent_prompt(
            session=session,
            config=config,
            runtime=bind_agent_runtime(
                session,
                interactive=False,
                askQuestion=lambda *_: "",
                emitEvent=lambda event, payload: events.append((event, _json_safe(payload))),
                tools=[],
                abortSignal=signal,
            ),
            prompt="continue",
            providerOverride={"send": send},
        )
    finally:
        agent_context_module.measure_request_input_tokens = original_measure
    return {
        "driver": "run_reactive_context_cancellation" if cancel else "run_reactive_context_retry",
        "status": "passed",
        "result": _json_safe(result),
        "requestCount": len(provider_requests),
        "providerRequests": provider_requests,
        "events": [{"event": event, "data": payload} for event, payload in events],
        "measureCalls": measure_calls["count"],
        "runtimeRestore": _json_safe(session.get("metadata", {}).get("runtimeRestore") or {}),
        "messagesUnchanged": session["messages"] == build_context_messages("plain", 22, "/tmp/workspace"),
    }


async def _capture_compaction_artifacts(state: dict[str, Any], alias: str, session: dict[str, Any], result: dict[str, Any]) -> None:
    manifest = await core.load_compaction_manifest(session["id"])
    checkpoint = None
    if manifest and manifest.get("latestGeneration"):
        path = Path(core.compaction_checkpoint_path(session["id"], int(manifest["latestGeneration"])))
        if path.exists():
            checkpoint = json.loads(path.read_text(encoding="utf-8"))
    artifacts = {}
    for name, path in (result.get("artifacts") or {}).items():
        artifacts[name] = _file_snapshot(path)
    state.setdefault("compaction_artifacts", []).append({"alias": alias, "manifest": _json_safe(manifest), "checkpoint": _json_safe(checkpoint), "files": artifacts, "sessionMetadata": _json_safe(session.get("metadata", {}).get("compactionState") or {})})


async def _capture_durable_memory(state: dict[str, Any], session: dict[str, Any], prompt: str) -> None:
    records = await core.load_project_memory_records(session.get("cwd"))
    retrieved = await core.retrieve_relevant_durable_memory({"session": session, "prompt": prompt})
    state["durable_memory"] = {"records": _json_safe(records), "retrieved": _json_safe(retrieved), "renderedPrompt": core.render_retrieved_memory_prompt(retrieved)}


def _file_snapshot(path: str) -> dict[str, Any]:
    candidate = Path(path)
    if not candidate.exists():
        return {"path": path, "exists": False}
    text = candidate.read_text(encoding="utf-8")
    record: dict[str, Any] = {"path": path, "exists": True, "size": len(text)}
    if candidate.suffix == ".json":
        try:
            parsed = json.loads(text)
            record["json"] = _json_safe(parsed if candidate.name != "state.json" else {"generation": parsed.get("generation"), "verification": parsed.get("verification"), "assessment": parsed.get("assessment")})
        except Exception:
            pass
    else:
        lines = text.splitlines()
        record["lineCount"] = len(lines)
        record["headings"] = [line.strip() for line in lines if line.lstrip().startswith("#")][:12]
    return record


def _compact_result(result: dict[str, Any]) -> dict[str, Any]:
    return {key: _json_safe(value) for key, value in result.items() if key != "artifacts"} | {"artifacts": result.get("artifacts") or {}}


def _current_session(state: dict[str, Any]) -> dict[str, Any]:
    session = state.get("session")
    if not isinstance(session, dict):
        raise ScenarioAssertionError("No context session is available", category="driver")
    return session


def _session_summary(alias: str, profile: str, session: dict[str, Any]) -> dict[str, Any]:
    return {"alias": alias, "profile": profile, "id": session["id"], "messageCount": len(session.get("messages") or []), "todoCount": len(session.get("todos") or []), "metadata": _json_safe(session.get("metadata") or {})}


def _select_context_record(state: dict[str, Any], data: dict[str, Any]) -> dict[str, Any]:
    family = str(data.get("family") or "continuation")
    records = state.get({"compaction": "compaction_results", "agent": "context_agent_results", "driver": "context_driver_results"}.get(family, "continuation_results")) or []
    if data.get("alias"):
        records = [record for record in records if record.get("alias") == data["alias"]]
    if not records:
        raise ScenarioAssertionError(f"No context record found for family {family!r}")
    return records[int(data.get("index", len(records) - 1))]


def _artifact_for_alias(state: dict[str, Any], alias: str) -> dict[str, Any]:
    for artifact in reversed(state.get("compaction_artifacts") or []):
        if artifact.get("alias") == alias:
            return artifact
    raise ScenarioAssertionError(f"No compaction artifact found for alias {alias!r}")


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
