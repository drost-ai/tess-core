from __future__ import annotations

import sys as _sys
import types as _types

from . import config_store as _impl
from .config_store import *

for _name in vars(_impl):
    if _name.startswith("_") and not _name.startswith("__"):
        globals()[_name] = getattr(_impl, _name)


class _ConfigModule(_types.ModuleType):
    def __setattr__(self, name, value):
        super().__setattr__(name, value)
        if name.startswith("__"):
            return
        setattr(_impl, name, value)


_sys.modules[__name__].__class__ = _ConfigModule

_parent = _sys.modules.get(__package__)
if _parent is not None and hasattr(_parent, 'config_store'):
    delattr(_parent, 'config_store')
