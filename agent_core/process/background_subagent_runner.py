from ..tasks.runners import subagent as _impl
from ..tasks.runners.subagent import *

for _name in dir(_impl):
    if _name.startswith("_") and not _name.startswith("__"):
        globals()[_name] = getattr(_impl, _name)

if __name__ == "__main__":
    main()
