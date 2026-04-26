from __future__ import annotations

import asyncio
import base64
import json
import os
import queue
import re
import socket
import subprocess
import threading
import uuid
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urljoin
from urllib.request import Request, urlopen

from ._utils import now_iso, pretty_json
from .storage import getStateRoot
from .tools import ToolDefinition


PROTOCOL_VERSION = "2024-11-05"


async def createMcpRuntime(args: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
    args = {**(args or {}), **kwargs}
    cwd = args.get("cwd") or os.getcwd()
    on_event = args.get("onRuntimeEvent") or (lambda *_: None)
    states = [_create_state(config) for config in _dedupe_servers(args.get("servers") or args.get("mcpConfig") or [])]
    await asyncio.gather(*(_connect_state(state, cwd, on_event) for state in states))
    await _register_requested_channels(states, args.get("registeredChannels") or [], args.get("sessionId"), on_event)

    def list_server_status() -> list[dict[str, Any]]:
        return [_server_status(state) for state in states]

    def diagnostics() -> dict[str, Any]:
        statuses = list_server_status()
        return {
            "servers": statuses,
            "connectedServers": sum(1 for state in states if state["connected"]),
            "totalTools": sum(len(state["tools"]) for state in states),
            "totalResources": sum(len(state["resources"]) for state in states),
        }

    def list_tools() -> list[dict[str, Any]]:
        return [tool for state in states for tool in state["tools"]]

    async def list_resources(server: str | None = None) -> list[dict[str, Any]]:
        selected = _select_states(states, server)
        out: list[dict[str, Any]] = []
        for state in selected:
            await _ensure_connected(state, cwd, on_event)
            state["resources"] = await _refresh_resources(state)
            out.extend(state["resources"])
        return out

    async def read_resource(server: str, uri: str) -> dict[str, Any]:
        state = await _require_connected(states, server, cwd, on_event)
        result = await _request_with_state_tracking(state, "resources/read", {"uri": uri})
        return await _normalize_read_resource_output(server, result)

    async def call_tool(server: str, name: str, tool_args: dict[str, Any] | None = None) -> dict[str, Any]:
        state = await _require_connected(states, server, cwd, on_event)
        result = await _request_with_state_tracking(state, "tools/call", {"name": name, "arguments": tool_args or {}})
        return _normalize_tool_call_result(server, name, result)

    def create_tool_definitions() -> list[ToolDefinition]:
        definitions: list[ToolDefinition] = []
        for state in states:
            for tool in state["tools"]:
                async def handler(input: dict[str, Any], _context: dict[str, Any], _tool=tool) -> dict[str, Any]:
                    try:
                        return await call_tool(_tool["server"], _tool["name"], input)
                    except Exception as exc:
                        return {"content": str(exc), "isError": True}

                definitions.append(
                    ToolDefinition(
                        tool["runtimeName"],
                        f"MCP {tool['server']}: {tool.get('description') or tool['name']}",
                        tool.get("inputSchema") or {"type": "object"},
                        handler,
                    )
                )
        return definitions

    async def close() -> None:
        await asyncio.gather(*(state["client"].close() for state in states if state.get("client")), return_exceptions=True)

    return {
        "tools": list_tools(),
        "resources": [resource for state in states for resource in state["resources"]],
        "servers": list_server_status(),
        "diagnostics": diagnostics(),
        "listServerStatus": list_server_status,
        "getDiagnostics": diagnostics,
        "listTools": list_tools,
        "listResources": list_resources,
        "readResource": read_resource,
        "callTool": call_tool,
        "createToolDefinitions": create_tool_definitions,
        "close": close,
    }


class JsonRpcClient:
    def __init__(self, config: dict[str, Any], cwd: str, on_notification: Callable[[dict[str, Any]], Any] | None = None):
        self.config = config
        self.cwd = cwd
        self.on_notification = on_notification
        self.next_id = 1

    async def initialize(self) -> None:
        await self.request(
            "initialize",
            {"protocolVersion": PROTOCOL_VERSION, "capabilities": {}, "clientInfo": {"name": "tess", "version": "0.1.0"}},
        )
        await self.notify("notifications/initialized")

    async def request(self, method: str, params: Any = None) -> Any:
        request_id = self.next_id
        self.next_id += 1
        response = await self._send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        return _unwrap_json_rpc_response(response)

    async def notify(self, method: str, params: Any = None) -> None:
        await self._send({"jsonrpc": "2.0", "method": method, "params": params})

    async def close(self) -> None:
        return None

    async def _send(self, request: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError


class HttpMcpClient(JsonRpcClient):
    async def _send(self, request: dict[str, Any]) -> dict[str, Any]:
        return await asyncio.to_thread(self._send_sync, request)

    def _send_sync(self, request: dict[str, Any]) -> dict[str, Any]:
        body = json.dumps(request).encode("utf-8")
        headers = {"content-type": "application/json", "accept": "application/json, text/event-stream", **(self.config.get("headers") or {})}
        req = Request(self.config["commandOrUrl"], data=body, headers=headers, method="POST")
        with urlopen(req, timeout=30) as response:
            if response.status in (202, 204):
                return _parse_http_rpc_response(response.status, "", response.headers.get("content-type", ""), request.get("id"))
            raw = response.read().decode("utf-8", errors="replace")
            return _parse_http_rpc_response(response.status, raw, response.headers.get("content-type", ""), request.get("id"))


class SseMcpClient(JsonRpcClient):
    def __init__(self, config: dict[str, Any], cwd: str, on_notification: Callable[[dict[str, Any]], Any] | None = None):
        super().__init__(config, cwd, on_notification)
        self.post_url = config["commandOrUrl"]
        self.pending: dict[int, queue.Queue[dict[str, Any] | BaseException]] = {}
        self.stop_event = threading.Event()
        self.endpoint_ready = threading.Event()
        self.stream_thread: threading.Thread | None = None
        self.stream_response: Any = None
        self.post_lock = threading.Lock()

    async def initialize(self) -> None:
        await asyncio.to_thread(self._start_event_stream)
        await super().initialize()

    async def close(self) -> None:
        self.stop_event.set()
        if self.stream_response:
            _close_http_response_socket(self.stream_response)
        if self.stream_thread and self.stream_thread.is_alive():
            await asyncio.to_thread(self.stream_thread.join, 1)
        for response_queue in self.pending.values():
            response_queue.put(RuntimeError(f"MCP SSE server closed: {self.config.get('name')}"))
        self.pending.clear()

    async def _send(self, request: dict[str, Any]) -> dict[str, Any]:
        request_id = request.get("id")
        response_queue: queue.Queue[dict[str, Any] | BaseException] | None = None
        if isinstance(request_id, int):
            response_queue = queue.Queue(maxsize=1)
            self.pending[request_id] = response_queue
        try:
            response, empty_ack = await asyncio.to_thread(self._post_sync, request)
            if not empty_ack or not response_queue:
                if isinstance(request_id, int):
                    self.pending.pop(request_id, None)
                return response
            try:
                queued = await asyncio.to_thread(response_queue.get, True, float(self.config.get("requestTimeoutSeconds") or 30))
            except queue.Empty as exc:
                raise TimeoutError(f"Timed out waiting for MCP SSE response {request_id}") from exc
            if isinstance(queued, BaseException):
                raise queued
            return queued
        except Exception:
            if isinstance(request_id, int):
                self.pending.pop(request_id, None)
            raise

    def _post_sync(self, request: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        body = json.dumps(request).encode("utf-8")
        headers = {"content-type": "application/json", "accept": "application/json, text/event-stream", **(self.config.get("headers") or {})}
        with self.post_lock:
            post_url = self.post_url
        req = Request(post_url, data=body, headers=headers, method="POST")
        with urlopen(req, timeout=30) as response:
            if response.status in (202, 204):
                return _parse_http_rpc_response(response.status, "", response.headers.get("content-type", ""), request.get("id")), True
            raw = response.read().decode("utf-8", errors="replace")
            empty_ack = response.status in (202, 204) or not raw.strip()
            return _parse_http_rpc_response(response.status, raw, response.headers.get("content-type", ""), request.get("id")), empty_ack

    def _start_event_stream(self) -> None:
        if self.stream_thread and self.stream_thread.is_alive():
            return
        self.stream_thread = threading.Thread(target=self._event_stream_loop, daemon=True)
        self.stream_thread.start()
        self.endpoint_ready.wait(timeout=float(self.config.get("startupTimeoutSeconds") or 2))

    def _event_stream_loop(self) -> None:
        event_name: str | None = None
        data_parts: list[str] = []
        try:
            headers = {"accept": "text/event-stream", **(self.config.get("headers") or {})}
            req = Request(self.config["commandOrUrl"], headers=headers, method="GET")
            with urlopen(req, timeout=30) as response:
                self.stream_response = response
                while not self.stop_event.is_set():
                    raw_line = response.readline()
                    if not raw_line:
                        break
                    line = raw_line.decode("utf-8", errors="replace").rstrip("\r\n")
                    if not line:
                        self._handle_sse_event(event_name, data_parts)
                        event_name = None
                        data_parts = []
                        continue
                    if line.startswith(":"):
                        continue
                    if line.startswith("event:"):
                        event_name = line[6:].strip()
                    elif line.startswith("data:"):
                        data_parts.append(line[5:].lstrip())
                if data_parts:
                    self._handle_sse_event(event_name, data_parts)
        except Exception as exc:
            if not self.stop_event.is_set():
                for response_queue in list(self.pending.values()):
                    response_queue.put(exc)
        finally:
            self.endpoint_ready.set()
            self.stream_response = None

    def _handle_sse_event(self, event_name: str | None, data_parts: list[str]) -> None:
        if not data_parts:
            return
        data = "\n".join(data_parts)
        try:
            payload = json.loads(data)
        except Exception:
            return
        if event_name == "endpoint":
            endpoint = payload if isinstance(payload, dict) else {}
            next_url = endpoint.get("url") if isinstance(endpoint.get("url"), str) else endpoint.get("endpoint") if isinstance(endpoint.get("endpoint"), str) else None
            if next_url:
                with self.post_lock:
                    self.post_url = urljoin(self.config["commandOrUrl"], next_url)
                self.endpoint_ready.set()
            return
        message = payload if isinstance(payload, dict) else {}
        request_id = message.get("id")
        method = message.get("method")
        if method and request_id is None:
            if self.on_notification:
                self.on_notification({"method": method, "params": message.get("params")})
            return
        if isinstance(request_id, int):
            response_queue = self.pending.pop(request_id, None)
            if response_queue:
                response_queue.put(message)


def _close_http_response_socket(response: Any) -> None:
    with contextlib_suppress():
        sock = response.fp.raw._sock
        sock.shutdown(socket.SHUT_RDWR)
        sock.close()
    with contextlib_suppress():
        response.close()


class StdioMcpClient(JsonRpcClient):
    def __init__(self, config: dict[str, Any], cwd: str, on_notification: Callable[[dict[str, Any]], Any] | None = None):
        super().__init__(config, cwd, on_notification)
        self.proc: subprocess.Popen[bytes] | None = None
        self.pending: dict[int, queue.Queue[dict[str, Any] | BaseException]] = {}
        self.buffer = b""
        self.reader_thread: threading.Thread | None = None
        self.write_lock = threading.Lock()

    async def initialize(self) -> None:
        env = {**os.environ, **(self.config.get("env") or {})}
        self.proc = subprocess.Popen(
            [self.config["commandOrUrl"], *(self.config.get("args") or [])],
            cwd=self.cwd,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        self.reader_thread = threading.Thread(target=self._read_loop, daemon=True)
        self.reader_thread.start()
        await super().initialize()

    async def close(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                await asyncio.to_thread(self.proc.wait, 2)
            except Exception:
                with contextlib_suppress():
                    self.proc.kill()
        if self.proc:
            for stream in (self.proc.stdin, self.proc.stdout, self.proc.stderr):
                if stream:
                    with contextlib_suppress():
                        stream.close()
        if self.reader_thread and self.reader_thread.is_alive():
            await asyncio.to_thread(self.reader_thread.join, 1)

    async def _send(self, request: dict[str, Any]) -> dict[str, Any]:
        if not self.proc or not self.proc.stdin:
            raise RuntimeError(f"MCP stdio server is not running: {self.config.get('name')}")
        request_id = request.get("id")
        if request_id is None:
            await asyncio.to_thread(self._write_message, request)
            return {"jsonrpc": "2.0", "id": None, "result": {}}
        response_queue: queue.Queue[dict[str, Any] | BaseException] = queue.Queue(maxsize=1)
        self.pending[int(request_id)] = response_queue
        await asyncio.to_thread(self._write_message, request)
        try:
            response = await asyncio.to_thread(response_queue.get, True, 30)
        except queue.Empty as exc:
            self.pending.pop(int(request_id), None)
            raise TimeoutError(f"Timed out waiting for MCP response {request_id}") from exc
        if isinstance(response, BaseException):
            raise response
        return response

    def _write_message(self, request: dict[str, Any]) -> None:
        assert self.proc and self.proc.stdin
        payload = json.dumps(request, separators=(",", ":")).encode("utf-8")
        with self.write_lock:
            self.proc.stdin.write(f"Content-Length: {len(payload)}\r\n\r\n".encode("utf-8") + payload)
            self.proc.stdin.flush()

    def _read_loop(self) -> None:
        assert self.proc and self.proc.stdout
        while True:
            chunk = self.proc.stdout.read1(4096) if hasattr(self.proc.stdout, "read1") else self.proc.stdout.read(4096)
            if not chunk:
                message = RuntimeError(f"MCP stdio server exited: {self.config.get('name')}")
                for response_queue in self.pending.values():
                    response_queue.put(message)
                self.pending.clear()
                return
            self.buffer += chunk
            self._drain_buffer()

    def _drain_buffer(self) -> None:
        while self.buffer:
            header_index = self.buffer.find(b"\r\n\r\n")
            if header_index >= 0:
                header = self.buffer[:header_index].decode("utf-8", errors="replace")
                match = re.search(r"Content-Length:\s*(\d+)", header, re.I)
                if not match:
                    self.buffer = self.buffer[header_index + 4 :]
                    continue
                length = int(match.group(1))
                start = header_index + 4
                if len(self.buffer) < start + length:
                    return
                payload = self.buffer[start : start + length].decode("utf-8", errors="replace")
                self.buffer = self.buffer[start + length :]
                self._handle_message(payload)
                continue
            newline = self.buffer.find(b"\n")
            if newline < 0:
                return
            line = self.buffer[:newline].decode("utf-8", errors="replace").strip()
            self.buffer = self.buffer[newline + 1 :]
            if line:
                self._handle_message(line)

    def _handle_message(self, payload: str) -> None:
        try:
            message = json.loads(payload)
        except Exception:
            return
        request_id = message.get("id")
        method = message.get("method")
        if method and request_id is None:
            if self.on_notification:
                self.on_notification({"method": method, "params": message.get("params")})
            return
        if isinstance(request_id, int):
            response_queue = self.pending.pop(request_id, None)
            if response_queue:
                response_queue.put(message)

class contextlib_suppress:
    def __enter__(self):
        return None

    def __exit__(self, *_exc):
        return True


def _dedupe_servers(servers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for server in servers:
        if server.get("name"):
            out[server["name"]] = server
    return list(out.values())


def _create_state(config: dict[str, Any]) -> dict[str, Any]:
    return {
        "config": config,
        "connected": False,
        "state": "disconnected",
        "error": None,
        "tools": [],
        "resources": [],
        "client": None,
        "lastConnectedAt": None,
        "lastErrorAt": None,
        "channelRegistration": {"requested": False, "status": "not_requested"},
    }


async def _connect_state(state: dict[str, Any], cwd: str, on_event: Callable[..., Any]) -> None:
    state.update({"state": "connecting", "error": None})
    if state.get("client"):
        try:
            await state["client"].close()
        except Exception:
            pass
    try:
        client = _create_client(state["config"], cwd, on_event)
        await client.initialize()
        state.update({"client": client, "connected": True, "state": "connected", "lastConnectedAt": now_iso()})
        state["tools"] = await _refresh_tools(state)
        state["resources"] = await _refresh_resources(state)
        on_event("mcp.server_connected", {"server": state["config"]["name"], "transport": state["config"].get("transport"), "tools": len(state["tools"]), "resources": len(state["resources"])})
    except Exception as exc:
        _mark_state_failure(state, exc)
        on_event("mcp.server_connection_failed", {"server": state["config"].get("name"), "transport": state["config"].get("transport"), "reason": state.get("error")})


def _create_client(config: dict[str, Any], cwd: str, on_event: Callable[..., Any]) -> JsonRpcClient:
    def notification(payload: dict[str, Any]) -> None:
        on_event("mcp.channel_notification", {"server": config.get("name"), "method": payload.get("method"), "params": payload.get("params") or {}})

    if config.get("transport") == "stdio":
        return StdioMcpClient(config, cwd, notification)
    if config.get("transport") == "sse":
        return SseMcpClient(config, cwd, notification)
    return HttpMcpClient(config, cwd, notification)


async def _ensure_connected(state: dict[str, Any], cwd: str, on_event: Callable[..., Any]) -> dict[str, Any]:
    if state.get("connected") and state.get("client"):
        return state
    await _connect_state(state, cwd, on_event)
    if state.get("connected") and state.get("client"):
        return state
    detail = f" ({state.get('error')})" if state.get("error") else ""
    raise RuntimeError(f"MCP server is not connected: {state['config'].get('name')}{detail}")


async def _require_connected(states: list[dict[str, Any]], server: str, cwd: str, on_event: Callable[..., Any]) -> dict[str, Any]:
    state = next((entry for entry in states if entry["config"].get("name") == server), None)
    if not state:
        raise RuntimeError(f"MCP server not found: {server}")
    return await _ensure_connected(state, cwd, on_event)


async def _request_with_state_tracking(state: dict[str, Any], method: str, params: Any = None) -> Any:
    try:
        return await state["client"].request(method, params)
    except Exception as exc:
        _mark_state_failure(state, exc)
        raise


async def _refresh_tools(state: dict[str, Any]) -> list[dict[str, Any]]:
    result = await _safe_request(state, "tools/list")
    if result is None:
        return []
    tools = result.get("tools") if isinstance(result, dict) else []
    out = []
    for tool in tools or []:
        if not isinstance(tool, dict) or not isinstance(tool.get("name"), str):
            continue
        out.append(
            {
                "server": state["config"]["name"],
                "name": tool["name"],
                "description": tool.get("description"),
                "inputSchema": tool.get("inputSchema") or {"type": "object"},
                "runtimeName": _runtime_tool_name(state["config"]["name"], tool["name"]),
            }
        )
    return out


async def _refresh_resources(state: dict[str, Any]) -> list[dict[str, Any]]:
    result = await _safe_request(state, "resources/list")
    if result is None:
        return state.get("resources") or []
    resources = result.get("resources") if isinstance(result, dict) else []
    return [
        {
            "server": state["config"]["name"],
            "uri": item.get("uri"),
            "name": item.get("name") or item.get("uri"),
            "mimeType": item.get("mimeType"),
            "description": item.get("description"),
        }
        for item in resources or []
        if isinstance(item, dict) and isinstance(item.get("uri"), str)
    ]


async def _safe_request(state: dict[str, Any], method: str, params: Any = None) -> Any:
    try:
        return await state["client"].request(method, params)
    except Exception as exc:
        if re.search(r"-32601|Method not found|not implemented", str(exc), re.I):
            return None
        raise


def _server_status(state: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": state["config"].get("name"),
        "transport": state["config"].get("transport"),
        "connected": state["connected"],
        "state": state["state"],
        "error": state.get("error"),
        "tools": len(state["tools"]),
        "resources": len(state["resources"]),
        "capabilities": {"tools": len(state["tools"]), "resources": len(state["resources"]), "channelsSupported": bool(state["channelRegistration"].get("requested")) or state["channelRegistration"].get("status") == "registered"},
        "lastConnectedAt": state.get("lastConnectedAt"),
        "lastErrorAt": state.get("lastErrorAt"),
        "channelRegistration": state["channelRegistration"],
    }


def _mark_state_failure(state: dict[str, Any], error: Exception) -> None:
    state["connected"] = False
    state["state"] = "degraded" if state["tools"] or state["resources"] else "disconnected"
    state["error"] = str(error)
    state["lastErrorAt"] = now_iso()


async def _register_requested_channels(states: list[dict[str, Any]], channels: list[str], session_id: str | None, on_event: Callable[..., Any]) -> None:
    if not session_id:
        return
    for server in channels:
        state = next((entry for entry in states if entry["config"].get("name") == server), None)
        if not state:
            on_event("mcp.channel_registration_failed", {"server": server, "session_id": session_id, "reason": "MCP server is not connected."})
            continue
        state["channelRegistration"]["requested"] = True
        if not state.get("connected"):
            state["channelRegistration"].update({"status": "failed", "updatedAt": now_iso(), "reason": state.get("error") or "MCP server is not connected."})
            on_event("mcp.channel_registration_failed", {"server": server, "session_id": session_id, "reason": state["channelRegistration"].get("reason")})
            continue
        try:
            result = await _safe_request(state, "channels/register", {"sessionId": session_id, "session_id": session_id, "client": {"name": "tess", "version": "0.1.0"}})
            state["channelRegistration"].update({"status": "registered", "updatedAt": now_iso()})
            on_event("mcp.channel_registered", {"server": server, "session_id": session_id, "result": result if isinstance(result, dict) else {}})
        except Exception as exc:
            state["channelRegistration"].update({"status": "failed", "updatedAt": now_iso(), "reason": str(exc)})
            on_event("mcp.channel_registration_failed", {"server": server, "session_id": session_id, "reason": str(exc)})


def _select_states(states: list[dict[str, Any]], server: str | None) -> list[dict[str, Any]]:
    return [state for state in states if not server or state["config"].get("name") == server]


def _normalize_tool_call_result(server: str, tool_name: str, value: Any) -> dict[str, Any]:
    result = value if isinstance(value, dict) else {}
    blocks = result.get("content") if isinstance(result.get("content"), list) else None
    text_parts = []
    for block in blocks or []:
        if isinstance(block, dict) and isinstance(block.get("text"), str):
            text_parts.append(block["text"])
        elif isinstance(block, dict) and isinstance(block.get("content"), str):
            text_parts.append(block["content"])
        else:
            text_parts.append(pretty_json(block))
    if text_parts:
        text = "\n".join(text_parts)
    elif "structuredContent" in result:
        text = pretty_json(result["structuredContent"])
    elif isinstance(value, str):
        text = value
    else:
        text = pretty_json(value)
    return {"content": text, "isError": bool(result.get("isError")), "meta": {"mcp_server": server, "mcp_tool": tool_name}}


async def _normalize_read_resource_output(server: str, value: Any) -> dict[str, Any]:
    result = value if isinstance(value, dict) else {}
    contents = result.get("contents") if isinstance(result.get("contents"), list) else []
    out = []
    for index, entry in enumerate(contents):
        content = entry if isinstance(entry, dict) else {}
        uri = content.get("uri") if isinstance(content.get("uri"), str) else f"{server}#{index}"
        if isinstance(content.get("text"), str):
            out.append({"uri": uri, "mimeType": content.get("mimeType"), "text": content["text"]})
            continue
        blob = content.get("blob") if isinstance(content.get("blob"), str) else content.get("data") if isinstance(content.get("data"), str) else None
        if blob:
            out.append({"uri": uri, "mimeType": content.get("mimeType"), "blobSavedTo": await _save_blob_content(server, uri, blob)})
        else:
            out.append({"uri": uri, "mimeType": content.get("mimeType"), "text": pretty_json(content)})
    return {"contents": out}


async def _save_blob_content(server: str, uri: str, data: str) -> str:
    target_dir = Path(getStateRoot()) / "mcp-blobs" / _sanitize_name(server)
    target_dir.mkdir(parents=True, exist_ok=True)
    ext = Path(uri).suffix or ".bin"
    target = target_dir / f"{_sanitize_name(Path(uri).name or str(uuid.uuid4()))}{ext}"
    target.write_bytes(base64.b64decode(data))
    return str(target)


def _runtime_tool_name(server: str, tool: str) -> str:
    return f"mcp__{_sanitize_name(server)}__{_sanitize_name(tool)}"


def _sanitize_name(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "_", value.strip().lower()).strip("_")
    return normalized or "item"


def _unwrap_json_rpc_response(response: dict[str, Any]) -> Any:
    if response.get("error"):
        error = response["error"]
        if isinstance(error, dict):
            raise RuntimeError(f"{error.get('code')}: {error.get('message')}")
        raise RuntimeError(str(error))
    return response.get("result") or {}


def _parse_http_rpc_response(status: int, raw: str, content_type: str, request_id: int | None) -> dict[str, Any]:
    if status in (202, 204) or not raw.strip():
        return {"jsonrpc": "2.0", "id": request_id, "result": {}}
    if "text/event-stream" in content_type:
        return _parse_sse_rpc_response(raw, request_id)
    return json.loads(raw)


def _parse_sse_rpc_response(raw: str, request_id: int | None) -> dict[str, Any]:
    for event in _parse_sse_events(raw):
        data = event.get("data")
        if not data:
            continue
        try:
            payload = json.loads(data)
        except Exception:
            continue
        if request_id is None or payload.get("id") == request_id:
            return payload
    return {"jsonrpc": "2.0", "id": request_id, "result": {}}


def _parse_sse_events(raw: str) -> list[dict[str, str]]:
    events: list[dict[str, str]] = []
    normalized = raw.replace("\r\n", "\n").replace("\r", "\n")
    for raw_event in normalized.split("\n\n"):
        if not raw_event.strip():
            continue
        event_name: str | None = None
        data_parts: list[str] = []
        for line in raw_event.split("\n"):
            if line.startswith("event:"):
                event_name = line[6:].strip()
            elif line.startswith("data:"):
                data_parts.append(line[5:].lstrip())
        event: dict[str, str] = {}
        if event_name:
            event["event"] = event_name
        if data_parts:
            event["data"] = "\n".join(data_parts)
        events.append(event)
    return events
