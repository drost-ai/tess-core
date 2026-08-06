from __future__ import annotations

import sys as _sys
import types as _types

from . import state_parts as _impl
from .state_parts import *

for _name in vars(_impl):
    if _name.startswith("_") and not _name.startswith("__"):
        globals()[_name] = getattr(_impl, _name)


class _RuntimeStateModule(_types.ModuleType):
    def __setattr__(self, name, value):
        super().__setattr__(name, value)
        if name.startswith("__"):
            return
        setattr(_impl, name, value)


_sys.modules[__name__].__class__ = _RuntimeStateModule

_parent = _sys.modules.get(__package__)
if _parent is not None and hasattr(_parent, 'state_parts'):
    delattr(_parent, 'state_parts')
