from __future__ import annotations

from .shared import *
from .transport_policy import *


def validateMcpResponseHeaders(response: Any) -> None:
    headers = getattr(response, "headers", None)
    if _measure_headers(headers) > MCP_HEADER_MAX_BYTES:
        _close_http_response(response)
        raise McpTransportError(
            "header_limit",
            f"MCP response headers exceed {MCP_HEADER_MAX_BYTES} bytes.",
        )
    lengths = _header_values(headers, "content-length")
    if len(lengths) > 1:
        _close_http_response(response)
        raise McpTransportError(
            "invalid_framing",
            "MCP response contains duplicate Content-Length headers.",
        )
    if lengths:
        declared = _parse_declared_length(lengths[0])
        if declared > MCP_FRAME_MAX_BYTES:
            _close_http_response(response)
            raise McpTransportError(
                "frame_limit",
                f"MCP declared compressed body exceeds {MCP_FRAME_MAX_BYTES} bytes.",
            )


def readMcpBodyBytes(
    response: Any,
    policy: dict[str, int],
    *,
    continuous: bool = False,
) -> bytes:
    chunks: list[bytes] = []
    total = 0
    for chunk in iterateMcpBody(response, policy, continuous=continuous):
        chunks.append(chunk)
        total += len(chunk)
    return b"".join(chunks)


def readMcpBodyText(response: Any, policy: dict[str, int]) -> str:
    raw = readMcpBodyBytes(response, policy)
    try:
        return raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError as error:
        raise McpTransportError(
            "invalid_encoding",
            "MCP HTTP response body is not valid UTF-8.",
        ) from error


def iterateMcpBody(
    response: Any,
    policy: dict[str, int],
    *,
    continuous: bool = False,
):
    encoding = (_first_header(getattr(response, "headers", None), "content-encoding") or "identity").strip().lower()
    if not encoding:
        encoding = "identity"
    if "," in encoding:
        raise McpTransportError(
            "invalid_encoding",
            "Multiple MCP content encodings are not supported.",
        )
    decoder = _create_decoder(encoding)
    compressed_bytes = 0
    decoded_bytes = 0
    _set_response_timeout(response, policy["bodyIdleTimeoutMs"] / 1000)
    try:
        while True:
            try:
                one_shot = False
                incremental_reader = getattr(response, "read1", None)
                if continuous and callable(incremental_reader):
                    try:
                        chunk = incremental_reader(64 * 1024)
                    except TypeError:
                        chunk = incremental_reader()
                else:
                    try:
                        chunk = response.read(64 * 1024)
                    except TypeError:
                        chunk = response.read()
                        one_shot = True
            except (TimeoutError, socket.timeout) as error:
                raise McpTransportError(
                    "body_idle_timeout",
                    f"MCP HTTP body was idle for {policy['bodyIdleTimeoutMs']}ms.",
                ) from error
            if not chunk:
                break
            raw = bytes(chunk)
            compressed_bytes += len(raw)
            if len(raw) > MCP_FRAME_MAX_BYTES or (
                not continuous and compressed_bytes > MCP_FRAME_MAX_BYTES
            ):
                raise McpTransportError(
                    "frame_limit",
                    f"MCP compressed body exceeds {MCP_FRAME_MAX_BYTES} bytes.",
                )
            try:
                decoded = raw if decoder is None else decoder.decompress(raw)
            except Exception as error:
                raise McpTransportError(
                    "invalid_encoding",
                    f"MCP response body could not be decoded as {encoding}.",
                ) from error
            if decoded:
                decoded_bytes = _check_decoded_limit(
                    decoded,
                    decoded_bytes,
                    continuous,
                )
                yield decoded
            if one_shot:
                break
        if decoder is not None:
            try:
                final = decoder.flush()
            except Exception as error:
                raise McpTransportError(
                    "invalid_encoding",
                    f"MCP response body could not be decoded as {encoding}.",
                ) from error
            if final:
                _check_decoded_limit(final, decoded_bytes, continuous)
                yield final
    finally:
        _close_http_response(response)


