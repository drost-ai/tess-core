from __future__ import annotations

import asyncio
import json
from typing import Any

import agent_core as core

from .mcp_fakes import start_fake_mcp_server
from .mcp_package28 import MemoryMcpTokenStore
from .model import ScenarioAssertionError
from .runtime_driver import ensure_session_config
from .sandbox import ScenarioSandbox


async def start_fake_mcp(data: dict[str, Any], sandbox: ScenarioSandbox, state: dict[str, Any]) -> list[str]:
    transport = str(data.get("transport") or "http")
    alias = str(data.get("alias") or data.get("name") or transport)
    if transport not in {"stdio", "sse", "http"}:
        raise ScenarioAssertionError(f"Unsupported fake MCP transport: {transport}", category="harness")
    config, control = start_fake_mcp_server(data, sandbox, alias)

    record = {"alias": alias, "profile": data.get("profile") or "normal", **config}
    state.setdefault("mcp_server_aliases", {})[alias] = config
    state.setdefault("mcp_server_configs", []).append(config)
    state.setdefault("mcp_servers", []).append(_json_safe(record))
    state.setdefault("mcp_fake_controls", {})[alias] = control
    state.setdefault("mcp_driver_results", []).append({"driver": "start_fake_mcp", "status": "passed", "alias": alias, "transport": transport})
    return ["mcp_servers", "mcp_driver_results"]


async def create_mcp_runtime(data: dict[str, Any], sandbox: ScenarioSandbox, state: dict[str, Any]) -> list[str]:
    await _close_runtime_only(state)
    session_id = data.get("sessionId")
    if data.get("registeredChannels") or data.get("useSession"):
        session, _config = await ensure_session_config(data, sandbox, state)
        session_id = session_id or session["id"]
    events = state.setdefault("mcp_events", [])
    token_store = None
    if data.get("memoryTokenStore"):
        token_store = state.setdefault("mcp_token_store", MemoryMcpTokenStore())
    runtime = await core.createMcpRuntime(
        {
            "cwd": str(sandbox.workspace),
            "servers": _selected_server_configs(data, state),
            "sessionId": session_id,
            "registeredChannels": data.get("registeredChannels") or [],
            "onRuntimeEvent": lambda event, payload: events.append({"event": event, "data": _json_safe(payload)}),
            **({"mcpTokenStore": token_store} if token_store is not None else {}),
        }
    )
    state["mcp_runtime"] = runtime
    _refresh_mcp_snapshots(state, runtime, data.get("label") or "create_mcp_runtime")
    state.setdefault("mcp_driver_results", []).append({"driver": "create_mcp_runtime", "status": "passed", "servers": [server["name"] for server in runtime["listServerStatus"]()]})
    return ["mcp_events", "mcp_status", "mcp_tools", "mcp_resources", "mcp_driver_results"]


async def list_mcp_tools(data: dict[str, Any], _sandbox: ScenarioSandbox, state: dict[str, Any]) -> list[str]:
    runtime = _require_runtime(state)
    state["mcp_tools"] = _json_safe(runtime["listTools"]())
    state.setdefault("mcp_driver_results", []).append({"driver": "list_mcp_tools", "status": "passed", "count": len(state["mcp_tools"])})
    return ["mcp_tools", "mcp_driver_results"]


async def list_mcp_resources(data: dict[str, Any], _sandbox: ScenarioSandbox, state: dict[str, Any]) -> list[str]:
    runtime = _require_runtime(state)
    server = _server_name(data, state)
    try:
        resources = await runtime["listResources"](server)
        record = {"driver": "list_mcp_resources", "server": server, "status": "passed", "resources": _json_safe(resources)}
    except Exception as exc:
        record = {"driver": "list_mcp_resources", "server": server, "status": "failed", "error": str(exc)}
        if not data.get("expectFailure"):
            state.setdefault("mcp_results", []).append(record)
            raise ScenarioAssertionError(f"MCP resource listing failed: {exc}", category="driver")
    state.setdefault("mcp_results", []).append(record)
    state["mcp_resources"] = record.get("resources") or state.get("mcp_resources") or []
    _refresh_mcp_snapshots(state, runtime, data.get("label") or f"list_resources:{server}")
    return ["mcp_resources", "mcp_results", "mcp_status"]


