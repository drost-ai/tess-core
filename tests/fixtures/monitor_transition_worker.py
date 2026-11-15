from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path

from tess_core._utils import now_iso
from tess_core.tasks.monitors import create_monitor_record
from tess_core.storage import configure_runtime_home
from tess_core.tasks.monitor_observation import apply_monitor_observation
from tess_core.tasks.monitor_transactions import (
    commit_monitor_mutation,
    start_monitor_once,
)
from tess_core.tasks.monitor_transitions import stop_monitor_record


async def main() -> None:
    (
        runtime_home,
        session_id,
        task_id,
        monitor_id,
        operation,
        ready_path,
        go_path,
        result_path,
        event_path,
    ) = sys.argv[1:]
    configure_runtime_home(runtime_home)
    with open(ready_path, "a", encoding="utf-8") as handle:
        handle.write(f"{os.getpid()}\n")
    deadline = time.monotonic() + 10
    while not Path(go_path).exists():
        if time.monotonic() >= deadline:
            raise RuntimeError("monitor worker barrier timed out")
        await asyncio.sleep(0.01)
    if operation == "start":
        commit = await start_monitor_once(
            sessionId=session_id,
            taskId=task_id,
            create=lambda: create_monitor_record(
                {
                    "id": f"monitor-worker-{os.getpid()}",
                    "sessionId": session_id,
                    "taskId": task_id,
                    "goal": "watch race",
                }
            ),
        )
        result = {
            "changed": commit["changed"],
            "monitorId": commit["monitor"]["id"],
            "revision": commit["revision"],
            "transitionId": commit.get("transitionId"),
            "status": commit["monitor"]["status"],
        }
    else:
        commit = await commit_monitor_mutation(
            monitorId=monitor_id,
            expectedSessionId=session_id,
            mutate=(
                lambda latest: _stop_mutation(latest)
                if operation == "stop"
                else _complete_mutation(latest)
            ),
        )
        result = {
            "changed": commit["changed"],
            "monitorId": commit["current"]["id"],
            "revision": commit["revision"],
            "transitionId": commit.get("transitionId"),
            "status": commit["current"]["status"],
        }
    Path(result_path).write_text(json.dumps(result), encoding="utf-8")
    if result.get("transitionId"):
        with open(event_path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(result) + "\n")


def _stop_mutation(latest):
    transition = stop_monitor_record(latest, now_iso())
    return {
        "record": transition["record"],
        "changed": transition["changed"],
        "transition": transition["changed"],
        "transitionLabel": (
            f"stop:{transition['previousStatus']}->{transition['record']['status']}"
        ),
        "value": transition,
    }


def _complete_mutation(latest):
    transition = apply_monitor_observation(
        latest,
        task={"status": "completed"},
        outputSize=321,
        now=1_783_000_000_000,
    )
    return {**transition, "value": transition}


if __name__ == "__main__":
    asyncio.run(main())