def _check_decoded_limit(chunk: bytes, total: int, continuous: bool) -> int:
    next_total = total + len(chunk)
    if len(chunk) > MCP_DECODED_BODY_MAX_BYTES or (
        not continuous and next_total > MCP_DECODED_BODY_MAX_BYTES
    ):
        raise McpTransportError(
            "decoded_limit",
            f"MCP decoded body exceeds {MCP_DECODED_BODY_MAX_BYTES} bytes.",
        )
    return next_total


def _create_decoder(encoding: str):
    if encoding == "identity":
        return None
    if encoding in {"gzip", "x-gzip"}:
        return zlib.decompressobj(16 + zlib.MAX_WBITS)
    if encoding == "deflate":
        return zlib.decompressobj()
    if encoding == "br":
        try:
            import brotli  # type: ignore[import-not-found]
        except ImportError as error:
            raise McpTransportError(
                "invalid_encoding",
                "Brotli MCP responses require the optional brotli package.",
            ) from error

        class _BrotliDecoder:
            def __init__(self) -> None:
                self.decoder = brotli.Decompressor()

            def decompress(self, value: bytes) -> bytes:
                return self.decoder.process(value)

            def flush(self) -> bytes:
                return b""

        return _BrotliDecoder()
    raise McpTransportError(
        "invalid_encoding",
        f"Unsupported MCP content encoding: {encoding}.",
    )


def _content_type(response: Any) -> str:
    return (_first_header(getattr(response, "headers", None), "content-type") or "").split(";", 1)[0].strip().lower()


def _is_json_content_type(value: str) -> bool:
    return value == "application/json" or bool(
        re.fullmatch(r"application/[a-z0-9.+-]+\+json", value)
    )


def _first_header(headers: Any, name: str) -> str | None:
    values = _header_values(headers, name)
    return values[0] if values else None


def _header_values(headers: Any, name: str) -> list[str]:
    if headers is None:
        return []
    get_all = getattr(headers, "get_all", None)
    if callable(get_all):
        values = get_all(name) or []
        return [str(value) for value in values]
    getter = getattr(headers, "get", None)
    if callable(getter):
        value = getter(name)
        return [] if value is None else [str(value)]
    if isinstance(headers, dict):
        value = next(
            (item for key, item in headers.items() if str(key).lower() == name.lower()),
            None,
        )
        if isinstance(value, list):
            return [str(item) for item in value]
        return [] if value is None else [str(value)]
    return []


def _measure_headers(headers: Any) -> int:
    if headers is None:
        return 0
    raw_items = getattr(headers, "raw_items", None)
    items = raw_items() if callable(raw_items) else headers.items() if hasattr(headers, "items") else []
    return sum(
        len(str(name).encode("utf-8")) + len(str(value).encode("utf-8")) + 4
        for name, value in items
    )


def _parse_declared_length(value: str) -> int:
    normalized = value.strip()
    if not re.fullmatch(r"(?:0|[1-9]\d*)", normalized):
        raise McpTransportError(
            "invalid_framing",
            "MCP Content-Length is malformed.",
        )
    parsed = int(normalized)
    if parsed > 9_007_199_254_740_991:
        raise McpTransportError(
            "invalid_framing",
            "MCP Content-Length is not a safe integer.",
        )
    return parsed


def _set_response_timeout(response: Any, timeout: float) -> None:
    candidates = (
        getattr(getattr(getattr(response, "fp", None), "raw", None), "_sock", None),
        getattr(getattr(response, "fp", None), "_sock", None),
        getattr(response, "sock", None),
    )
    for candidate in candidates:
        setter = getattr(candidate, "settimeout", None)
        if callable(setter):
            with contextlib.suppress(Exception):
                setter(timeout)
            return


def _close_http_response(response: Any) -> None:
    candidates = (
        getattr(getattr(getattr(response, "fp", None), "raw", None), "_sock", None),
        getattr(getattr(response, "fp", None), "_sock", None),
        getattr(response, "sock", None),
        getattr(getattr(response, "_mcp_connection", None), "sock", None),
    )
    seen: set[int] = set()
    for candidate in candidates:
        if candidate is None or id(candidate) in seen:
            continue
        seen.add(id(candidate))
        shutdown = getattr(candidate, "shutdown", None)
        if callable(shutdown):
            with contextlib.suppress(Exception):
                shutdown(socket.SHUT_RDWR)
    closer = getattr(response, "close", None)
    if callable(closer):
        with contextlib.suppress(Exception):
            closer()
    connection = getattr(response, "_mcp_connection", None)
    connection_closer = getattr(connection, "close", None)
    if callable(connection_closer):
        with contextlib.suppress(Exception):
            connection_closer()


