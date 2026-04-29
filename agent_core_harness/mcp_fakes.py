from __future__ import annotations

import base64
import http.server
import json
import queue
import sys
import textwrap
import threading
from typing import Any

from .sandbox import ScenarioSandbox


def start_fake_mcp_server(data: dict[str, Any], sandbox: ScenarioSandbox, alias: str) -> tuple[dict[str, Any], dict[str, Any]]:
    transport = str(data.get("transport") or "http")
    if transport == "stdio":
        return _start_stdio_server(data, sandbox, alias)
    if transport == "sse":
        return _start_sse_server(data)
    return _start_http_server(data)


def _start_http_server(data: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    calls = {"tool": 0}
    payload = _server_payload(data)

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *_args: Any) -> None:
            return

        def do_POST(self) -> None:
            request = _read_json_request(self)
            method = request.get("method")
            request_id = request.get("id")
            if method == "notifications/initialized":
                _send_empty(self)
                return
            if payload["profile"] == "protocol" and method == "tools/call":
                variant = (request.get("params") or {}).get("arguments", {}).get("variant")
                if variant in {"batch", "missing", "wrong", "invalid"}:
                    _send_protocol_variant(self, variant, request_id)
                    return
            if method == "tools/call" and payload["fail_first_tool_call"] and calls["tool"] == 0:
                calls["tool"] += 1
                self.send_response(500)
                self.end_headers()
                self.wfile.write(b"temporary failure")
                return
            result = _rpc_result(payload, request, calls)
            _send_json(self, {"jsonrpc": "2.0", "id": request_id, "result": result})

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_address[1]}/rpc"
    return _server_config(data, "http", url), {"server": server, "thread": thread}


def _start_sse_server(data: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    payload = _server_payload(data)
    calls = {"tool": 0}
    stop_event = threading.Event()
    queues: list[queue.Queue[dict[str, Any]]] = []
    queues_lock = threading.Lock()

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *_args: Any) -> None:
            return

        def do_GET(self) -> None:
            if self.path != "/sse":
                self.send_error(404)
                return
            event_queue: queue.Queue[dict[str, Any]] = queue.Queue()
            with queues_lock:
                queues.append(event_queue)
            self.send_response(200)
            self.send_header("content-type", "text/event-stream")
            self.end_headers()
            endpoint = f"http://127.0.0.1:{self.server.server_address[1]}/messages"
            self.wfile.write(f"event: endpoint\ndata: {json.dumps({'url': endpoint})}\n\n".encode())
            self.wfile.flush()
            try:
                while not stop_event.is_set():
                    try:
                        message = event_queue.get(timeout=0.1)
                    except queue.Empty:
                        continue
                    self.wfile.write(f"data: {json.dumps(message)}\n\n".encode())
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                return

        def do_POST(self) -> None:
            if self.path != "/messages":
                self.send_error(404)
                return
            request = _read_json_request(self)
            request_id = request.get("id")
            if request_id is not None:
                result = _rpc_result(payload, request, calls)
                with queues_lock:
                    active_queues = list(queues)
                for item in active_queues:
                    item.put({"jsonrpc": "2.0", "id": request_id, "result": result})
                    if request.get("method") == "initialize":
                        for notification in _notifications():
                            item.put(notification)
            _send_empty(self)

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_address[1]}/sse"
    return _server_config(data, "sse", url), {"server": server, "thread": thread, "stop_event": stop_event}


def _start_stdio_server(data: dict[str, Any], sandbox: ScenarioSandbox, alias: str) -> tuple[dict[str, Any], dict[str, Any]]:
    payload = _server_payload(data)
    script_dir = sandbox.workspace / ".harness" / "mcp"
    script_dir.mkdir(parents=True, exist_ok=True)
    script = script_dir / f"{alias}_stdio_server.py"
    script.write_text(_stdio_server_script(payload), encoding="utf-8")
    return _server_config(data, "stdio", sys.executable, [str(script)]), {"script": str(script)}


