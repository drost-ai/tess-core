"""MCP: JSON-RPC clients, transports (stdio/HTTP/SSE), OAuth, and the runtime.

The facade re-exports exactly the public names its submodules define.
"""
from __future__ import annotations

from .auth import (
    McpAuthorizationRequiredError,
    McpOAuthRuntime,
    canonical_origin,
    create_mcp_oauth_runtime,
)
from .auth_loopback import LoopbackListener
from .clients import JsonRpcClient
from .http_body import (
    iterate_mcp_body,
    read_mcp_body_bytes,
    read_mcp_body_text,
    validate_mcp_response_headers,
)
from .http_transport import HttpMcpClient
from .negotiation import (
    LEGACY_PROTOCOL_VERSION,
    PREFERRED_PROTOCOL_VERSION,
    SUPPORTED_PROTOCOL_VERSIONS,
    bounded_capabilities,
    resolve_negotiated_protocol,
)
from .payloads import (
    MAX_MCP_CONTENTS,
    shape_mcp_resource_list,
    shape_mcp_resource_read,
)
from .runtime import create_mcp_runtime
from .sse_decoder import (
    McpSseDecoder,
    parse_sse_events,
)
from .sse_transport import SseMcpClient
from .stdio_decoder import McpStdioFrameDecoder
from .stdio_transport import StdioMcpClient
from .token_store import (
    FileMcpTokenStore,
    create_file_mcp_token_store,
    mcp_token_store_key,
    token_store_delete,
    token_store_get,
    token_store_set,
)
from .transport_policy import (
    MCP_DECODED_BODY_MAX_BYTES,
    MCP_FRAME_MAX_BYTES,
    MCP_HEADER_MAX_BYTES,
    MCP_NOTIFICATION_QUEUE_MAX,
    MCP_PENDING_REQUEST_MAX,
    MCP_SSE_EVENT_MAX_BYTES,
    MCP_STDERR_TAIL_MAX_BYTES,
    McpTransportError,
    resolve_mcp_transport_policy,
)
from .transport_shared import (
    BoundedNotificationQueue,
    contextlib_suppress,
)

__all__ = [
    "BoundedNotificationQueue",
    "FileMcpTokenStore",
    "HttpMcpClient",
    "JsonRpcClient",
    "LEGACY_PROTOCOL_VERSION",
    "LoopbackListener",
    "MAX_MCP_CONTENTS",
    "MCP_DECODED_BODY_MAX_BYTES",
    "MCP_FRAME_MAX_BYTES",
    "MCP_HEADER_MAX_BYTES",
    "MCP_NOTIFICATION_QUEUE_MAX",
    "MCP_PENDING_REQUEST_MAX",
    "MCP_SSE_EVENT_MAX_BYTES",
    "MCP_STDERR_TAIL_MAX_BYTES",
    "McpAuthorizationRequiredError",
    "McpOAuthRuntime",
    "McpSseDecoder",
    "McpStdioFrameDecoder",
    "McpTransportError",
    "PREFERRED_PROTOCOL_VERSION",
    "SUPPORTED_PROTOCOL_VERSIONS",
    "SseMcpClient",
    "StdioMcpClient",
    "bounded_capabilities",
    "canonical_origin",
    "contextlib_suppress",
    "create_file_mcp_token_store",
    "create_mcp_oauth_runtime",
    "create_mcp_runtime",
    "iterate_mcp_body",
    "mcp_token_store_key",
    "parse_sse_events",
    "read_mcp_body_bytes",
    "read_mcp_body_text",
    "resolve_mcp_transport_policy",
    "resolve_negotiated_protocol",
    "shape_mcp_resource_list",
    "shape_mcp_resource_read",
    "token_store_delete",
    "token_store_get",
    "token_store_set",
    "validate_mcp_response_headers",
]
