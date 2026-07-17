from __future__ import annotations

import asyncio
import copy
import json
import time
from typing import Any

import agent_core as core
from agent_core.agent_loop.tool_dispatch import (
    resolve_tool_concurrency,
    run_tool_dispatch_batch,
)

from .model import ScenarioAssertionError
from .runtime_bindings import bind_agent_runtime
from .runtime_driver import ensure_session_config
from .sandbox import ScenarioSandbox


class MemoryMcpTokenStore:
    def __init__(self) -> None:
        self.records: dict[str, dict[str, Any]] = {}

    async def get(self, key: str) -> dict[str, Any] | None:
        record = self.records.get(key)
        return copy.deepcopy(record) if record is not None else None

    async def set(self, key: str, record: dict[str, Any]) -> None:
        self.records[key] = copy.deepcopy(record)

    async def delete(self, key: str) -> None:
        self.records.pop(key, None)


async def run_mcp_plan_probe(
    data: dict[str, Any], sandbox: ScenarioSandbox, state: dict[str, Any]
) -> list[str]:
    runtime = _runtime(state)
    session, config = await ensure_session_config({"permissionMode": "plan"}, sandbox, state)
    session["permissionMode"] = "plan"
    config["permissionMode"] = "plan"
    config["sessionPersistence"] = False
    definitions = runtime["createToolDefinitions"]()
    requested = [str(item) for item in data.get("runtimeNames") or []]
    selected = [tool for tool in definitions if not requested or tool.name in requested]
    if len(selected) != len(requested):
        raise ScenarioAssertionError("Package 28 plan probe could not resolve all generated tools")
    config["tools"] = [tool.name for tool in selected]
    config["allowedTools"] = list(config["tools"])
    config["disallowedTools"] = []
    events: list[dict[str, Any]] = []
    requests: list[dict[str, Any]] = []
    sends = 0

    async def send(request: dict[str, Any]) -> dict[str, Any]:
        nonlocal sends
        sends += 1
        requests.append(_json_safe(request))
        if sends == 1:
            return {
                "model": config["model"],
                "stopReason": "tool_use",
                "usage": {"input_tokens": 1, "output_tokens": 1},
                "content": [
                    {"type": "tool_use", "id": f"plan-{index}", "name": tool.name, "input": {}}
                    for index, tool in enumerate(selected)
                ],
            }
        return {
            "model": config["model"],
            "stopReason": "end_turn",
            "usage": {"input_tokens": 1, "output_tokens": 1},
            "content": [{"type": "text", "text": "plan probe complete"}],
        }

    await core.runAgentPrompt(
        session=session,
        config=config,
        runtime=bind_agent_runtime(
            session,
            tools=selected,
            interactive=False,
            emitEvent=lambda event, payload: events.append(
                {"event": event, "data": _json_safe(payload)}
            ),
            repositoryIdentity="package-28-harness",
        ),
        prompt="exercise MCP plan policy",
        providerOverride={"send": send},
    )
    results = _tool_results_from_request(requests[-1])
    decisions = [
        event
        for event in events
        if event["event"] in {"plan.mcp_tool_allowed", "plan.mcp_tool_blocked"}
    ]
    expected_reasons = [str(item) for item in data.get("expectedReasons") or []]
    observed_reasons = [event["data"].get("reason") for event in decisions]
    if expected_reasons and observed_reasons != expected_reasons:
        raise ScenarioAssertionError(
            f"Package 28 plan reasons differed: {observed_reasons!r}"
        )
    record = {
        "probe": "plan_gate",
        "status": "passed",
        "decisions": decisions,
        "toolResults": [
            {
                "toolUseId": result.get("tool_use_id"),
                "isError": bool(result.get("is_error")),
            }
            for result in results
        ],
    }
    state.setdefault("mcp_package28_results", []).append(record)
    state.setdefault("mcp_package28_events", []).extend(decisions)
    return ["mcp_package28_results", "mcp_package28_events", "transcript"]


