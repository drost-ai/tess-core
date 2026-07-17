from __future__ import annotations

import sys as _sys
import types as _types

from . import runtime_bootstrap as _impl
from .runtime_bootstrap import *


class _RuntimeModule(_types.ModuleType):
    def __setattr__(self, name, value):
        super().__setattr__(name, value)
        if not name.startswith("__"):
            setattr(_impl, name, value)


_sys.modules[__name__].__class__ = _RuntimeModule
