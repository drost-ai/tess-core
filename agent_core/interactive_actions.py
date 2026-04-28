from __future__ import annotations

import sys

from .shell_surface import *

try:
    del shell_surface
except NameError:
    pass

_parent = sys.modules.get(__package__)
if _parent is not None:
    _parent.__dict__.pop("shell_surface", None)
del sys
