"""Collaboration runtime: worker snapshots, narration, remote sessions, bridges."""

from __future__ import annotations

import sys as _sys
import types as _types

from .._utils import packageExports as _package_exports
from . import runtime as _impl
from .runtime import *

for _name in vars(_impl):
    if _name.startswith("_") and not _name.startswith("__"):
        globals()[_name] = getattr(_impl, _name)


class _CollaborationModule(_types.ModuleType):
    # Tests and embedders replace provider senders on ``agent_core.collaboration``;
    # forward those onto the implementation module so the running code sees them.
    def __setattr__(self, name, value):
        super().__setattr__(name, value)
        if not name.startswith("__"):
            setattr(_impl, name, value)


_sys.modules[__name__].__class__ = _CollaborationModule

__all__ = _package_exports(globals(), {"bridge", "remote", "runtime", "updates"})
