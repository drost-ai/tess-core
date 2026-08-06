
from __future__ import annotations

import sys as _sys
import types as _types
from importlib import import_module

_MODULE_NAMES = ['shared', 'payloads', 'transport', 'sse', 'request']

_modules = [import_module(f"{__name__}.{name}") for name in _MODULE_NAMES]
_namespace = {}
for _module in _modules:
    for _name, _value in vars(_module).items():
        if not _name.startswith("__"):
            _namespace[_name] = _value
for _module in _modules:
    vars(_module).update(_namespace)
globals().update(_namespace)

_PUBLIC_EXPORTS = ['ANTHROPIC_ABORT_POLL_INTERVAL_MS', 'ANTHROPIC_JSON_RESPONSE_TIMEOUT_MS', 'ANTHROPIC_REQUEST_START_TIMEOUT_MS', 'ANTHROPIC_STREAM_IDLE_TIMEOUT_MS', 'Any', 'HTTPError', 'MESSAGES_API_URL', 'MESSAGES_COUNT_TOKENS_API_URL', 'Request', 'annotations', 'asyncio', 'buildAnthropicCountTokensPayload', 'buildAnthropicHeaders', 'buildAnthropicMessagesPayload', 'canRetryProviderFailure', 'classifyProviderHttpFailure', 'contextlib', 'createProviderFailureRecord', 'createProviderRequestError', 'json', 'normalizeAnthropicContentBlocks', 'normalizeAnthropicSseResponse', 'normalizeProviderBlocks', 'normalizeUnknownProviderFailure', 'parseAnthropicSseEvents', 'providerRetryDelayMs', 're', 'resolveAnthropicCountTokensUrl', 'resolveAnthropicMessagesUrl', 'sendAnthropicMessagesRequest', 'threading', 'urlopen']
__all__ = _PUBLIC_EXPORTS


def __dir__():
    return sorted(_PUBLIC_EXPORTS)


class _AnthropicPartsModule(_types.ModuleType):
    def __setattr__(self, name, value):
        super().__setattr__(name, value)
        if name.startswith("__"):
            return
        for module in _modules:
            if name in vars(module):
                setattr(module, name, value)


_sys.modules[__name__].__class__ = _AnthropicPartsModule
