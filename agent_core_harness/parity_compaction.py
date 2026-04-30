from __future__ import annotations

import copy
import os
from pathlib import Path
from typing import Any

import agent_core as core


async def run_compaction_primitives(data: dict[str, Any], paths: dict[str, Any]) -> dict[str, Any]:
    config, session = await _build_session(data, paths)
    compacted = _with_compaction_state(session, paths)
    plan = core.resolveCompactionExecutionPlan({"config": config, "session": session})
    configured = {**config, "compactionModel": data.get("configuredModel", "gpt-5.4"), "compactionVerificationModel": data.get("configuredVerificationModel", "gpt-5.4-mini")}
    system_none = await core.buildCompactionSystemPromptContext(session)
    system_summary = await core.buildCompactionSystemPromptContext(compacted)
    return _clean(
        {
            "name": data.get("name") or data.get("action"),
            "modelPlan": {
                "default": _project_plan(plan),
                "configured": _project_plan(core.resolveCompactionExecutionPlan({"config": configured, "session": session})),
            },
            "modelCandidates": {
                "synthesis": [_project_model(item) for item in core.resolveCompactionModelCandidates({"config": config, "session": session, "purpose": "synthesis"})],
                "verification": [_project_model(item) for item in core.resolveCompactionModelCandidates({"config": config, "session": session, "purpose": "verification"})],
            },
            "tokenEstimates": {
                "plain": core.estimateWorkingContextTokens(session),
                "compacted": core.estimateWorkingContextTokens(compacted),
                "messageCount": len(session["messages"]),
                "recentMessageCount": len(compacted["messages"]) - compacted["metadata"]["compactionState"]["compactedThroughMessageIndex"],
            },
            "autoCompact": {
                "below": core.shouldAutoCompact(session, 42_000),
                "soft": core.shouldAutoCompact(session, 151_000),
                "hard": core.shouldAutoCompact(session, 181_000),
                "suppressed": core.shouldAutoCompact(compacted, 151_000),
            },
            "workingMessages": {
                "plain": _project_messages(core.buildMessagesForWorkingContext(session)),
                "compacted": _project_messages(core.buildMessagesForWorkingContext(compacted)),
            },
            "systemPromptContext": {
                "withoutState": _project_system_context(system_none),
                "summaryFallback": _project_system_context(system_summary),
            },
        }
    )


async def _build_session(data: dict[str, Any], paths: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    config = await core.resolveConfig(
        {
            "provider": data.get("provider", "openai"),
            "model": data.get("model", "gpt-5.4"),
            "permissionMode": data.get("permissionMode", "bypassPermissions"),
            "runtimeHome": str(paths["python_home"]),
        },
        str(paths["python_workspace"]),
    )
    session = core.createEmptySession(
        {
            "cwd": str(paths["python_workspace"]),
            "provider": config["provider"],
            "providerProfileName": config.get("providerProfileName"),
            "model": config["model"],
            "effort": config.get("effort"),
            "systemPrompt": config["systemPrompt"],
            "permissionMode": config["permissionMode"],
            "sessionId": data.get("sessionId") or "compaction-primitives-session",
        }
    )
    session["createdAt"] = "2026-01-01T00:00:00.000Z"
    session["updatedAt"] = "2026-01-01T00:00:01.000Z"
    session["messages"] = _messages()
    session["todos"] = [{"id": "todo-1", "text": "Build compaction parity primitives", "status": "in_progress"}]
    session.setdefault("metadata", {})["workspaceName"] = data.get("workspaceName") or "Compaction Parity"
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


def _with_compaction_state(session: dict[str, Any], paths: dict[str, Any]) -> dict[str, Any]:
    compacted = copy.deepcopy(session)
    summary = Path(core.getSessionCompactionsDir(compacted["id"])) / "summary-primitive.md"
    os.makedirs(summary.parent, exist_ok=True)
    summary.write_text("# Compacted Summary\n\n- Preserve checkpoint-v2 primitive parity.\n- Continue with deterministic fake model clients.\n", encoding="utf-8")
    compacted["metadata"]["compactionState"] = {
        "generation": 1,
        "compactedThroughMessageIndex": 1,
        "archiveMessageCount": len(compacted["messages"]) - 4,
        "compactedContextTokenEstimate": 321,
        "workingContextTokenEstimate": 500,
        "workingContextTokenMeasurement": {"inputTokens": 150_500, "source": "scripted"},
        "artifacts": {"summary": str(summary), "workingContext": str(paths["python_home"] / "missing-working-context.json")},
    }
    return compacted


def _project_plan(plan: dict[str, Any]) -> dict[str, Any]:
    return {key: _project_model(plan[key]) for key in ("synthesis", "verification")}


def _project_model(model: dict[str, Any]) -> dict[str, Any]:
    return {key: model.get(key) for key in ("provider", "model", "effort", "purpose", "selectionSource") if model.get(key) is not None}


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
