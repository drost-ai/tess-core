from __future__ import annotations

from typing import Any, Callable

from ..payloads.agent_results import finalize_tool_dispatch_result


class ToolResultBatchBudget:
    """Finishes the results of one batch of tool calls.

    Each result is held to its own tool's allowance and to nothing else: calling several tools
    at once does not shrink what any one of them may return.
    """

    def __init__(self, profile: dict[str, Any] | None = None) -> None:
        self.batch_bytes = None
        self.source = "disabled"

    def perResultLimit(self, result_count: int) -> int | None:
        return None

    def createFinalizer(
        self,
        *,
        config: dict[str, Any],
        sessionId: str,
        assistantMessageIndex: int,
        resultCount: int,
        emit: Callable[..., Any],
    ) -> tuple[Callable[..., Any], int | None]:
        async def finish(
            tool_use: dict[str, Any],
            outcome: dict[str, Any],
            *,
            tool_name: str | None = None,
            rejected: bool = False,
            side_effects_may_have_occurred: bool = False,
            max_result_chars: int | None = None,
        ) -> dict[str, Any]:
            completed = await finalize_tool_dispatch_result(
                config=config,
                sessionId=sessionId,
                assistantMessageIndex=assistantMessageIndex,
                toolUse=tool_use,
                toolName=tool_name,
                outcome=outcome,
                sideEffectsMayHaveOccurred=side_effects_may_have_occurred,
                maxChars=max_result_chars,
                rejected=rejected,
                emit=emit,
            )
            block = completed["blocks"][0]
            if len(completed["blocks"]) > 1:
                block["_additionalBlocks"] = completed["blocks"][1:]
            if completed["result"].get("pause") == "approval":
                block["_pauseMessage"] = (
                    completed["result"].get("content") or ""
                ).strip() or "Approval required before continuing."
            return block

        return finish, None

    def record(
        self,
        blocks: list[dict[str, Any] | None],
        tool_uses: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any] | None:
        return None
