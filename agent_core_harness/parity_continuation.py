from __future__ import annotations

import json
from typing import Any

import agent_core._facade as core


async def run_continuation_relief(data: dict[str, Any], paths: dict[str, Any]) -> dict[str, Any]:
    config, session, messages, memory = await _build_fixture(data, paths)
    before = core.classify_context_pressure({"session": session, "messages": messages})
    lightweight = core.apply_lightweight_context_relief(messages, session)
    relief = core.apply_context_relief_modes(
        {
            "session": session,
            "config": config,
            "prompt": data.get("prompt") or "continue parity relief",
            "messages": messages,
            "retrievedMemory": memory,
            "aggressive": data.get("aggressive", False),
            "reactiveAttempt": data.get("reactiveAttempt", 0),
        }
    )
    after = core.classify_context_pressure({"session": session, "messages": relief["messages"]})
    summary = relief["summary"]
    return _clean(
        {
            "name": data.get("name") or data.get("action"),
            "pressure": {"before": _project_pressure(before), "after": _project_pressure(after), "lightweight": _project_summary(lightweight["summary"])},
            "selection": _project_summary(summary),
            "steps": [_project_step(step) for step in summary.get("steps", [])],
            "messages": _project_messages(messages, relief["messages"], summary),
            "reinjection": {
                "lines": summary.get("reinjectionLines") or [],
                "summary": summary.get("reinjectionSummary"),
                "lineCount": len(summary.get("reinjectionLines") or []),
                "hasDurableMemory": any("durable memory:" in line for line in summary.get("reinjectionLines") or []),
                "hasRelevantFiles": any("relevant files:" in line for line in summary.get("reinjectionLines") or []),
            },
        }
    )


async def _build_fixture(data: dict[str, Any], paths: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]], dict[str, Any]]:
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
            "sessionId": data.get("sessionId") or "continuation-relief-session",
        }
    )
    session["createdAt"] = "2026-01-01T00:00:00.000Z"
    session["updatedAt"] = "2999-01-01T00:00:01.000Z"
    session["todos"] = [
        {"content": "Build continuation relief parity", "activeForm": "building continuation relief parity", "status": "in_progress"},
        {"content": "Commit compaction repair pack", "activeForm": "committing compaction repair pack", "status": "completed"},
    ]
    if data.get("oldHistoryTokens"):
        session["metadata"]["compactionState"] = {"compactedContextTokenEstimate": data["oldHistoryTokens"]}
    return config, session, _messages(str(data.get("fixture") or "tool-heavy")), _retrieved_memory(bool(data.get("retrievedMemory")))


def _messages(fixture: str) -> list[dict[str, Any]]:
    if fixture == "plain-long":
        return _plain_messages(48, "plain continuation detail ")
    if fixture == "partial-long":
        return _plain_messages(44, "partial continuation file src/continuation.py command pytest ")
    return _tool_heavy_messages(10)


def _plain_messages(count: int, seed: str) -> list[dict[str, Any]]:
    return [{"role": "user" if index % 2 == 0 else "assistant", "content": f"{seed}{index} " + ("carry forward current implementation facts. " * 80)} for index in range(count)]


def _tool_heavy_messages(pairs: int) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    payload = "def checkpoint():\n    return 'continuation relief parity'\n" * 60
    for index in range(pairs):
        tool_id = f"read-{index}"
        messages.append({"role": "user", "content": f"inspect repeated payload {index}"})
        messages.append({"role": "assistant", "content": [{"type": "tool_use", "id": tool_id, "name": "Read", "input": {"file_path": "src/repeated.py", "offset": index * 10}}]})
        messages.append({"role": "user", "content": [{"type": "tool_result", "tool_use_id": tool_id, "content": json.dumps({"type": "read", "file": {"filePath": "src/repeated.py", "startLine": 1, "totalLines": 800, "numLines": 120, "content": payload}})}]})
        messages.append({"role": "assistant", "content": f"noted repeated payload {index}"})
    messages.append({"role": "user", "content": "continue with continuation relief parity"})
    return messages


def _retrieved_memory(enabled: bool) -> dict[str, Any]:
    records = [{"kind": "workflow", "source": f"memory-{index}", "text": f"durable memory item {index}: preserve Slice D context relief evidence and reinjection state."} for index in range(5)] if enabled else []
    return {"records": records, "tokenEstimate": 900 if enabled else 0}


def _project_pressure(pressure: dict[str, Any]) -> dict[str, Any]:
    return {"totalTokenEstimate": pressure.get("totalTokenEstimate"), "categories": pressure.get("categories")}


def _project_summary(summary: dict[str, Any]) -> dict[str, Any]:
    projected = {key: summary.get(key) for key in ("applied", "selectedMode", "appliedModes", "plannedModes", "pressureLevel", "reactiveAttempt", "preservedRawStartIndex", "compactedMessageCount", "droppedMessageCount")}
    projected["tokensSavedEstimate"] = _token_bucket(summary.get("tokensSavedEstimate"))
    return projected


def _project_step(step: dict[str, Any]) -> dict[str, Any]:
    projected = {key: step.get(key) for key in ("mode", "applied", "compactedMessageCount", "droppedMessageCount", "detail", "requiresReinjection")}
    projected["tokensSavedEstimate"] = _token_bucket(step.get("tokensSavedEstimate"))
    return projected


def _token_bucket(value: Any) -> str:
    amount = int(value or 0)
    if amount <= 0:
        return "none"
    if amount < 1_000:
        return "small"
    if amount < 10_000:
        return "medium"
    return "large"


def _project_messages(before: list[dict[str, Any]], after: list[dict[str, Any]], summary: dict[str, Any]) -> dict[str, Any]:
    after_text = json.dumps(after, sort_keys=True)
    return {
        "beforeCount": len(before),
        "afterCount": len(after),
        "rolesHead": [message.get("role") for message in after[:5]],
        "rolesTail": [message.get("role") for message in after[-5:]],
        "hasPartialSummary": "partial_compaction" in (summary.get("appliedModes") or []),
        "hasRepeatedFileOmission": "Repeated file content omitted" in after_text,
        "hasLatestRawTail": any("continue with continuation relief parity" in str(message.get("content")) for message in after[-4:]),
    }


def _clean(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _clean(item) for key, item in value.items() if item is not None}
    if isinstance(value, list):
        return [_clean(item) for item in value]
    return value
