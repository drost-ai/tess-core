from .tasks import subagents as _impl
from .tasks.subagents import *

for _name in dir(_impl):
    if _name.startswith("_") and not _name.startswith("__"):
        globals()[_name] = getattr(_impl, _name)
