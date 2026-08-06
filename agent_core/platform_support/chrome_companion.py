from __future__ import annotations

import asyncio
import sys

from .chrome import runChromeCompanionMcpServer, runChromeNativeHost


async def main(argv: list[str] | None = None) -> None:
    argv = list(argv if argv is not None else sys.argv[1:])
    mode = argv[0] if argv else "mcp"
    if mode in {"native", "native-host", "--native-host"}:
        await runChromeNativeHost()
        return
    await runChromeCompanionMcpServer()


if __name__ == "__main__":
    asyncio.run(main())
