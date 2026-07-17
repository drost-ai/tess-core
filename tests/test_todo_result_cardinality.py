from __future__ import annotations

import copy
import asyncio
import json
import uuid
from functools import wraps

import pytest

import agent_core as core
from agent_core.agent_tool_results import finalizeToolDispatchResult
from agent_core.shell_surface.status import buildTodosReport
from agent_core.tool_result_guard import (
    TOOL_RESULT_FINAL_BYTES,
    guardToolCallResult,
    serializedContentBytes,
)


def _run_async(function):
    @wraps(function)
    def wrapper(*args, **kwargs):
        return asyncio.run(function(*args, **kwargs))

    return wrapper


def _todo(index: int) -> dict[str, str]:
    status = "in_progress" if index % 3 == 0 else "pending" if index % 3 == 1 else "completed"
    return {"content": f"todo-{index}", "status": status, "activeForm": f"doing-{index}"}


def _session(cwd: str) -> dict:
    return core.createEmptySession(
        cwd=cwd,
        provider="openai",
        model="tiny-model",
        effort="medium",
        systemPrompt="test",
        permissionMode="bypassPermissions",
        sessionId=str(uuid.uuid4()),
        originalCwd=cwd,
    )


def _small_config(cwd: str) -> dict:
    return {
        "cwd": cwd,
        "runtimeHome": cwd,
        "allowedRoots": [cwd],
        "provider": "openai",
        "providerKind": "openai",
        "openaiApiMode": "chat_completions",
        "openaiBaseUrl": "https://example.invalid",
        "model": "tiny-model",
        "sessionPersistence": True,
    }


def _todo_tool():
    return next(tool for tool in core.createBuiltinTools() if tool.name == "TodoWrite")


def _assert_no_finite_repetition_keywords(schema: object) -> None:
    if not isinstance(schema, dict):
        return
    assert "maxItems" not in schema
    assert "maxLength" not in schema
    _assert_no_finite_repetition_keywords(schema.get("items"))
    for child in (schema.get("properties") or {}).values():
        _assert_no_finite_repetition_keywords(child)


@_run_async
async def test_todo_write_enforces_runtime_limits_and_returns_bounded_summary(tmp_path):
    tool = _todo_tool()
    todos_schema = tool.inputSchema["properties"]["todos"]
    item_properties = todos_schema["items"]["properties"]
    _assert_no_finite_repetition_keywords(tool.inputSchema)
    assert "at most 100 items" in todos_schema["description"].lower()
    assert "at most 2000 utf-8 bytes" in item_properties["content"]["description"].lower()
    assert "at most 1000 utf-8 bytes" in item_properties["activeForm"]["description"].lower()

    session = _session(str(tmp_path))
    persisted = 0

    async def persist() -> None:
        nonlocal persisted
        persisted += 1

    result = await tool.handler(
        {"todos": [_todo(index) for index in range(100)]},
        {"session": session, "persistSession": persist},
    )
    payload = json.loads(result["content"])
    assert persisted == 1
    assert len(session["todos"]) == 100
    assert payload["originalItemCount"] == 100
    assert payload["returnedItemCount"] == 20
    assert payload["omittedItemCount"] == 80
    assert payload["itemsTruncated"] is True
    assert len(payload["todos"]) == 20
    assert payload["statusCounts"] == {
        "pending": 33,
        "inProgress": 34,
        "completed": 33,
        "unknown": 0,
    }
    assert serializedContentBytes(result["content"]) <= TOOL_RESULT_FINAL_BYTES["large"]
    assert session["metadata"]["activeTask"]["content"] == "todo-0"

    before = copy.deepcopy(session["todos"])
    with pytest.raises(ValueError, match=r"at most 100 items; received 101"):
        await tool.handler(
            {"todos": [_todo(index) for index in range(101)]},
            {"session": session, "persistSession": persist},
        )
    assert session["todos"] == before
    assert persisted == 1

    with pytest.raises(ValueError, match=r"content exceeds the 2000-byte UTF-8 limit \(2004 bytes\)"):
        await tool.handler(
            {"todos": [{**_todo(0), "content": "🙂" * 501}]},
            {"session": session, "persistSession": persist},
        )
    with pytest.raises(ValueError, match=r"activeForm exceeds the 1000-byte UTF-8 limit"):
        await tool.handler(
            {"todos": [{**_todo(0), "activeForm": "a" * 1_001}]},
            {"session": session, "persistSession": persist},
        )
    with pytest.raises(ValueError, match=r"Invalid todos\[0\]\.status"):
        await tool.handler(
            {"todos": [{**_todo(0), "status": "almost_done"}]},
            {"session": session, "persistSession": persist},
        )
    assert persisted == 1


