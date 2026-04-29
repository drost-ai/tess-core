from __future__ import annotations

import sys as _sys
import types as _types

from . import agent_loop as _impl
from .agent_loop import *

for _name in vars(_impl):
    if _name.startswith("_") and not _name.startswith("__"):
        globals()[_name] = getattr(_impl, _name)


class _AgentModule(_types.ModuleType):
    def __setattr__(self, name, value):
        super().__setattr__(name, value)
        if name.startswith("__"):
            return
        setattr(_impl, name, value)


_sys.modules[__name__].__class__ = _AgentModule

_parent = _sys.modules.get(__package__)
if _parent is not None and hasattr(_parent, 'agent_loop'):
    delattr(_parent, 'agent_loop')
