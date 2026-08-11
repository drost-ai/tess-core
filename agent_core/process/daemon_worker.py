from __future__ import annotations

import argparse
import asyncio
import os

from ..daemon import runTaskSupervisorDaemon
from ..storage import configure_runtime_home


def main() -> None:
    parser = argparse.ArgumentParser(description="agent-core Python task supervisor daemon")
    parser.add_argument("--cwd", required=True)
    parser.add_argument("--instance-id", required=True)
    parser.add_argument("--owner-pid", type=int, default=None)
    args = parser.parse_args()
    runtime_home = os.environ.get("AGENT_CORE_HOME")
    if runtime_home:
        configure_runtime_home(runtime_home)
    # The daemon enforces knob-governed policy (capacity, tick intervals),
    # so it must install the settings-folder knob overrides like any
    # config-resolving process would.
    from ..storage.settings import load_settings
    from ..config._knobs import configureKnobs

    settings = asyncio.run(load_settings("user", args.cwd))
    configureKnobs(settings.get("knobs"))
    asyncio.run(runTaskSupervisorDaemon(args.cwd, args.instance_id, ownerPid=args.owner_pid))


if __name__ == "__main__":
    main()
