from .tasks import supervisor as _impl
from .tasks.supervisor import *
import sys as _sys
import types as _types

for _name in dir(_impl):
    if _name.startswith("_") and not _name.startswith("__"):
        globals()[_name] = getattr(_impl, _name)


class _TaskSupervisorModule(_types.ModuleType):
    def __setattr__(self, name, value):
        super().__setattr__(name, value)
        if name.startswith("__"):
            return
        setattr(_impl, name, value)


_sys.modules[__name__].__class__ = _TaskSupervisorModule
