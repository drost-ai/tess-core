from .._utils import packageExports as _package_exports
from ..tasks import subagents as _impl
from ..tasks.subagents import *
import sys as _sys
import types as _types

for _name in dir(_impl):
    if _name.startswith("_") and not _name.startswith("__"):
        globals()[_name] = getattr(_impl, _name)


class _SubagentsModule(_types.ModuleType):
    def __setattr__(self, name, value):
        super().__setattr__(name, value)
        if name.startswith("__"):
            return
        setattr(_impl, name, value)


_sys.modules[__name__].__class__ = _SubagentsModule

__all__ = _package_exports(globals(), {'capacity', 'continuation', 'continuation_recovery', 'contract', 'coordination', 'depth_policy', 'groups', 'limits', 'reconciliation', 'reconciliation_renderer', 'reconciliation_store', 'result', 'result_payload'})


def __dir__():
    return __all__
