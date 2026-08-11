from __future__ import annotations

from typing import Any, Callable

from ..payloads.agent_results import finalizeToolDispatchResult
from ..providers.token_budget import resolve_interactive_context_policy
from ..payloads.result_guard import (
    MIN_EFFECTIVE_TOOL_RESULT_BYTES,
    non_artifact_tool_result_max_bytes,
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
        effective_limit = self.perResultLimit(resultCount)

        async def finish(
            tool_use: dict[str, Any],
            outcome: dict[str, Any],
            *,
            tool_name: str | None = None,
            rejected: bool = False,
            side_effects_may_have_occurred: bool = False,
        ) -> dict[str, Any]:
            artifact_page = tool_use.get("name") == "ReadToolArtifact"
            completed = await finalizeToolDispatchResult(
                config=config,
                sessionId=sessionId,
                assistantMessageIndex=assistantMessageIndex,
                toolUse=tool_use,
                toolName=tool_name,
                outcome=outcome,
                sideEffectsMayHaveOccurred=side_effects_may_have_occurred,
                maxBytes=(
                    non_artifact_tool_result_max_bytes()
                    if artifact_page
                    else effective_limit
                ),
                requireArtifact=effective_limit is not None and not artifact_page,
                neverArtifact=artifact_page,
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
