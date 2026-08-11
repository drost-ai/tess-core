from __future__ import annotations

import asyncio
from typing import Any

import agent_core._facade as core
from agent_core.agent.tool_dispatch import (
    resolve_tool_concurrency,
    run_tool_dispatch_batch,
)


async def run_tool_dispatch_parity(data: dict[str, Any]) -> dict[str, Any]:
    parallel_started = asyncio.Event()
    exclusive_started = asyncio.Event()
    exclusive_active = 0
    maximum_exclusive_active = 0
    parallel_saw_exclusive = False
    exclusive_saw_parallel = False
    exclusive_order: list[str] = []

    async def dispatch(tool_use: dict[str, Any]) -> dict[str, Any]:
        nonlocal exclusive_active, maximum_exclusive_active
        nonlocal parallel_saw_exclusive, exclusive_saw_parallel
        name = str(tool_use["name"])
        if name == "parallel":
            parallel_started.set()
            await exclusive_started.wait()
            parallel_saw_exclusive = True
        else:
            exclusive_active += 1
            maximum_exclusive_active = max(maximum_exclusive_active, exclusive_active)
            exclusive_order.append(name)
            exclusive_started.set()
            await parallel_started.wait()
            exclusive_saw_parallel = True
            exclusive_active -= 1
        return {
            "result": {
                "type": "tool_result",
                "tool_use_id": tool_use["id"],
                "content": name,
            }
        }

    tool_uses = [
        {"id": "exclusive-1", "name": "exclusive-1"},
        {"id": "parallel", "name": "parallel"},
        {"id": "exclusive-2", "name": "exclusive-2"},
    ]
    batch = await run_tool_dispatch_batch(
        tool_uses=tool_uses,
        sequential=False,
        concurrency_of=lambda item: (
            "parallel" if item["name"] == "parallel" else "exclusive"
        ),
        run=dispatch,
    )

    pause_ran: list[str] = []

    async def pause_dispatch(tool_use: dict[str, Any]) -> dict[str, Any]:
        pause_ran.append(str(tool_use["name"]))
        outcome = {
            "result": {
                "type": "tool_result",
                "tool_use_id": tool_use["id"],
                "content": tool_use["name"],
            }
        }
        if tool_use["name"] == "pause":
            outcome["pauseMessage"] = "approval"
        return outcome

    pause_batch = await run_tool_dispatch_batch(
        tool_uses=[
            {"id": "pause", "name": "pause"},
            {"id": "parallel-after-pause", "name": "parallel"},
            {"id": "exclusive-skipped", "name": "exclusive"},
        ],
        sequential=False,
        concurrency_of=lambda item: (
            "parallel" if item["name"] == "parallel" else "exclusive"
        ),
        run=pause_dispatch,
    )

    async def noop(_input: dict[str, Any], _context: dict[str, Any]) -> dict[str, Any]:
        return {"content": "ok"}

    parallel = core.ToolDefinition(
        "parallel",
        "parallel",
        {"type": "object"},
        noop,
        concurrency="parallel",
    )
    return {
        "name": data.get("name") or data.get("action") or "tool-dispatch",
        "dispatch": {
            "defaults": {
                "missing": resolve_tool_concurrency(None),
                "declaredParallel": resolve_tool_concurrency(parallel),
            },
            "resultOrder": [item.get("tool_use_id") for item in batch["results"]],
            "parallelOverlapped": parallel_saw_exclusive and exclusive_saw_parallel,
            "exclusiveSerialized": maximum_exclusive_active == 1,
            "exclusiveOrder": exclusive_order,
            "pause": {
                "ran": sorted(pause_ran),
                "resultOrder": [item.get("tool_use_id") for item in pause_batch["results"]],
                "pausedForApproval": pause_batch["pausedForApproval"],
                "skippedLaterExclusive": "Skipped" in str(pause_batch["results"][2].get("content")),
            },
        },
    }
