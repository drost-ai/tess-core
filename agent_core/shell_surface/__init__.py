from __future__ import annotations

from importlib import import_module

_MODULE_NAMES = [
    "shared",
    "registry",
    "common",
    "status",
    "tasks",
    "agents",
    "remote_background",
    "context_commands",
    "diagnostics",
    "dispatcher",
]

_modules = [import_module(f"{__name__}.{name}") for name in _MODULE_NAMES]
_namespace = {}
for _module in _modules:
    for _name, _value in vars(_module).items():
        if not _name.startswith("__"):
            _namespace[_name] = _value
for _module in _modules:
    vars(_module).update(_namespace)
globals().update(_namespace)

__all__ = sorted(_namespace)
