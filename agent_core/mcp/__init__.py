"""MCP: JSON-RPC clients, transports (stdio/HTTP/SSE), OAuth, and the runtime.

The facade re-exports exactly the public names its submodules define.
"""
from __future__ import annotations

from .auth import (
    McpAuthorizationRequiredError,
    McpOAuthRuntime,
    canonical_origin,
    createMcpOAuthRuntime,
    create_mcp_oauth_runtime,
)
from .auth_loopback import LoopbackListener
from .clients import JsonRpcClient
from .http_body import (
    iterateMcpBody,
    readMcpBodyBytes,
    readMcpBodyText,
    validateMcpResponseHeaders,
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
    DEFAULT_INLINE_BYTES,
    MAX_MCP_CONTENTS,
    MAX_MCP_RESOURCE_PAGE,
    shapeMcpResourceList,
    shapeMcpResourceRead,
    shape_mcp_resource_list,
    shape_mcp_resource_read,
)
from .runtime import createMcpRuntime
from .sse_decoder import (
    McpSseDecoder,
    parseSseEvents,
)
from .sse_transport import SseMcpClient
from .stdio_decoder import McpStdioFrameDecoder
from .stdio_transport import StdioMcpClient
from .token_store import (
    FileMcpTokenStore,
    createFileMcpTokenStore,
    mcpTokenStoreKey,
    token_store_delete,
    token_store_get,
    token_store_set,
)
from .transport_policy import (
    MCP_CLOSE_GRACE_MS,
    MCP_CLOSE_KILL_WAIT_MS,
    MCP_CLOSE_STDIN_GRACE_MS,
    MCP_DECODED_BODY_MAX_BYTES,
    MCP_DEFAULT_BODY_IDLE_TIMEOUT_MS,
    MCP_DEFAULT_CONNECT_TIMEOUT_MS,
    MCP_DEFAULT_HEADERS_TIMEOUT_MS,
    MCP_DEFAULT_REQUEST_TIMEOUT_MS,
    MCP_DEFAULT_STARTUP_TIMEOUT_MS,
    MCP_FRAME_MAX_BYTES,
    MCP_HEADER_MAX_BYTES,
    MCP_NOTIFICATION_QUEUE_MAX,
    MCP_PENDING_REQUEST_MAX,
    MCP_SSE_EVENT_MAX_BYTES,
    MCP_STDERR_TAIL_MAX_BYTES,
    McpTransportError,
    resolveMcpTransportPolicy,
)
from .transport_shared import (
    BoundedNotificationQueue,
    contextlib_suppress,
)

__all__ = [
    "BoundedNotificationQueue",
    "DEFAULT_INLINE_BYTES",
    "FileMcpTokenStore",
    "HttpMcpClient",
    "JsonRpcClient",
    "LEGACY_PROTOCOL_VERSION",
    "LoopbackListener",
    "MAX_MCP_CONTENTS",
    "MAX_MCP_RESOURCE_PAGE",
    "MCP_CLOSE_GRACE_MS",
    "MCP_CLOSE_KILL_WAIT_MS",
    "MCP_CLOSE_STDIN_GRACE_MS",
    "MCP_DECODED_BODY_MAX_BYTES",
    "MCP_DEFAULT_BODY_IDLE_TIMEOUT_MS",
    "MCP_DEFAULT_CONNECT_TIMEOUT_MS",
    "MCP_DEFAULT_HEADERS_TIMEOUT_MS",
    "MCP_DEFAULT_REQUEST_TIMEOUT_MS",
    "MCP_DEFAULT_STARTUP_TIMEOUT_MS",
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
    "createFileMcpTokenStore",
    "createMcpOAuthRuntime",
    "createMcpRuntime",
    "create_mcp_oauth_runtime",
    "iterateMcpBody",
    "mcpTokenStoreKey",
    "parseSseEvents",
    "readMcpBodyBytes",
    "readMcpBodyText",
    "resolveMcpTransportPolicy",
    "resolve_negotiated_protocol",
    "shapeMcpResourceList",
    "shapeMcpResourceRead",
    "shape_mcp_resource_list",
    "shape_mcp_resource_read",
    "token_store_delete",
    "token_store_get",
    "token_store_set",
    "validateMcpResponseHeaders",
]
