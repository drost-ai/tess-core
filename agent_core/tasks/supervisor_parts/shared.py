from __future__ import annotations

import asyncio
import subprocess
import threading
from typing import Any


_PROCESS_REGISTRY: dict[str, subprocess.Popen[Any]] = {}
_WATCHERS: set[threading.Thread | asyncio.Task[Any]] = set()
