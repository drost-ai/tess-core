from __future__ import annotations

import sys as _sys
import types as _types
from importlib import import_module

_MODULE_NAMES = ['shared', 'transport_policy', 'transport_shared', 'stdio_decoder', 'sse_decoder', 'http_body', 'negotiation', 'token_store', 'auth_loopback', 'auth', 'runtime', 'clients', 'stdio_transport', 'http_transport', 'sse_transport', 'state', 'notifications', 'normalization', 'blobs', 'protocol', 'cancellation']

_modules = [import_module(f"{__name__}.{name}") for name in _MODULE_NAMES]
_namespace = {}
for _module in _modules:
    for _name, _value in vars(_module).items():
        if not _name.startswith("__"):
            _namespace[_name] = _value
for _module in _modules:
    vars(_module).update(_namespace)
globals().update(_namespace)

_PUBLIC_EXPORTS = ['Any', 'Callable', 'FileMcpTokenStore', 'HTTPError', 'HttpMcpClient', 'JsonRpcClient', 'McpAuthorizationRequiredError', 'McpOAuthRuntime', 'PROTOCOL_VERSION', 'Path', 'Request', 'SseMcpClient', 'StdioMcpClient', 'ToolDefinition', 'annotations', 'asyncio', 'base64', 'contextlib_suppress', 'createFileMcpTokenStore', 'createMcpOAuthRuntime', 'createMcpRuntime', 'create_file_mcp_token_store', 'getStateRoot', 'hashlib', 'json', 'mcpTokenStoreKey', 'mcp_token_store_key', 'now_iso', 'os', 'pretty_json', 'queue', 're', 'socket', 'subprocess', 'threading', 'unquote', 'urlopen', 'urlparse', 'uuid']

__all__ = _PUBLIC_EXPORTS


def __dir__():
    return sorted(_PUBLIC_EXPORTS)


class _McpRuntimeModule(_types.ModuleType):
    def __setattr__(self, name, value):
        super().__setattr__(name, value)
        if name.startswith("__"):
            return
        for module in _modules:
            if name in vars(module):
                setattr(module, name, value)


_sys.modules[__name__].__class__ = _McpRuntimeModule