async def read_mcp_resource(data: dict[str, Any], _sandbox: ScenarioSandbox, state: dict[str, Any]) -> list[str]:
    runtime = _require_runtime(state)
    server = _server_name(data, state)
    uri = str(data.get("uri") or "")
    try:
        result = await runtime["readResource"](server, uri)
        record = {"driver": "read_mcp_resource", "server": server, "uri": uri, "status": "passed", "result": _json_safe(result)}
    except Exception as exc:
        record = {"driver": "read_mcp_resource", "server": server, "uri": uri, "status": "failed", "error": str(exc)}
        if not data.get("expectFailure"):
            state.setdefault("mcp_results", []).append(record)
            raise ScenarioAssertionError(f"MCP resource read failed: {exc}", category="driver")
    _remember_result_alias(state, data, record)
    state.setdefault("mcp_results", []).append(record)
    _refresh_mcp_snapshots(state, runtime, data.get("label") or f"read_resource:{server}")
    return ["mcp_resources", "mcp_results", "mcp_status"]


async def call_mcp_tool(data: dict[str, Any], _sandbox: ScenarioSandbox, state: dict[str, Any]) -> list[str]:
    runtime = _require_runtime(state)
    server = _server_name(data, state)
    tool = str(data.get("tool") or data.get("name") or "echo")
    args = dict(data.get("input") or {})
    try:
        result = await runtime["callTool"](server, tool, args)
        status = "failed" if result.get("isError") else "passed"
        record = {"driver": "call_mcp_tool", "server": server, "tool": tool, "status": status, "input": args, "result": _json_safe(result)}
    except Exception as exc:
        record = {"driver": "call_mcp_tool", "server": server, "tool": tool, "status": "failed", "input": args, "error": str(exc)}
        if not data.get("expectFailure"):
            state.setdefault("mcp_results", []).append(record)
            _refresh_mcp_snapshots(state, runtime, data.get("label") or f"call_tool:{server}:{tool}")
            raise ScenarioAssertionError(f"MCP tool call failed: {exc}", category="driver")
    _remember_result_alias(state, data, record)
    state.setdefault("mcp_results", []).append(record)
    _refresh_mcp_snapshots(state, runtime, data.get("label") or f"call_tool:{server}:{tool}")
    if record["status"] == "failed" and not data.get("expectFailure"):
        raise ScenarioAssertionError(f"MCP tool returned an error: {tool}", category="driver")
    return ["mcp_results", "mcp_status", "mcp_tools"]


async def call_mcp_generated_tool(data: dict[str, Any], sandbox: ScenarioSandbox, state: dict[str, Any]) -> list[str]:
    runtime = _require_runtime(state)
    session, config = await ensure_session_config(data, sandbox, state)
    name = str(data.get("runtimeName") or data.get("name") or "")
    tool = next((item for item in runtime["createToolDefinitions"]() if item.name == name), None)
    if not tool:
        raise ScenarioAssertionError(f"Generated MCP tool not found: {name}", category="driver")
    context = {
        "cwd": str(sandbox.workspace),
        "session": session,
        "config": config,
        "interactive": False,
        "abortSignal": _abort_signal(data.get("abortMode"), int(data.get("abortAfterChecks") or 2)),
        "outputJson": lambda *_args: None,
        "askQuestion": lambda *_args: "",
        "persistSession": lambda: None,
    }
    try:
        result = await tool.handler(dict(data.get("input") or {}), context)
        record = {"driver": "call_mcp_generated_tool", "tool": name, "status": "passed", "result": _json_safe(result)}
    except Exception as exc:
        record = {"driver": "call_mcp_generated_tool", "tool": name, "status": "failed", "error": str(exc), "errorName": getattr(exc, "name", exc.__class__.__name__)}
        if not data.get("expectFailure"):
            state.setdefault("mcp_results", []).append(record)
            raise ScenarioAssertionError(f"Generated MCP tool failed: {exc}", category="driver")
    state.setdefault("mcp_results", []).append(record)
    _refresh_mcp_snapshots(state, runtime, data.get("label") or f"generated_tool:{name}")
    return ["mcp_results", "mcp_status"]


