from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from ..tools.definitions import ToolConcurrency, ToolDefinition


ToolDispatchOutcome = dict[str, Any]


def invalid_tool_input_message(tool_use: Any) -> str | None:
    if not isinstance(tool_use, dict):
        return None
    if tool_use.get("_agent_core_invalid_tool_input") is True:
        message = tool_use.get("_agent_core_invalid_tool_input_error")
        if isinstance(message, str) and message.strip():
            return message.strip()
        return "The provider streamed incomplete or invalid JSON for this tool call. Retry the tool call with complete valid JSON arguments."
    input_value = tool_use.get("input")
    if not isinstance(input_value, dict) or input_value.get("__agent_core_invalid_tool_input") is not True:
        return None
    message = input_value.get("error")
    if isinstance(message, str) and message.strip():
        return message.strip()
    return "The provider streamed incomplete or invalid JSON for this tool call. Retry the tool call with complete valid JSON arguments."


def resolve_tool_concurrency(tool: ToolDefinition | None) -> ToolConcurrency:
    """Resolve an internal tool's same-batch execution contract fail-safely."""

    return "parallel" if getattr(tool, "concurrency", None) == "parallel" else "exclusive"


def approval_skip_result(tool_use: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "tool_result",
        "tool_use_id": tool_use.get("id"),
        "is_error": True,
        "content": "Skipped: previous tool in the batch paused for approval.",
    }


class SerialQueue:
    """Serialize async commits without poisoning later callers on failure."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()

    async def run(self, task: Callable[[], Awaitable[None]]) -> None:
        async with self._lock:
            await task()


def create_serial_queue() -> SerialQueue:
    return SerialQueue()


async def run_tool_dispatch_batch(
    *,
    tool_uses: list[dict[str, Any]],
    sequential: bool,
    concurrency_of: Callable[[dict[str, Any]], ToolConcurrency],
    run: Callable[[dict[str, Any]], Awaitable[ToolDispatchOutcome]],
) -> dict[str, Any]:
    """Dispatch a provider tool batch while preserving emission-order results."""

    outcomes: list[ToolDispatchOutcome | None] = [None] * len(tool_uses)

    if sequential:
        paused_at: int | None = None
        for index, tool_use in enumerate(tool_uses):
            outcome = await run(tool_use)
            outcomes[index] = outcome
            if outcome.get("pauseMessage"):
                paused_at = index
                break
        if paused_at is not None:
            for index in range(paused_at + 1, len(tool_uses)):
                outcomes[index] = {"result": approval_skip_result(tool_uses[index])}
        return _assemble_batch(tool_uses, outcomes)

    exclusive_order: list[int] = []
    parallel_order: list[int] = []
    for index, tool_use in enumerate(tool_uses):
        if concurrency_of(tool_use) == "parallel":
            parallel_order.append(index)
        else:
            exclusive_order.append(index)

    async def run_parallel(index: int) -> None:
        outcomes[index] = await run(tool_uses[index])

    async def run_exclusive() -> None:
        for position, index in enumerate(exclusive_order):
            outcome = await run(tool_uses[index])
            outcomes[index] = outcome
            if outcome.get("pauseMessage"):
                for skipped_index in exclusive_order[position + 1 :]:
                    outcomes[skipped_index] = {"result": approval_skip_result(tool_uses[skipped_index])}
                return

    tasks = [asyncio.create_task(run_parallel(index)) for index in parallel_order]
    tasks.append(asyncio.create_task(run_exclusive()))
    try:
        await asyncio.gather(*tasks)
    except BaseException:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise
    return _assemble_batch(tool_uses, outcomes)


def _assemble_batch(
    tool_uses: list[dict[str, Any]],
    outcomes: list[ToolDispatchOutcome | None],
) -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    blocks: list[dict[str, Any]] = []
    paused_for_approval = False
    paused_message = ""
    for index, tool_use in enumerate(tool_uses):
        outcome = outcomes[index]
        if outcome is None:
            # Defensive transcript preservation if a caller supplied a broken
            # scheduler callback; normal execution never reaches this branch.
            outcome = {
                "result": {
                    "type": "tool_result",
                    "tool_use_id": tool_use.get("id"),
                    "is_error": True,
                    "content": "Tool dispatch did not produce a result.",
                }
            }
        results.append(outcome["result"])
        blocks.append(outcome["result"])
        blocks.extend(outcome.get("additionalResults") or [])
        if outcome.get("pauseMessage") and not paused_for_approval:
            paused_for_approval = True
            paused_message = str(outcome["pauseMessage"])
    return {
        "results": results,
        "blocks": blocks,
        "pausedForApproval": paused_for_approval,
        "pausedMessage": paused_message,
    }
