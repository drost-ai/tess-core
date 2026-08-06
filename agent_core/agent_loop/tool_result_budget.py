from __future__ import annotations

from typing import Any, Callable

from ..payloads.agent_results import createToolResultFinalizer
from ..providers.token_budget import resolve_interactive_context_policy
from ..payloads.result_guard import (
    MIN_EFFECTIVE_TOOL_RESULT_BYTES,
    NON_ARTIFACT_TOOL_RESULT_MAX_BYTES,
    serializedContentBytes,
)


class ToolResultBatchBudget:
    def __init__(self, profile: dict[str, Any]) -> None:
        policy = resolve_interactive_context_policy(profile)
        self.batch_bytes = policy.get("inlineToolResultBatchBytes")
        self.source = policy.get("toolBudgetSource")

    def perResultLimit(self, result_count: int) -> int | None:
        if self.batch_bytes is None:
            return None
        return max(
            MIN_EFFECTIVE_TOOL_RESULT_BYTES,
            int(self.batch_bytes) // max(1, int(result_count)),
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
            artifact_page = tool_use.get("name") == "ReadToolArtifact"
            return await base(
                tool_use,
                outcome,
                max_bytes=(
                    NON_ARTIFACT_TOOL_RESULT_MAX_BYTES
                    if artifact_page
                    else effective_limit
                ),
                require_artifact=effective_limit is not None and not artifact_page,
                never_artifact=artifact_page,
                **kwargs,
            )

        return finish, effective_limit

    def record(
        self,
        blocks: list[dict[str, Any] | None],
        tool_uses: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any] | None:
        if self.batch_bytes is None:
            return None
        artifact_page_ids = {
            str(tool_use.get("id") or "")
            for tool_use in (tool_uses or [])
            if tool_use.get("name") == "ReadToolArtifact"
        }
        used = sum(
            serializedContentBytes(str(block.get("content") or ""))
            for block in blocks
            if (
                isinstance(block, dict)
                and block.get("type") == "tool_result"
                and str(block.get("tool_use_id") or "") not in artifact_page_ids
            )
        )
        reserved_used = sum(
            serializedContentBytes(str(block.get("content") or ""))
            for block in blocks
            if (
                isinstance(block, dict)
                and block.get("type") == "tool_result"
                and str(block.get("tool_use_id") or "") in artifact_page_ids
            )
        )
        return {
            "batch_budget_bytes": int(self.batch_bytes),
            "batch_used_bytes": used,
            "artifact_page_reserved_bytes": reserved_used,
            "source": self.source,
        }


# Compatibility import for consumers that referenced the Package 38 class.
# The alias has batch-local, stateless semantics.
ToolResultTurnBudget = ToolResultBatchBudget
