from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import agent_core._facade as core
from agent_core.providers.errors import create_provider_failure_record, create_provider_request_error

from .parity_compaction_fixtures import build_compaction_authority_states


async def run_compaction_primitives(data: dict[str, Any], paths: dict[str, Any]) -> dict[str, Any]:
    config, session = await _build_session(data, paths)
    compacted_states = build_compaction_authority_states(session, paths)
    compacted = compacted_states["ownedWorkingContext"]
    plan = core.resolve_compaction_execution_plan({"config": config, "session": session})
    configured = {**config, "compactionModel": data.get("configuredModel", "gpt-5.4"), "compactionVerificationModel": data.get("configuredVerificationModel", "gpt-5.4-mini")}
    system_none = await core.build_compaction_system_prompt_context(session)
    system_contexts = {
        name: await core.build_compaction_system_prompt_context(candidate)
        for name, candidate in compacted_states.items()
    }
    return _clean(
        {
            "name": data.get("name") or data.get("action"),
            "modelPlan": {
                "default": _project_plan(plan),
                "configured": _project_plan(core.resolve_compaction_execution_plan({"config": configured, "session": session})),
            },
            "modelCandidates": {
                "synthesis": [_project_model(item) for item in core.resolve_compaction_model_candidates({"config": config, "session": session, "purpose": "synthesis"})],
                "verification": [_project_model(item) for item in core.resolve_compaction_model_candidates({"config": config, "session": session, "purpose": "verification"})],
            },
            "tokenEstimates": {
                "plain": core.estimate_working_context_tokens(session),
                "compacted": core.estimate_working_context_tokens(compacted),
                "messageCount": len(session["messages"]),
                "recentMessageCount": len(compacted["messages"]) - compacted["metadata"]["compactionState"]["compactedThroughMessageIndex"],
            },
            "autoCompact": {
                "below": core.should_auto_compact(session, 42_000),
                "soft": core.should_auto_compact(session, 151_000),
                "hard": core.should_auto_compact(session, 181_000),
                "suppressed": core.should_auto_compact(compacted, 151_000),
            },
            "workingMessages": {
                "plain": _project_messages(core.build_messages_for_working_context(session)),
                "compacted": _project_messages(core.build_messages_for_working_context(compacted)),
            },
            "systemPromptContext": {
                "withoutState": _project_system_context(system_none),
                **{
                    name: _project_system_context(value)
                    for name, value in system_contexts.items()
                },
            },
        }
    )


async def run_compaction_run(data: dict[str, Any], paths: dict[str, Any]) -> dict[str, Any]:
    config, session = await _build_session_for(data, paths, default_session_id="compaction-checkpoint-v2-session")
    fake_client = _FakeCompactionModelClient(data.get("script") or {})
    outcome = await core.run_compaction(
        {
            "session": session,
            "config": config,
            "trigger": data.get("trigger") or "manual",
            "force": data.get("force", True),
            "modelClient": fake_client,
        }
    )
    artifacts = _load_compaction_artifacts(outcome)
    return _clean(
        {
            "name": data.get("name") or data.get("action"),
            "outcome": _project_outcome(outcome),
            "attempts": _project_attempts(fake_client.records),
            "finalOutcome": _project_final_outcome(outcome),
            "retryLineage": _project_retry_lineage(fake_client.records),
            "repair": _project_repair(artifacts.get("state") or {}),
            "skips": _project_skip(outcome, fake_client.records),
            "models": {
                "compaction": _project_model(outcome.get("compactionModel") or {}),
                "verification": _project_model(outcome.get("verificationModel") or {}),
            },
            "verification": _project_verification(outcome.get("verification") or {}),
            "artifacts": _project_artifacts(outcome.get("artifacts") or {}, artifacts),
            "workingContext": _project_working_context(artifacts.get("workingContext") or {}),
            "checkpoint": _project_checkpoint(artifacts.get("checkpoint") or {}),
            "sessionMetadata": _project_compaction_metadata(session),
            "modelCalls": fake_client.records,
        }
    )


