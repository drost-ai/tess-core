from __future__ import annotations

from .shared import *


class JsonRpcClient:
    def __init__(
        self,
        config: dict[str, Any],
        cwd: str,
        on_notification: Callable[[dict[str, Any]], Any] | None = None,
        auth: Any = None,
    ):
        self.config = config
        self.cwd = cwd
        self.on_notification = on_notification
        self.next_id = 1
        self.server_info: dict[str, Any] = {}
        self.server_capabilities: dict[str, Any] = {}
        self.negotiated_protocol_version: str | None = None
        self.auth = auth

    async def initialize(self) -> None:
        result = await self.request(
            "initialize",
            {"protocolVersion": PREFERRED_PROTOCOL_VERSION, "capabilities": {}, "clientInfo": {"name": "tess", "version": "0.1.0"}},
        )
        if isinstance(result, dict):
            self.server_info = bounded_capabilities(result.get("serverInfo"))
            self.server_capabilities = bounded_capabilities(result.get("capabilities"))
        self.negotiated_protocol_version = resolve_negotiated_protocol(result)
        await self.notify("notifications/initialized")

    def _http_headers(self, base: dict[str, str]) -> dict[str, str]:
        headers = {**base, **(self.config.get("headers") or {})}
        if self.negotiated_protocol_version:
            headers["MCP-Protocol-Version"] = self.negotiated_protocol_version
        return headers

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
        resource_url = self.config["commandOrUrl"]
        auth_headers = await self.auth.authorize_headers(resource_url) if self.auth else {}
        try:
            return await asyncio.to_thread(self._send_sync, request, auth_headers)
        except _McpHttpUnauthorized as exc:
            if not self.auth:
                raise RuntimeError(f"HTTP 401 from MCP server {self.config.get('name')}") from None
            retry = await self.auth.handle_unauthorized(
                www_authenticate=exc.www_authenticate,
                resource_url=resource_url,
                sent_authorization=auth_headers.get("Authorization"),
            )
            if not retry:
                raise RuntimeError(f"HTTP 401 from MCP server {self.config.get('name')}") from None
            auth_headers = await self.auth.authorize_headers(resource_url)
            return await asyncio.to_thread(self._send_sync, request, auth_headers)

    def _send_sync(self, request: dict[str, Any], auth_headers: dict[str, str] | None = None) -> dict[str, Any]:
        body = json.dumps(request).encode("utf-8")
        headers = {**self._http_headers({"content-type": "application/json", "accept": "application/json, text/event-stream"}), **(auth_headers or {})}
        req = Request(self.config["commandOrUrl"], data=body, headers=headers, method="POST")
        try:
            with urlopen(req, timeout=float(self.config.get("requestTimeoutSeconds") or 30)) as response:
                if response.status in (202, 204):
                    return _parse_http_rpc_response(response.status, "", response.headers.get("content-type", ""), request.get("id"))
                raw = response.read().decode("utf-8", errors="replace")
                return _parse_http_rpc_response(response.status, raw, response.headers.get("content-type", ""), request.get("id"))
        except HTTPError as exc:
            if exc.code == 401:
                challenge = exc.headers.get("WWW-Authenticate")
                with contextlib_suppress():
                    exc.close()
                raise _McpHttpUnauthorized(challenge) from None
            raise RuntimeError(f"HTTP {exc.code} from MCP server {self.config.get('name')}") from exc