async def create_mcp_plugin_fixture(data: dict[str, Any], sandbox: ScenarioSandbox, state: dict[str, Any]) -> list[str]:
    alias = str(data.get("alias") or data.get("pluginId") or "plugin")
    plugin_id = str(data.get("pluginId") or alias)
    server_config = _config_for_alias(str(data.get("serverAlias") or alias), state)
    plugin_dir = sandbox.workspace / ".harness" / "plugins" / alias
    plugin_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "id": plugin_id,
        "version": str(data.get("version") or "1.0.0"),
        "capabilities": {"mcpServers": True},
        "mcpServers": [{key: value for key, value in server_config.items() if key != "updatedAt"}],
    }
    (plugin_dir / "plugin.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    state.setdefault("mcp_plugin_dirs", []).append(str(plugin_dir))
    state.setdefault("mcp_plugin_results", []).append({"driver": "create_mcp_plugin_fixture", "status": "passed", "pluginId": plugin_id, "server": server_config["name"]})
    return ["mcp_plugin_results", "workspace_tree"]


async def bootstrap_mcp_plugin_runtime(data: dict[str, Any], sandbox: ScenarioSandbox, state: dict[str, Any]) -> list[str]:
    await _close_runtime_only(state)
    config = await core.resolveConfig(
        {
            "runtimeHome": str(sandbox.home),
            "permissionMode": data.get("permissionMode", "bypassPermissions"),
            "pluginDir": state.get("mcp_plugin_dirs") or [],
        },
        str(sandbox.workspace),
    )
    config["mcpConfig"] = _config_servers_from_data(data, state)
    session = core.createEmptySession(
        cwd=str(sandbox.workspace),
        provider=config["provider"],
        providerProfileName=config.get("providerProfileName"),
        model=config["model"],
        effort=config.get("effort"),
        systemPrompt=config["systemPrompt"],
        permissionMode=config.get("permissionMode", "bypassPermissions"),
    )
    state["config"] = config
    state["session"] = session
    events = state.setdefault("mcp_events", [])
    boot = await core.bootstrapRuntime({"session": session, "config": config, "daemonEnabled": False})
    boot["subscribeEvents"](lambda envelope: events.append({"event": envelope.get("event"), "data": _json_safe(envelope.get("data") or {})}))
    runtime = boot.get("mcpRuntime")
    state["mcp_bootstrap_runtime"] = boot
    state["mcp_runtime"] = runtime
    record = {
        "driver": "bootstrap_mcp_plugin_runtime",
        "status": "passed",
        "toolNames": [_tool_name(tool) for tool in boot.get("tools") or [] if _tool_name(tool)],
        "pluginIds": [plugin.get("id") for plugin in (boot.get("plugins") or {}).get("plugins", [])],
        "serverStatuses": _json_safe(runtime["listServerStatus"]() if runtime else []),
    }
    state.setdefault("mcp_plugin_results", []).append(record)
    if runtime:
        _refresh_mcp_snapshots(state, runtime, data.get("label") or "bootstrap_mcp_plugin_runtime")
    return ["mcp_plugin_results", "mcp_status", "mcp_tools", "mcp_events"]


def assert_mcp_status(state: dict[str, Any], data: dict[str, Any]) -> None:
    runtime = _require_runtime(state)
    _refresh_mcp_snapshots(state, runtime, data.get("label") or "assert_mcp_status")
    server = _server_name(data, state)
    status = _latest_server_status(state, server)
    if not status:
        raise ScenarioAssertionError(f"MCP status not found for server: {server}")
    for key in ("state", "connected", "protocolVersion"):
        if key in data and status.get(key) != data[key]:
            raise ScenarioAssertionError(f"MCP status {key} expected {data[key]!r}, got {status.get(key)!r}")
    if data.get("serverInfoName") and (status.get("serverInfo") or {}).get("name") != data["serverInfoName"]:
        raise ScenarioAssertionError(f"MCP serverInfo.name expected {data['serverInfoName']!r}, got {(status.get('serverInfo') or {}).get('name')!r}")
    if "minTools" in data and int(status.get("tools") or 0) < int(data["minTools"]):
        raise ScenarioAssertionError(f"MCP status expected at least {data['minTools']} tools")
    if data.get("errorContains") and data["errorContains"] not in str(status.get("error") or ""):
        raise ScenarioAssertionError(f"MCP status error did not contain {data['errorContains']!r}")


async def assert_mcp_event(data: dict[str, Any], _sandbox: ScenarioSandbox, state: dict[str, Any]) -> list[str]:
    deadline = asyncio.get_running_loop().time() + float(data.get("timeoutSeconds") or 2)
    missing: list[str] = []
    while True:
        events = state.get("mcp_events") or []
        names = [event.get("event") for event in events]
        missing = [name for name in data.get("eventsInclude", []) if name not in names]
        methods = [event.get("data", {}).get("method") for event in events]
        missing_methods = [name for name in data.get("methodsInclude", []) if name not in methods]
        if not missing and not missing_methods:
            return ["mcp_events"]
        if asyncio.get_running_loop().time() >= deadline:
            break
        await asyncio.sleep(0.05)
    raise ScenarioAssertionError(f"MCP events missing: {missing}", category="assertion")


def assert_mcp_result(state: dict[str, Any], data: dict[str, Any]) -> None:
    record = _selected_result(state, data)
    for key in ("driver", "status", "server", "tool"):
        if key in data and record.get(key) != data[key]:
            raise ScenarioAssertionError(f"MCP result {key} expected {data[key]!r}, got {record.get(key)!r}")
    body = json.dumps(record, sort_keys=True)
    if data.get("contentContains") and data["contentContains"] not in body:
        raise ScenarioAssertionError(f"MCP result did not contain {data['contentContains']!r}")
    if data.get("errorContains") and data["errorContains"] not in body:
        raise ScenarioAssertionError(f"MCP error did not contain {data['errorContains']!r}")
    if data.get("requiresBlobSavedTo") and "blobSavedTo" not in body:
        raise ScenarioAssertionError("MCP resource result did not include a saved blob path")
    if data.get("sameBlobPathAs"):
        current = _first_blob_path(record)
        previous = _first_blob_path(_aliased_result(state, data["sameBlobPathAs"]))
        if current != previous:
            raise ScenarioAssertionError(f"MCP blob path changed: {previous!r} -> {current!r}")


def assert_mcp_bootstrap(state: dict[str, Any], data: dict[str, Any]) -> None:
    records = [item for item in state.get("mcp_plugin_results") or [] if item.get("driver") == "bootstrap_mcp_plugin_runtime"]
    if not records:
        raise ScenarioAssertionError("No MCP plugin bootstrap result captured")
    record = records[-1]
    missing = sorted(set(data.get("toolNamesInclude") or []) - set(record.get("toolNames") or []))
    if missing:
        raise ScenarioAssertionError(f"MCP plugin bootstrap tools missing: {missing}")
    if data.get("serverInfoName"):
        names = [(status.get("serverInfo") or {}).get("name") for status in record.get("serverStatuses") or []]
        if data["serverInfoName"] not in names:
            raise ScenarioAssertionError(f"MCP plugin server info missing: {data['serverInfoName']!r}")


async def close_mcp_runtime(_data: dict[str, Any], _sandbox: ScenarioSandbox, state: dict[str, Any]) -> list[str]:
    await _close_runtime_only(state)
    state.setdefault("mcp_driver_results", []).append({"driver": "close_mcp_runtime", "status": "passed"})
    return ["mcp_driver_results"]


async def close_mcp_driver(state: dict[str, Any]) -> None:
    await _close_runtime_only(state)
    for control in list((state.get("mcp_fake_controls") or {}).values()):
        stop_event = control.get("stop_event")
        if stop_event:
            stop_event.set()
        server = control.get("server")
        if server:
            await asyncio.to_thread(server.shutdown)
            server.server_close()


def _selected_server_configs(data: dict[str, Any], state: dict[str, Any]) -> list[dict[str, Any]]:
    requested = data.get("servers")
    if not requested:
        return list(state.get("mcp_server_configs") or [])
    out = []
    for item in requested:
        out.append(_config_for_alias(str(item), state) if isinstance(item, str) else item)
    return out


def _config_servers_from_data(data: dict[str, Any], state: dict[str, Any]) -> list[dict[str, Any]]:
    servers = []
    for item in data.get("configServers") or []:
        servers.append(_config_for_alias(str(item), state) if isinstance(item, str) else item)
    if data.get("brokenConfigServerName"):
        servers.append({"name": data["brokenConfigServerName"], "transport": "http", "commandOrUrl": "http://127.0.0.1:1/broken", "updatedAt": "now"})
    return servers


def _config_for_alias(alias: str, state: dict[str, Any]) -> dict[str, Any]:
    config = (state.get("mcp_server_aliases") or {}).get(alias)
    if not config:
        raise ScenarioAssertionError(f"Unknown MCP server alias: {alias}", category="harness")
    return dict(config)


def _server_name(data: dict[str, Any], state: dict[str, Any]) -> str:
    if data.get("server"):
        return str(data["server"])
    alias = str(data.get("alias") or data.get("serverAlias") or "")
    if alias:
        return _config_for_alias(alias, state)["name"]
    return str(data.get("name") or "fake")


def _require_runtime(state: dict[str, Any]) -> dict[str, Any]:
    runtime = state.get("mcp_runtime")
    if not runtime:
        raise ScenarioAssertionError("No MCP runtime is available", category="driver")
    return runtime


def _refresh_mcp_snapshots(state: dict[str, Any], runtime: dict[str, Any], label: str) -> None:
    status = _json_safe(runtime["listServerStatus"]())
    state.setdefault("mcp_status", []).append({"label": label, "servers": status, "diagnostics": _json_safe(runtime["getDiagnostics"]())})
    state["mcp_tools"] = _json_safe(runtime["listTools"]())
    state["mcp_resources"] = _json_safe(runtime.get("resources") or [])


def _latest_server_status(state: dict[str, Any], server: str) -> dict[str, Any] | None:
    for snapshot in reversed(state.get("mcp_status") or []):
        for item in snapshot.get("servers") or []:
            if item.get("name") == server:
                return item
    return None


def _remember_result_alias(state: dict[str, Any], data: dict[str, Any], record: dict[str, Any]) -> None:
    alias = data.get("resultAlias") or data.get("aliasResult")
    if alias:
        state.setdefault("mcp_result_aliases", {})[str(alias)] = record


def _selected_result(state: dict[str, Any], data: dict[str, Any]) -> dict[str, Any]:
    if data.get("resultAlias"):
        return _aliased_result(state, str(data["resultAlias"]))
    records = [item for item in state.get("mcp_results") or [] if not data.get("driver") or item.get("driver") == data["driver"]]
    if not records:
        raise ScenarioAssertionError(f"No MCP result found for {data.get('driver')!r}")
    return records[int(data.get("index", len(records) - 1))]


def _aliased_result(state: dict[str, Any], alias: str) -> dict[str, Any]:
    record = (state.get("mcp_result_aliases") or {}).get(alias)
    if not record:
        raise ScenarioAssertionError(f"No MCP result alias found: {alias}")
    return record


def _first_blob_path(record: dict[str, Any]) -> str | None:
    contents = ((record.get("result") or {}).get("contents") or []) if isinstance(record.get("result"), dict) else []
    for item in contents:
        if isinstance(item, dict) and item.get("blobSavedTo"):
            return item["blobSavedTo"]
    return None


def _abort_signal(mode: Any, after_checks: int) -> Any:
    if mode == "pre":
        return {"aborted": True, "reason": "pre-cancelled"}
    if mode == "post":
        class FlipSignal:
            reason = "post-cancelled"

            def __init__(self) -> None:
                self.count = 0

            @property
            def aborted(self) -> bool:
                self.count += 1
                return self.count >= after_checks

        return FlipSignal()
    return None


async def _close_runtime_only(state: dict[str, Any]) -> None:
    boot = state.pop("mcp_bootstrap_runtime", None)
    runtime = state.pop("mcp_runtime", None)
    if boot and boot.get("close"):
        result = boot["close"]()
        if hasattr(result, "__await__"):
            await result
    elif runtime and runtime.get("close"):
        result = runtime["close"]()
        if hasattr(result, "__await__"):
            await result


def _tool_name(tool: Any) -> str | None:
    return getattr(tool, "name", None) or (tool.get("name") if isinstance(tool, dict) else None)


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
