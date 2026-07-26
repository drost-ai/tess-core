
from __future__ import annotations

import sys as _sys
import types as _types
from importlib import import_module

_MODULE_NAMES = ['shared', 'provider_payloads', 'payloads', 'factory']

_modules = [import_module(f"{__name__}.{name}") for name in _MODULE_NAMES]
_namespace = {}
for _module in _modules:
    for _name, _value in vars(_module).items():
        if not _name.startswith("__"):
            _namespace[_name] = _value
for _module in _modules:
    vars(_module).update(_namespace)
globals().update(_namespace)

_PUBLIC_EXPORTS = ['Any', 'KNOWN_RUNTIME_EVENTS', 'annotations', 'createRuntimeEvent', 'math', 'now_iso']
__all__ = _PUBLIC_EXPORTS


def __dir__():
    return sorted(_PUBLIC_EXPORTS)


class _RuntimeEventPartsModule(_types.ModuleType):
    def __setattr__(self, name, value):
        super().__setattr__(name, value)
        if name.startswith("__"):
            return
        for module in _modules:
            if name in vars(module):
                setattr(module, name, value)


_sys.modules[__name__].__class__ = _RuntimeEventPartsModule