async def run_mcp_dispatch_probe(
    _data: dict[str, Any], _sandbox: ScenarioSandbox, state: dict[str, Any]
) -> list[str]:
    parallel_started = asyncio.Event()
    exclusive_started = asyncio.Event()
    execution: list[str] = []
    exclusive_active = False
    parallel_overlapped = False
    exclusive_serialized = True

    async def dispatch(tool_use: dict[str, Any]) -> dict[str, Any]:
        nonlocal exclusive_active, parallel_overlapped, exclusive_serialized
        name = str(tool_use["name"])
        execution.append(f"{name}:start")
        if name == "parallel":
            parallel_started.set()
            await exclusive_started.wait()
            parallel_overlapped = True
        else:
            if exclusive_active:
                exclusive_serialized = False
            exclusive_active = True
            exclusive_started.set()
            await parallel_started.wait()
            exclusive_active = False
        execution.append(f"{name}:end")
        return {
            "result": {
                "type": "tool_result",
                "tool_use_id": tool_use["id"],
                "content": name,
            }
        }

    tool_uses = [
        {"id": "exclusive-1", "name": "exclusive-1"},
        {"id": "parallel", "name": "parallel"},
        {"id": "exclusive-2", "name": "exclusive-2"},
    ]
    batch = await run_tool_dispatch_batch(
        tool_uses=tool_uses,
        sequential=False,
        concurrency_of=lambda item: (
            "parallel" if item["name"] == "parallel" else "exclusive"
        ),
        run=dispatch,
    )

    async def noop(_input: dict[str, Any], _context: dict[str, Any]) -> dict[str, Any]:
        return {"content": "ok"}

    undeclared = core.ToolDefinition("custom", "custom", {"type": "object"}, noop)
    result_order = [str(item.get("tool_use_id")) for item in batch["results"]]
    if result_order != ["exclusive-1", "parallel", "exclusive-2"]:
        raise ScenarioAssertionError("Package 28 dispatch result order changed")
    if not parallel_overlapped or not exclusive_serialized:
        raise ScenarioAssertionError("Package 28 dispatch overlap/serialization contract failed")
    record = {
        "probe": "dispatch",
        "status": "passed",
        "defaultConcurrency": resolve_tool_concurrency(undeclared),
        "parallelOverlapped": parallel_overlapped,
        "exclusiveSerialized": exclusive_serialized,
        "resultOrder": result_order,
        "execution": execution,
    }
    state.setdefault("mcp_package28_results", []).append(record)
    return ["mcp_package28_results"]


async def apply_mcp_catalog_change(
    data: dict[str, Any], _sandbox: ScenarioSandbox, state: dict[str, Any]
) -> list[str]:
    runtime = _runtime(state)
    alias = str(data.get("alias") or "catalog")
    control = _control(state, alias)
    payload = control.get("payload")
    if not isinstance(payload, dict):
        raise ScenarioAssertionError("Fake MCP server does not support catalog mutation")
    before_names = _runtime_tool_names(runtime)
    before_generation = runtime["toolRegistryGeneration"]()
    payload["tools"] = copy.deepcopy(data.get("tools") or [])
    notification = {
        "jsonrpc": "2.0",
        "method": "notifications/tools/list_changed",
        "params": {"reason": "package-28-harness"},
    }
    lock = control.get("queues_lock")
    if lock:
        with lock:
            queues = list(control.get("queues") or [])
    else:
        queues = list(control.get("queues") or [])
    for item in queues:
        item.put(notification)
    deadline = time.monotonic() + float(data.get("timeoutSeconds") or 3)
    while not runtime["hasPendingCatalogChanges"]() and time.monotonic() < deadline:
        await asyncio.sleep(0.01)
    if not runtime["hasPendingCatalogChanges"]():
        raise ScenarioAssertionError("MCP catalog notification was not observed")
    during_names = _runtime_tool_names(runtime)
    applied = await runtime["applyPendingCatalogChanges"]()
    after_names = _runtime_tool_names(runtime)
    if during_names != before_names:
        raise ScenarioAssertionError("MCP catalog changed before the safe boundary")
    expected_after = [str(item) for item in data.get("expectedRuntimeNames") or []]
    if expected_after and after_names != expected_after:
        raise ScenarioAssertionError(f"MCP catalog swap differed: {after_names!r}")
    record = {
        "probe": "catalog_boundary",
        "status": "passed",
        "beforeNames": before_names,
        "duringNames": during_names,
        "afterNames": after_names,
        "generationBefore": before_generation,
        "generationAfter": runtime["toolRegistryGeneration"](),
        "applied": _json_safe(applied),
    }
    state.setdefault("mcp_package28_results", []).append(record)
    return ["mcp_package28_results", "mcp_events", "mcp_tools", "mcp_status"]