@_run_async
async def test_worst_case_todo_summary_bypasses_generic_reshaping(tmp_path):
    tool = _todo_tool()
    session = _session(str(tmp_path))
    result = await tool.handler(
        {
            "todos": [
                {**_todo(index), "content": "\\" * 2_000, "activeForm": '"' * 1_000}
                for index in range(100)
            ]
        },
        {"session": session, "persistSession": lambda: None},
    )
    assert serializedContentBytes(result["content"]) <= TOOL_RESULT_FINAL_BYTES["small"]
    payload = json.loads(result["content"])
    assert payload["returnedItemCount"] == 20
    guarded = await guardToolCallResult(
        config=_small_config(str(tmp_path)),
        sessionId=session["id"],
        toolName="TodoWrite",
        outcome={"kind": "returned", "result": result},
    )
    assert guarded["truncated"] is False
    assert guarded["cardinality"]["originalItemCount"] == 100
    assert guarded["cardinality"]["returnedItemCount"] == 20


@_run_async
async def test_generic_byte_small_nested_json_is_structurally_bounded(tmp_path):
    raw = json.dumps(
        {"rows": [{"index": index, "nested": {"label": f"row-{index}"}} for index in range(140)]},
        separators=(",", ":"),
    )
    assert serializedContentBytes(raw) < TOOL_RESULT_FINAL_BYTES["small"]
    captured = None

    async def artifact_writer(values: dict) -> dict:
        nonlocal captured
        captured = values
        return {
            "artifactId": "artifact-cardinality",
            "toolName": values["toolName"],
            "sessionId": values["sessionId"],
            "family": values["family"],
            "filePath": "/tmp/cardinality.json",
            "bytes": len(values["content"].encode()),
            "lines": 1,
            "contentType": values["contentType"],
            "createdAt": "2026-01-01T00:00:00Z",
        }

    guarded = await guardToolCallResult(
        config=_small_config(str(tmp_path)),
        sessionId=str(uuid.uuid4()),
        toolName="generic_rows",
        outcome={"kind": "returned", "result": {"content": raw}},
        artifactWriter=artifact_writer,
    )
    assert guarded["truncated"] is True
    assert guarded["cardinality"]["itemsTruncated"] is True
    assert guarded["cardinality"]["originalItemCount"] > guarded["cardinality"]["returnedItemCount"]
    assert captured["content"] == raw
    payload = json.loads(guarded["result"]["content"])
    assert payload["type"] == "structured_result_summary"
    assert payload["artifact"]["artifactId"] == "artifact-cardinality"
    assert payload["returnedItemCount"] <= 60
    assert serializedContentBytes(guarded["result"]["content"]) <= TOOL_RESULT_FINAL_BYTES["small"]


@_run_async
async def test_finalization_records_emits_and_types_cardinality(tmp_path):
    emitted: list[dict] = []
    recorded: list[dict] = []
    completed = await finalizeToolDispatchResult(
        config=_small_config(str(tmp_path)),
        sessionId=str(uuid.uuid4()),
        assistantMessageIndex=1,
        toolUse={"type": "tool_use", "id": "structured-call", "name": "structured_rows", "input": {}},
        outcome={
            "kind": "returned",
            "result": {"content": json.dumps([{"index": index} for index in range(120)])},
        },
        artifactWriter=lambda values: {
            "artifactId": "artifact-event",
            "toolName": values["toolName"],
            "sessionId": values["sessionId"],
            "family": values["family"],
            "filePath": "/tmp/event.json",
            "bytes": len(values["content"].encode()),
            "lines": 1,
            "contentType": values["contentType"],
            "createdAt": "2026-01-01T00:00:00Z",
        },
        record=recorded.append,
        emit=lambda event, data: emitted.append({"event": event, "data": data}),
    )
    event = next(entry for entry in emitted if entry["event"] == "tool.completed")
    assert event["data"]["original_item_count"] == completed["cardinality"]["originalItemCount"]
    assert event["data"]["returned_item_count"] == completed["cardinality"]["returnedItemCount"]
    assert event["data"]["items_truncated"] is True
    assert recorded[0]["original_item_count"] == completed["cardinality"]["originalItemCount"]
    typed = core.createRuntimeEvent({"event": event["event"], "data": event["data"]})
    assert typed["kind"] == "tool.completed"
    assert typed["payload"]["originalItemCount"] == completed["cardinality"]["originalItemCount"]
    assert typed["payload"]["returnedItemCount"] == completed["cardinality"]["returnedItemCount"]
    assert typed["payload"]["itemsTruncated"] is True


def test_legacy_todo_display_is_bounded_without_mutation(tmp_path):
    session = _session(str(tmp_path))
    session["todos"] = [
        {**_todo(index), "content": "legacy-" + "漢" * 2_000 if index == 0 else f"legacy-{index}"}
        for index in range(140)
    ]
    before = copy.deepcopy(session["todos"])
    report = buildTodosReport(session)
    assert "120 omitted (20 of 140 shown)" in report
    assert len(report.encode("utf-8")) < 50_000
    assert session["todos"] == before