def _server_payload(data: dict[str, Any]) -> dict[str, Any]:
    name = str(data.get("name") or data.get("alias") or "fake")
    tool_name = str(data.get("toolName") or ("edge" if data.get("profile") == "protocol" else "echo"))
    default_uri = "memory://blob/large.bin" if data.get("resourceMode") == "blob" else f"memory://{name}"
    resource_uri = str(data.get("resourceUri") or default_uri)
    blob_text = str(data.get("blobText") or ("blob-data-" * 512))
    return {
        "name": name,
        "server_info_name": str(data.get("serverInfoName") or f"{name}-fake"),
        "profile": str(data.get("profile") or "normal"),
        "tool_name": tool_name,
        "tool_description": str(data.get("toolDescription") or f"{tool_name} input"),
        "text_prefix": str(data.get("textPrefix") or f"{name}:"),
        "resource_uri": resource_uri,
        "resource_name": str(data.get("resourceName") or resource_uri.rsplit("/", 1)[-1]),
        "resource_text": str(data.get("resourceText") or f"{name} resource"),
        "resource_mode": str(data.get("resourceMode") or "text"),
        "blob": base64.b64encode(blob_text.encode("utf-8")).decode("ascii"),
        "fail_first_tool_call": bool(data.get("failFirstToolCall")),
        "capabilities": data.get("capabilities") or {"tools": {"listChanged": True}, "resources": {"listChanged": True}, "logging": {}},
    }


def _server_config(data: dict[str, Any], transport: str, command_or_url: str, args: list[str] | None = None) -> dict[str, Any]:
    config = {
        "name": str(data.get("name") or data.get("alias") or transport),
        "transport": transport,
        "commandOrUrl": command_or_url,
        "updatedAt": str(data.get("updatedAt") or "now"),
        "requestTimeoutSeconds": float(data.get("requestTimeoutSeconds") or 3),
    }
    if args:
        config["args"] = args
    return config


def _rpc_result(payload: dict[str, Any], request: dict[str, Any], calls: dict[str, int]) -> Any:
    method = request.get("method")
    if method == "initialize":
        return {"serverInfo": {"name": payload["server_info_name"]}, "capabilities": payload["capabilities"]}
    if method == "tools/list":
        return {
            "tools": [
                {
                    "name": payload["tool_name"],
                    "description": payload["tool_description"],
                    "inputSchema": {"type": "object", "properties": {"value": {"type": "string"}, "variant": {"type": "string"}}},
                }
            ]
        }
    if method == "resources/list":
        return {"resources": [{"uri": payload["resource_uri"], "name": payload["resource_name"], "mimeType": "text/plain"}]}
    if method == "resources/read":
        if payload["resource_mode"] == "blob":
            return {"contents": [{"uri": payload["resource_uri"], "mimeType": "application/octet-stream", "blob": payload["blob"]}]}
        return {"contents": [{"uri": payload["resource_uri"], "mimeType": "text/plain", "text": payload["resource_text"]}]}
    if method == "tools/call":
        calls["tool"] += 1
        args = (request.get("params") or {}).get("arguments") or {}
        if payload["profile"] == "protocol" and args.get("variant") == "ok":
            return {"content": [{"type": "text", "text": "ok"}]}
        return {"content": [{"type": "text", "text": payload["text_prefix"] + str(args.get("value", ""))}]}
    if method == "channels/register":
        return {"registered": True, "sessionId": (request.get("params") or {}).get("sessionId")}
    return {}


def _send_protocol_variant(handler: http.server.BaseHTTPRequestHandler, variant: str, request_id: Any) -> None:
    if variant == "batch":
        _send_raw(handler, json.dumps([{"jsonrpc": "2.0", "id": request_id, "result": {}}]).encode())
    elif variant == "missing":
        _send_raw(handler, json.dumps({"jsonrpc": "2.0", "result": {}}).encode())
    elif variant == "wrong":
        _send_raw(handler, json.dumps({"jsonrpc": "2.0", "id": int(request_id or 0) + 1, "result": {}}).encode())
    else:
        _send_raw(handler, b"{not-json")