async def record_mcp_protocol_probe(
    data: dict[str, Any], _sandbox: ScenarioSandbox, state: dict[str, Any]
) -> list[str]:
    runtime = _runtime(state)
    aliases = [str(item) for item in data.get("aliases") or []]
    statuses = {item["name"]: item for item in runtime["listServerStatus"]()}
    records = []
    for alias in aliases:
        config = (state.get("mcp_server_aliases") or {}).get(alias) or {}
        name = config.get("name")
        status = statuses.get(name) or {}
        control = _control(state, alias)
        records.append(
            {
                "alias": alias,
                "state": status.get("state"),
                "protocolVersion": status.get("protocolVersion"),
                "protocolHeaders": list(control.get("protocol_headers") or []),
            }
        )
    state.setdefault("mcp_package28_results", []).append(
        {"probe": "protocol", "status": "passed", "servers": records}
    )
    return ["mcp_package28_results", "mcp_status"]


async def recover_mcp_headless_auth(
    data: dict[str, Any], _sandbox: ScenarioSandbox, state: dict[str, Any]
) -> list[str]:
    runtime = _runtime(state)
    alias = str(data.get("alias") or "oauth")
    config = (state.get("mcp_server_aliases") or {}).get(alias)
    store = state.get("mcp_token_store")
    if not isinstance(config, dict) or store is None:
        raise ScenarioAssertionError("Headless OAuth probe requires an injected memory token store")
    before = next(item for item in runtime["listServerStatus"]() if item["name"] == config["name"])
    if before.get("state") != "authorization_required":
        raise ScenarioAssertionError(f"Expected authorization_required, got {before.get('state')!r}")
    try:
        await runtime["callTool"](config["name"], str(data.get("tool") or "read"), {})
    except Exception as exc:
        fail_fast = "requires authorization" in str(exc)
    else:
        fail_fast = False
    token = str(data.get("token") or "package-28-headless-secret")
    control = _control(state, alias)
    control["accepted_tokens"].add(token)
    await store.set(
        core.mcpTokenStoreKey(config),
        {"accessToken": token, "tokenType": "Bearer", "resource": _origin(config["commandOrUrl"])},
    )
    result = await runtime["callTool"](config["name"], str(data.get("tool") or "read"), {})
    after = next(item for item in runtime["listServerStatus"]() if item["name"] == config["name"])
    record = {
        "probe": "headless_auth",
        "status": "passed",
        "beforeState": before.get("state"),
        "failFast": fail_fast,
        "afterState": after.get("state"),
        "authorizationUrlHasQuery": "?" in str(before.get("authorizationUrl") or ""),
        "resultHasStructuredContent": "structuredContent" in result,
    }
    observable = json.dumps({"record": record, "events": state.get("mcp_events") or []})
    if token in observable:
        raise ScenarioAssertionError("OAuth token leaked into Package 28 harness observations")
    if not fail_fast or after.get("state") != "connected":
        raise ScenarioAssertionError("Headless OAuth recovery contract failed")
    state.setdefault("mcp_package28_results", []).append(record)
    return ["mcp_package28_results", "mcp_events", "mcp_status"]


def _runtime(state: dict[str, Any]) -> dict[str, Any]:
    runtime = state.get("mcp_runtime")
    if not isinstance(runtime, dict):
        raise ScenarioAssertionError("No MCP runtime is available", category="driver")
    return runtime


def _control(state: dict[str, Any], alias: str) -> dict[str, Any]:
    control = (state.get("mcp_fake_controls") or {}).get(alias)
    if not isinstance(control, dict):
        raise ScenarioAssertionError(f"No fake MCP control found for {alias}")
    return control


def _runtime_tool_names(runtime: dict[str, Any]) -> list[str]:
    return [str(item["runtimeName"]) for item in runtime["listTools"]()]


def _tool_results_from_request(request: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        block
        for message in request.get("messages") or []
        if isinstance(message.get("content"), list)
        for block in message["content"]
        if isinstance(block, dict) and block.get("type") == "tool_result"
    ]


def _origin(raw: str) -> str:
    from urllib.parse import urlsplit

    parsed = urlsplit(raw)
    return f"{parsed.scheme}://{parsed.netloc}"


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