async def _build_session(data: dict[str, Any], paths: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    return await _build_session_for(data, paths, default_session_id="compaction-primitives-session")


async def _build_session_for(data: dict[str, Any], paths: dict[str, Any], *, default_session_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
    config = await core.resolveConfig(
        {
            "provider": data.get("provider", "openai"),
            "model": data.get("model", "gpt-5.4"),
            "permissionMode": data.get("permissionMode", "bypassPermissions"),
            "runtimeHome": str(paths["python_home"]),
        },
        str(paths["python_workspace"]),
    )
    session = core.create_empty_session(
        {
            "cwd": str(paths["python_workspace"]),
            "provider": config["provider"],
            "providerProfileName": config.get("providerProfileName"),
            "model": config["model"],
            "effort": config.get("effort"),
            "systemPrompt": config["systemPrompt"],
            "permissionMode": config["permissionMode"],
            "sessionId": data.get("sessionId") or default_session_id,
        }
    )
    session["createdAt"] = "2026-01-01T00:00:00.000Z"
    session["updatedAt"] = "2026-01-01T00:00:01.000Z"
    session["messages"] = _messages_for_fixture(data.get("fixture"))
    session["todos"] = [{"id": "todo-1", "text": "Build compaction parity primitives", "status": "in_progress"}]
    metadata = session.setdefault("metadata", {})
    metadata["workspaceName"] = data.get("workspaceName") or "Compaction Parity"
    return config, session


def _messages() -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    rel_path = "agent_core/context/compaction.py"
    for index in range(12):
        messages.append({"role": "user", "content": f"review compaction file {index} in {rel_path}"})
        if index % 3 == 1:
            tool_id = f"read-{index}"
            messages.append({"role": "assistant", "content": [{"type": "tool_use", "id": tool_id, "name": "Read", "input": {"file_path": rel_path, "offset": index}}]})
            messages.append({"role": "user", "content": [{"type": "tool_result", "tool_use_id": tool_id, "content": f"line {index}: checkpoint-v2 detail with deterministic evidence"}]})
        else:
            messages.append({"role": "assistant", "content": f"noted checkpoint-v2 detail {index}; next validate parity evidence"})
    messages.append({"role": "user", "content": "continue with live compaction parity checks"})
    messages.append({"role": "assistant", "content": "ready to compare primitive projections"})
    return messages


def _messages_for_fixture(name: Any) -> list[dict[str, Any]]:
    if name == "unsafe-cutoff-session":
        messages = [{"role": "user", "content": "bootstrap context before unsafe tool tail"}]
        for index in range(24):
            tool_id = f"todo-{index}"
            messages.append({"role": "assistant", "content": [{"type": "tool_use", "id": tool_id, "name": "TodoWrite", "input": {"items": [f"low-value update {index}"]}}]})
        return messages
    if name == "short-session":
        return [{"role": "user", "content": "too short"}, {"role": "assistant", "content": "ack"}]
    return _messages()


def _project_plan(plan: dict[str, Any]) -> dict[str, Any]:
    return {key: _project_model(plan[key]) for key in ("synthesis", "verification")}


def _project_model(model: dict[str, Any]) -> dict[str, Any]:
    return {key: model.get(key) for key in ("provider", "model", "effort", "purpose", "selectionSource") if model.get(key) is not None}


class _FakeCompactionModelClient:
    def __init__(self, script: dict[str, Any]) -> None:
        self.script = script
        self.offsets: dict[str, int] = {}
        self.records: list[dict[str, Any]] = []

    async def complete(self, payload: dict[str, Any]) -> dict[str, Any]:
        model = payload["model"]
        purpose = model.get("purpose") or "synthesis"
        offset = self.offsets.get(purpose, 0)
        self.offsets[purpose] = offset + 1
        step = _script_step(self.script, purpose, offset)
        kind = step.get("kind") or "success"
        self.records.append(
            {
                "purpose": purpose,
                "provider": model.get("provider"),
                "model": model.get("model"),
                "effort": model.get("effort"),
                "attemptIndex": offset,
                "outcome": kind,
                "textFixture": step.get("textFixture"),
                "usage": _usage(step, purpose),
                "prompt": _project_prompt(payload.get("prompt") or "", payload.get("system") or ""),
            }
        )
        if kind == "malformed_json":
            return {"text": "{not valid json", "usage": _usage(step, purpose), "model": model}
        if kind == "incomplete_context":
            return {"text": json.dumps({"shouldCheckpoint": True, "quality": {"classification": "usable", "reason": "missing context"}, "workingContext": None}), "usage": _usage(step, purpose), "model": model}
        if kind == "retryable_error":
            raise _provider_error("server", "fake retryable compaction failure")
        if kind == "context_size_error":
            raise _provider_error("client_request", "maximum context length exceeded")
        if kind == "reject":
            return {"text": json.dumps(_verification_fixture("reject-needs-repair"), indent=2), "usage": _usage(step, purpose), "model": model}
        text = _fixture_text(str(step.get("textFixture") or ("rich-working-context" if purpose == "synthesis" else "accept-rich")))
        return {"text": text, "usage": _usage(step, purpose), "model": model}


def _script_step(script: dict[str, Any], purpose: str, offset: int) -> dict[str, Any]:
    steps = script.get(purpose)
    if isinstance(steps, list) and steps:
        return steps[min(offset, len(steps) - 1)] or {}
    return {"kind": "success", "textFixture": "rich-working-context" if purpose == "synthesis" else "accept-rich"}


def _fixture_text(name: str) -> str:
    if name.startswith("accept") or name.startswith("reject") or name.startswith("repair"):
        return json.dumps(_verification_fixture(name), indent=2)
    return json.dumps(_synthesis_fixture(name), indent=2)


def _synthesis_fixture(name: str) -> dict[str, Any]:
    if name == "skip-low-value":
        return {
            "shouldCheckpoint": False,
            "quality": {"classification": "thin", "reason": "Compacted slice is too thin to justify a checkpoint."},
            "workingContext": _working_context_fields(),
        }
    quality = "usable" if name == "usable-working-context" else "rich"
    return {"shouldCheckpoint": True, "quality": {"classification": quality, "reason": "Deterministic checkpoint fixture preserves implementation state."}, "workingContext": _working_context_fields()}


def _verification_fixture(name: str) -> dict[str, Any]:
    if name == "repair-rich":
        repaired = _working_context_fields()
        repaired["nextSteps"] = ["Persist the verifier-repaired checkpoint.", "Continue with Slice C fallback coverage."]
        repaired["criticalContext"] = [*repaired["criticalContext"], "Verifier repair replaced stale next steps."]
        return {
            "verdict": "repair",
            "confidence": "medium",
            "reason": "Candidate had stale next steps; repaired checkpoint supplied.",
            "issues": ["Stale next step"],
            "notes": ["Repair keeps the active Slice C objective."],
            "repairedCheckpoint": {
                "quality": {"classification": "rich", "reason": "Verifier repaired stale continuation state."},
                "workingContext": repaired,
            },
        }
    if name == "reject-needs-repair":
        return {"verdict": "reject", "confidence": "medium", "reason": "Checkpoint omitted the active Slice B next step.", "issues": ["Missing active next step"], "notes": []}
    return {
        "verdict": "accept_with_notes",
        "confidence": "high",
        "reason": "Checkpoint-v2 fixture preserves current goals, files, decisions, and next steps.",
        "issues": [],
        "notes": ["Artifact families are deterministic.", "Recent raw messages remain outside the checkpoint."],
    }


def _working_context_fields() -> dict[str, Any]:
    return {
        "project": {
            "description": "Python parity port for the TypeScript agent-core runtime.",
            "facts": {"primaryLanguage": "Python", "languages": ["Python", "TypeScript"], "ecosystem": "agent-core", "packageManager": "stdlib", "evidence": ["Harness parity compares Python against TypeScript dist/testing.js."]},
            "keyFiles": [
                {"path": "agent_core/context/compaction/orchestration.py", "role": "modified", "note": "Python checkpoint-v2 orchestration"},
                {"path": "agent_core_harness/parity_compaction.py", "role": "modified", "note": "Live parity compaction driver"},
            ],
        },
        "goals": {
            "active": ["Finish Slice B checkpoint-v2 live parity."],
            "recentRequests": ["Build the next compaction parity pack end-to-end."],
            "constraints": ["Never call live providers from parity scenarios.", "Keep edited files below 500 lines."],
            "preferences": ["Use committed goldens for stable comparison."],
        },
        "currentState": {
            "completed": ["Slice A primitives parity is committed and passing."],
            "inProgress": ["Running full compaction through deterministic fake model clients."],
            "blocked": [],
        },
        "decisions": ["Use dist/testing.js for TypeScript compaction exports.", "Normalize artifact projections instead of raw host paths."],
        "discoveries": ["Absolute workspace path length can alter token estimates.", "Checkpoint artifacts include summary, decisions, activity, todos, state, working context, plan, manifest, and checkpoint."],
        "activity": {
            "commands": ["python3 -m agent_core_harness parity parity.compaction-checkpoint-v2-pack"],
            # Fixture prose compared byte-for-byte against the TS peer — the
            # runCompaction here is search-command DATA, not this port's API.
            "searches": ["grep -RIn runCompaction agent_core /Users/dev/agents/agent-core/src"],
            "web": [],
            "failures": [],
        },
        "nextSteps": ["Commit the checkpoint-v2 parity pack.", "Use Slice C for repair and fallback failures."],
        "criticalContext": ["The fake model client records prompt signals and usage without sending network requests."],
    }


def _usage(step: dict[str, Any], purpose: str) -> dict[str, int]:
    usage = step.get("usage") if isinstance(step.get("usage"), dict) else {}
    default = {"input_tokens": 100 if purpose == "synthesis" else 50, "output_tokens": 40 if purpose == "synthesis" else 12}
    return {**default, **usage}


def _provider_error(category: str, raw_message: str) -> Exception:
    record = {
        "provider": "openai",
        "providerLabel": "OpenAI",
        "phase": "response_complete",
        "category": category,
        "retryable": category != "client_request",
        "attempt": 1,
        "hadAssistantOutput": False,
        "hadToolUse": False,
        "rawMessage": raw_message,
        "rawBody": raw_message,
    }
    return create_provider_request_error(create_provider_failure_record(record))


def _project_prompt(prompt: str, system: str) -> dict[str, Any]:
    lines = prompt.splitlines()
    source_range = next((line for line in lines if line.startswith("Compacted source range:")), "")
    return {
        "systemKind": "verification" if "verify" in system.lower() else "synthesis",
        "firstLine": lines[0] if lines else "",
        "sourceRange": source_range,
        "hasTodoSnapshot": "Exact current todo snapshot:" in prompt,
        "hasLatestPlan": "Latest saved plan state:" in prompt,
        "hasEvidence": "Compacted transcript evidence:" in prompt,
        "hasCandidate": "Candidate checkpoint:" in prompt,
        "hasPreviousError": "Previous output error:" in prompt,
    }


def _load_compaction_artifacts(outcome: dict[str, Any]) -> dict[str, Any]:
    paths = outcome.get("artifacts") or {}
    return {
        "summary": _read_text(paths.get("summary")),
        "decisions": _read_text(paths.get("decisions")),
        "activity": _read_text(paths.get("activity")),
        "plan": _read_text(paths.get("plan")),
        "todos": _read_json(paths.get("todos")),
        "state": _read_json(paths.get("state")),
        "workingContext": _read_json(paths.get("workingContext")),
        "manifest": _read_json(paths.get("manifest")),
        "checkpoint": _read_json(paths.get("checkpoint")),
    }


def _project_outcome(outcome: dict[str, Any]) -> dict[str, Any]:
    keys = ("status", "trigger", "reason", "detail", "previousCutoff", "candidateCutoff", "generation", "compactedThroughMessageIndex", "compactedMessageCount", "recentMessageCount", "rawTokenEstimate", "compactedContextTokenEstimate", "workingContextTokenEstimate", "qualityClassification")
    return {key: outcome.get(key) for key in keys}


def _project_attempts(records: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "total": len(records),
        "synthesis": [record["outcome"] for record in records if record.get("purpose") == "synthesis"],
        "verification": [record["outcome"] for record in records if record.get("purpose") == "verification"],
    }


def _project_final_outcome(outcome: dict[str, Any]) -> dict[str, Any]:
    return _clean({key: outcome.get(key) for key in ("status", "reason", "trigger", "qualityClassification", "compactedThroughMessageIndex")})


def _project_retry_lineage(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "purpose": record.get("purpose"),
            "attemptIndex": record.get("attemptIndex"),
            "outcome": record.get("outcome"),
            "hasPreviousError": (record.get("prompt") or {}).get("hasPreviousError"),
        }
        for record in records
    ]


def _project_repair(state: dict[str, Any]) -> dict[str, Any]:
    verification = state.get("verification") or {}
    working_context = state.get("workingContext") or {}
    return _clean(
        {
            "repairCount": verification.get("repairCount"),
            "verdict": verification.get("verdict"),
            "nextSteps": working_context.get("nextSteps"),
            "criticalContext": working_context.get("criticalContext"),
        }
    )


def _project_skip(outcome: dict[str, Any], records: list[dict[str, Any]]) -> dict[str, Any]:
    if outcome.get("status") != "skipped":
        return {"skipped": False}
    return _clean(
        {
            "skipped": True,
            "reason": outcome.get("reason"),
            "detail": outcome.get("detail"),
            "previousCutoff": outcome.get("previousCutoff"),
            "candidateCutoff": outcome.get("candidateCutoff"),
            "qualityClassification": outcome.get("qualityClassification"),
            "modelCallCount": len(records),
        }
    )


def _project_verification(value: dict[str, Any]) -> dict[str, Any]:
    return {key: value.get(key) for key in ("verdict", "confidence", "reason", "issues", "notes")}


def _project_artifacts(paths: dict[str, str], artifacts: dict[str, Any]) -> dict[str, Any]:
    return {
        "paths": {key: {"present": bool(value), "path": value} for key, value in paths.items()},
        "summaryMarkdown": artifacts.get("summary"),
        "decisionsMarkdown": artifacts.get("decisions"),
        "activityMarkdown": artifacts.get("activity"),
        "planMarkdown": artifacts.get("plan"),
        "todosJson": artifacts.get("todos"),
        "manifestJson": _project_manifest(artifacts.get("manifest") or {}),
        "stateJson": artifacts.get("state"),
    }


def _project_manifest(manifest: dict[str, Any]) -> dict[str, Any]:
    return _clean({key: manifest.get(key) for key in ("sessionId", "strategy", "latestGeneration", "latestCompactionId", "compactedThroughMessageIndex", "compactedMessageCount", "recentMessageCount", "archiveMessageCount", "rawTokenEstimate", "compactedContextTokenEstimate", "workingContextTokenEstimate", "lastTrigger", "qualityClassification", "compactionModel", "verificationModel", "verification", "artifacts")})


def _project_working_context(context: dict[str, Any]) -> dict[str, Any]:
    return _clean(context) if context else None


def _project_checkpoint(checkpoint: dict[str, Any]) -> dict[str, Any]:
    return _clean({key: checkpoint.get(key) for key in ("id", "generation", "sessionId", "createdAt", "trigger", "sourceRange", "rawTokenEstimate", "compactedContextTokenEstimate", "workingContextTokenEstimate", "qualityClassification", "compactionModel", "verificationModel", "verification", "artifacts", "state")})


def _project_compaction_metadata(session: dict[str, Any]) -> dict[str, Any]:
    metadata = session.get("metadata") or {}
    return _clean({"compactions": metadata.get("compactions"), "usage": metadata.get("usage"), "compactionState": metadata.get("compactionState")})


def _read_text(path: str | None) -> str | None:
    if not path:
        return None
    try:
        return Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        return None


def _read_json(path: str | None) -> Any:
    text = _read_text(path)
    return json.loads(text) if text else None


def _project_messages(messages: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "count": len(messages),
        "roles": [message.get("role") for message in messages[:6]],
        "firstContent": _content_label(messages[0].get("content")) if messages else None,
        "hasContinuationBridge": bool(messages and "Continuation context:" in str(messages[0].get("content"))),
        "toolUseCount": sum(1 for message in messages if "tool_use" in str(message.get("content"))),
        "toolResultCount": sum(1 for message in messages if "tool_result" in str(message.get("content"))),
    }


def _project_system_context(value: str | None) -> dict[str, Any]:
    if not value:
        return {"present": False}
    lines = value.splitlines()
    return {"present": True, "lineCount": len(lines), "firstLine": lines[0] if lines else "", "containsSummary": "Compacted Summary" in value, "containsPrimitiveParity": "primitive parity" in value}


def _content_label(content: Any) -> str:
    if isinstance(content, str):
        return content[:120]
    if isinstance(content, list):
        return ",".join(str(item.get("type")) for item in content if isinstance(item, dict))
    return str(content)[:120]


def _clean(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _clean(item) for key, item in value.items() if item is not None}
    if isinstance(value, list):
        return [_clean(item) for item in value]
    return value