class SseMcpClient(JsonRpcClient):
    def __init__(
        self,
        config: dict[str, Any],
        cwd: str,
        on_notification: Callable[[dict[str, Any]], Any] | None = None,
        auth: Any = None,
    ):
        super().__init__(config, cwd, on_notification, auth)
        self.post_url = config["commandOrUrl"]
        self.pending: dict[int, queue.Queue[dict[str, Any] | BaseException]] = {}
        self.stop_event = threading.Event()
        self.endpoint_ready = threading.Event()
        self.stream_thread: threading.Thread | None = None
        self.stream_response: Any = None
        self.post_lock = threading.Lock()
        self.stream_auth_headers: dict[str, str] = {}
        self.stream_unauthorized: _McpHttpUnauthorized | None = None

    async def initialize(self) -> None:
        self.stream_auth_headers = await self.auth.authorize_headers(self.config["commandOrUrl"]) if self.auth else {}
        await asyncio.to_thread(self._start_event_stream)
        if self.stream_unauthorized and self.auth:
            retry = await self.auth.handle_unauthorized(
                www_authenticate=self.stream_unauthorized.www_authenticate,
                resource_url=self.config["commandOrUrl"],
                sent_authorization=self.stream_auth_headers.get("Authorization"),
            )
            if retry:
                if self.stream_thread and self.stream_thread.is_alive():
                    await asyncio.to_thread(self.stream_thread.join, 1)
                self.stream_auth_headers = await self.auth.authorize_headers(self.config["commandOrUrl"])
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
            auth_headers = await self.auth.authorize_headers(self.post_url) if self.auth else {}
            try:
                response, empty_ack = await asyncio.to_thread(self._post_sync, request, auth_headers)
            except _McpHttpUnauthorized as exc:
                if not self.auth:
                    raise RuntimeError(f"HTTP 401 from MCP SSE server {self.config.get('name')}") from None
                retry = await self.auth.handle_unauthorized(
                    www_authenticate=exc.www_authenticate,
                    resource_url=self.post_url,
                    sent_authorization=auth_headers.get("Authorization"),
                )
                if not retry:
                    raise RuntimeError(f"HTTP 401 from MCP SSE server {self.config.get('name')}") from None
                auth_headers = await self.auth.authorize_headers(self.post_url)
                response, empty_ack = await asyncio.to_thread(self._post_sync, request, auth_headers)
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
        except asyncio.CancelledError:
            if isinstance(request_id, int):
                self.pending.pop(request_id, None)
            raise
        except Exception:
            if isinstance(request_id, int):
                self.pending.pop(request_id, None)
            raise

    def _post_sync(self, request: dict[str, Any], auth_headers: dict[str, str] | None = None) -> tuple[dict[str, Any], bool]:
        body = json.dumps(request).encode("utf-8")
        headers = {**self._http_headers({"content-type": "application/json", "accept": "application/json, text/event-stream"}), **(auth_headers or {})}
        with self.post_lock:
            post_url = self.post_url
        req = Request(post_url, data=body, headers=headers, method="POST")
        try:
            with urlopen(req, timeout=float(self.config.get("requestTimeoutSeconds") or 30)) as response:
                if response.status in (202, 204):
                    return _parse_http_rpc_response(response.status, "", response.headers.get("content-type", ""), request.get("id")), True
                raw = response.read().decode("utf-8", errors="replace")
                empty_ack = response.status in (202, 204) or not raw.strip()
                return _parse_http_rpc_response(response.status, raw, response.headers.get("content-type", ""), request.get("id")), empty_ack
        except HTTPError as exc:
            if exc.code == 401:
                challenge = exc.headers.get("WWW-Authenticate")
                with contextlib_suppress():
                    exc.close()
                raise _McpHttpUnauthorized(challenge) from None
            raise RuntimeError(f"HTTP {exc.code} from MCP SSE server {self.config.get('name')}") from exc

    def _start_event_stream(self) -> None:
        if self.stream_thread and self.stream_thread.is_alive():
            return
        self.endpoint_ready.clear()
        self.stream_unauthorized = None
        self.stream_thread = threading.Thread(target=self._event_stream_loop, daemon=True)
        self.stream_thread.start()
        self.endpoint_ready.wait(timeout=float(self.config.get("startupTimeoutSeconds") or 2))

    def _event_stream_loop(self) -> None:
        event_name: str | None = None
        data_parts: list[str] = []
        try:
            headers = {**self._http_headers({"accept": "text/event-stream"}), **self.stream_auth_headers}
            req = Request(self.config["commandOrUrl"], headers=headers, method="GET")
            try:
                response_context = urlopen(req, timeout=float(self.config.get("streamTimeoutSeconds") or 30))
            except HTTPError as exc:
                if exc.code == 401:
                    challenge = exc.headers.get("WWW-Authenticate")
                    with contextlib_suppress():
                        exc.close()
                    self.stream_unauthorized = _McpHttpUnauthorized(challenge)
                    return
                raise
            with response_context as response:
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
        if isinstance(payload, list):
            self._reject_pending(RuntimeError("MCP server returned a batch JSON-RPC response."))
            return
        if event_name == "endpoint":
            endpoint = payload if isinstance(payload, dict) else {}
            next_url = endpoint.get("url") if isinstance(endpoint.get("url"), str) else endpoint.get("endpoint") if isinstance(endpoint.get("endpoint"), str) else None
            if next_url:
                with self.post_lock:
                    self.post_url = next_url
                self.endpoint_ready.set()
            return
        message = payload if isinstance(payload, dict) else {}
        request_id = message.get("id")
        method = message.get("method")
        if method and request_id is None:
            if self.on_notification:
                self.on_notification({"method": method, "params": message.get("params")})
            return
        if isinstance(request_id, int) and not isinstance(request_id, bool):
            response_queue = self.pending.pop(request_id, None)
            if response_queue:
                response_queue.put(message)
            return
        if "result" in message or "error" in message:
            self._reject_pending(RuntimeError("MCP server response id did not match the pending request."))

    def _reject_pending(self, error: BaseException) -> None:
        for response_queue in list(self.pending.values()):
            response_queue.put(error)
        self.pending.clear()


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
            response = await asyncio.to_thread(response_queue.get, True, float(self.config.get("requestTimeoutSeconds") or 30))
        except queue.Empty as exc:
            self.pending.pop(int(request_id), None)
            raise TimeoutError(f"Timed out waiting for MCP response {request_id}") from exc
        except asyncio.CancelledError:
            self.pending.pop(int(request_id), None)
            raise
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
        if isinstance(message, list):
            self._reject_pending(RuntimeError("MCP server returned a batch JSON-RPC response."))
            return
        if not isinstance(message, dict):
            self._reject_pending(RuntimeError("MCP server returned an invalid JSON-RPC response."))
            return
        request_id = message.get("id")
        method = message.get("method")
        if method and request_id is None:
            if self.on_notification:
                self.on_notification({"method": method, "params": message.get("params")})
            return
        if isinstance(request_id, int) and not isinstance(request_id, bool):
            response_queue = self.pending.pop(request_id, None)
            if response_queue:
                response_queue.put(message)
            return
        if "result" in message or "error" in message:
            self._reject_pending(RuntimeError("MCP server response id did not match the pending request."))

    def _reject_pending(self, error: BaseException) -> None:
        for response_queue in list(self.pending.values()):
            response_queue.put(error)
        self.pending.clear()


class contextlib_suppress:
    def __enter__(self):
        return None

    def __exit__(self, *_exc):
        return True


class _McpHttpUnauthorized(RuntimeError):
    def __init__(self, www_authenticate: str | None):
        super().__init__("MCP HTTP request requires authorization.")
        self.www_authenticate = www_authenticate