class _NoMcpRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _validate_outgoing_headers(headers: dict[str, str]) -> None:
    size = sum(
        len(str(name).encode("utf-8")) + len(str(value).encode("utf-8")) + 4
        for name, value in headers.items()
    )
    if size > MCP_HEADER_MAX_BYTES:
        raise McpTransportError(
            "header_limit",
            f"MCP request headers exceed {MCP_HEADER_MAX_BYTES} bytes.",
        )


class _BoundedHeaderReader:
    def __init__(self, stream: Any) -> None:
        self.stream = stream
        self.first_line = True
        self.header_bytes = 0

    def readline(self, limit: int = -1) -> bytes:
        if self.first_line:
            self.first_line = False
            return self.stream.readline(limit)
        remaining = MCP_HEADER_MAX_BYTES - self.header_bytes
        bounded = remaining + 1 if limit < 0 else min(limit, remaining + 1)
        line = self.stream.readline(bounded)
        self.header_bytes += len(line)
        if self.header_bytes > MCP_HEADER_MAX_BYTES:
            raise McpTransportError(
                "header_limit",
                f"MCP response headers exceed {MCP_HEADER_MAX_BYTES} bytes.",
            )
        return line

    def __getattr__(self, name: str) -> Any:
        return getattr(self.stream, name)


class _BoundedHTTPResponse(HTTPResponse):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.fp = _BoundedHeaderReader(self.fp)


def _open_without_redirects(
    request: Request,
    connect_timeout: float,
    headers_timeout: float | None = None,
    exchange: Any = None,
) -> Any:
    if getattr(urlopen, "__module__", "") != "urllib.request":
        return urlopen(
            request,
            timeout=min(connect_timeout, headers_timeout or connect_timeout),
        )
    if exchange is None:
        return build_opener(_NoMcpRedirect()).open(request, timeout=connect_timeout)
    parsed = urlparse(request.full_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise McpTransportError("invalid_framing", "MCP HTTP URL is invalid.")
    if parsed.username is not None or parsed.password is not None:
        raise McpTransportError(
            "invalid_framing",
            "MCP HTTP URL must not contain embedded credentials.",
        )
    try:
        port = parsed.port
    except ValueError as error:
        raise McpTransportError("invalid_framing", "MCP HTTP URL port is invalid.") from error
    connection_type = HTTPSConnection if parsed.scheme == "https" else HTTPConnection
    connection = connection_type(parsed.hostname, port, timeout=connect_timeout)
    connection.response_class = _BoundedHTTPResponse
    exchange.bind(connection)
    try:
        try:
            connection.connect()
        except (TimeoutError, socket.timeout) as error:
            raise McpTransportError(
                "connect_timeout",
                "MCP connection deadline exceeded.",
            ) from error
        if connection.sock is not None:
            connection.sock.settimeout(headers_timeout or connect_timeout)
        path = parsed.path or "/"
        if parsed.params:
            path += f";{parsed.params}"
        if parsed.query:
            path += f"?{parsed.query}"
        headers = dict(request.header_items())
        body = request.data
        lowered = {name.lower() for name in headers}
        if body is not None and "content-length" not in lowered:
            headers["Content-Length"] = str(len(body))
        connection.putrequest(
            request.get_method(),
            path,
            skip_host="host" in lowered,
            skip_accept_encoding="accept-encoding" in lowered,
        )
        for name, value in headers.items():
            connection.putheader(name, value)
        try:
            connection.endheaders(body)
            response = connection.getresponse()
        except (TimeoutError, socket.timeout) as error:
            raise McpTransportError(
                "headers_timeout",
                "MCP response-header deadline exceeded.",
            ) from error
        response._mcp_connection = connection
        exchange.bind(response)
        exchange.unbind(connection)
        return response
    except BaseException:
        exchange.unbind(connection)
        connection.close()
        raise