def _read_json_request(handler: http.server.BaseHTTPRequestHandler) -> dict[str, Any]:
    length = int(handler.headers.get("content-length") or 0)
    if length <= 0:
        return {}
    return json.loads(handler.rfile.read(length).decode("utf-8"))


def _send_empty(handler: http.server.BaseHTTPRequestHandler) -> None:
    handler.send_response(202)
    handler.end_headers()


def _send_json(handler: http.server.BaseHTTPRequestHandler, payload: dict[str, Any]) -> None:
    _send_raw(handler, json.dumps(payload).encode("utf-8"), "application/json")


def _send_raw(handler: http.server.BaseHTTPRequestHandler, raw: bytes, content_type: str = "application/json") -> None:
    handler.send_response(200)
    handler.send_header("content-type", content_type)
    handler.send_header("content-length", str(len(raw)))
    handler.end_headers()
    handler.wfile.write(raw)


def _notifications() -> list[dict[str, Any]]:
    return [
        {"jsonrpc": "2.0", "method": "notifications/message", "params": {"level": "info", "text": "ready"}},
        {"jsonrpc": "2.0", "method": "notifications/progress", "params": "halfway"},
        {"jsonrpc": "2.0", "method": "notifications/tools/list_changed", "params": {"reason": "new-tool"}},
        {"jsonrpc": "2.0", "method": "notifications/resources/list_changed", "params": {"reason": "new-resource"}},
        {"jsonrpc": "2.0", "method": "notifications/logging/message", "params": {"level": "warning", "data": "log-ready"}},
    ]


def _stdio_server_script(payload: dict[str, Any]) -> str:
    return textwrap.dedent(
        f"""
        import json
        import sys

        PAYLOAD = json.loads({json.dumps(json.dumps(payload))})
        CALLS = {{"tool": 0}}

        def read_message():
            header = b""
            while not header.endswith(b"\\r\\n\\r\\n"):
                chunk = sys.stdin.buffer.read(1)
                if not chunk:
                    return None
                header += chunk
            length = 0
            for line in header.decode().splitlines():
                if line.lower().startswith("content-length:"):
                    length = int(line.split(":", 1)[1].strip())
            return json.loads(sys.stdin.buffer.read(length).decode())

        def send(message):
            raw = json.dumps(message).encode()
            sys.stdout.buffer.write(f"Content-Length: {{len(raw)}}\\r\\n\\r\\n".encode() + raw)
            sys.stdout.buffer.flush()

        def result_for(request):
            method = request.get("method")
            if method == "initialize":
                return {{"serverInfo": {{"name": PAYLOAD["server_info_name"]}}, "capabilities": PAYLOAD["capabilities"]}}
            if method == "tools/list":
                return {{"tools": [{{"name": PAYLOAD["tool_name"], "description": PAYLOAD["tool_description"], "inputSchema": {{"type": "object", "properties": {{"value": {{"type": "string"}}}}}}}}]}}
            if method == "resources/list":
                return {{"resources": [{{"uri": PAYLOAD["resource_uri"], "name": PAYLOAD["resource_name"], "mimeType": "text/plain"}}]}}
            if method == "resources/read":
                return {{"contents": [{{"uri": PAYLOAD["resource_uri"], "mimeType": "text/plain", "text": PAYLOAD["resource_text"]}}]}}
            if method == "tools/call":
                CALLS["tool"] += 1
                args = (request.get("params") or {{}}).get("arguments") or {{}}
                return {{"content": [{{"type": "text", "text": PAYLOAD["text_prefix"] + str(args.get("value", ""))}}]}}
            if method == "channels/register":
                return {{"registered": True}}
            return {{}}

        while True:
            request = read_message()
            if request is None:
                break
            request_id = request.get("id")
            if request_id is None:
                continue
            send({{"jsonrpc": "2.0", "id": request_id, "result": result_for(request)}})
        """
    ).lstrip()
