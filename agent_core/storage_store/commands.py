from __future__ import annotations

from .shared import *

async def runCommand(command: str, args: list[str], cwd: str) -> str:
    proc = await asyncio.create_subprocess_exec(
        command,
        *args,
        cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode:
        raise RuntimeError(stderr.decode("utf-8", errors="replace"))
    return stdout.decode("utf-8", errors="replace")
