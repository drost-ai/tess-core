from __future__ import annotations

import argparse
import asyncio
import os

from ..daemon import runTaskSupervisorDaemon
from ..storage import configureRuntimeHome


def main() -> None:
    parser = argparse.ArgumentParser(description="agent-core Python task supervisor daemon")
    parser.add_argument("--cwd", required=True)
    parser.add_argument("--instance-id", required=True)
    args = parser.parse_args()
    runtime_home = os.environ.get("AGENT_CORE_HOME")
    if runtime_home:
        configureRuntimeHome(runtime_home)
    asyncio.run(runTaskSupervisorDaemon(args.cwd, args.instance_id))


if __name__ == "__main__":
    main()
