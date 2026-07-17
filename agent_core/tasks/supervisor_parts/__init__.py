from __future__ import annotations

from importlib import import_module

_MODULE_NAMES = [
    "shared",
    "utils",
    "mappers",
    "summaries",
    "records",
    "processes",
    "live_work",
    "launch",
    "subagent_launch",
    "health",
    "communication",
    "record_issues",
    "projection",
    "reconciliation",
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
