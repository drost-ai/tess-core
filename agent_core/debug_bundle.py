from __future__ import annotations

import os
import shutil
from typing import Any


async def createDebugBundle(args: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
    args = {**(args or {}), **kwargs}
    output = args.get("outputPath") or os.path.join(args.get("cwd", os.getcwd()), "agent-core-debug-bundle")
    os.makedirs(output, exist_ok=True)
    return {"path": output, "files": []}
