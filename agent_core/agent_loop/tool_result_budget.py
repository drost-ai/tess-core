from __future__ import annotations

from typing import Any, Callable

from ..agent_tool_results import createToolResultFinalizer
from ..provider_token_budget import resolve_interactive_context_policy
from ..tool_result_guard import (
    MIN_EFFECTIVE_TOOL_RESULT_BYTES,
    serializedContentBytes,
)


class ToolResultTurnBudget:
    def __init__(self, profile: dict[str, Any]) -> None:
        policy = resolve_interactive_context_policy(profile)
        self.batch_bytes = policy.get("inlineToolResultBatchBytes")
        self.remaining_bytes = policy.get("inlineToolResultTurnBytes")
        self.source = policy.get("toolBudgetSource")

    def perResultLimit(self, result_count: int) -> int | None:
        if self.batch_bytes is None or self.remaining_bytes is None:
            return None
        available = min(int(self.batch_bytes), int(self.remaining_bytes))
        return max(
            MIN_EFFECTIVE_TOOL_RESULT_BYTES,
            available // max(1, int(result_count)),
        )

    def createFinalizer(
        self,
        *,
        config: dict[str, Any],
        sessionId: str,
        assistantMessageIndex: int,
        resultCount: int,
        emit: Callable[..., Any],
    ) -> tuple[Callable[..., Any], int | None]:
        base = createToolResultFinalizer(
            config=config,
            sessionId=sessionId,
            assistantMessageIndex=assistantMessageIndex,
            emit=emit,
        )
        effective_limit = self.perResultLimit(resultCount)

        async def finish(
            tool_use: dict[str, Any],
            outcome: dict[str, Any],
            **kwargs: Any,
        ) -> dict[str, Any]:
            return await base(
                tool_use,
                outcome,
                max_bytes=effective_limit,
                require_artifact=effective_limit is not None,
                **kwargs,
            )

        return finish, effective_limit

    def record(self, blocks: list[dict[str, Any] | None]) -> dict[str, Any] | None:
        if self.remaining_bytes is None:
            return None
        used = sum(
            serializedContentBytes(str(block.get("content") or ""))
            for block in blocks
            if isinstance(block, dict) and block.get("type") == "tool_result"
        )
        before = int(self.remaining_bytes)
        self.remaining_bytes = max(0, before - used)
        return {
            "batch_budget_bytes": min(int(self.batch_bytes or 0), before),
            "batch_used_bytes": used,
            "turn_remaining_bytes": self.remaining_bytes,
            "source": self.source,
        }
