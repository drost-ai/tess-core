from __future__ import annotations

import inspect
from typing import Any, Callable

from .result_guard import boundedToolIdentity, guardToolCallResult
from .attachments import (
    imageAttachmentEligibility,
    imageAttachmentToBlock,
    resolveRuntimeProviderCapabilities,
)
from .utf8_bounds import sliceUtf8Prefix


async def finalizeToolDispatchResult(args: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
    values = {**(args or {}), **kwargs}
    tool_use = values["toolUse"]
    tool_name = boundedToolIdentity(values.get("toolName") or tool_use["name"])
    guarded = await guardToolCallResult(
        config=values["config"],
        sessionId=values["sessionId"],
        toolName=tool_name,
        outcome=values["outcome"],
        sideEffectsMayHaveOccurred=values.get("sideEffectsMayHaveOccurred"),
        **({"maxBytes": values["maxBytes"]} if values.get("maxBytes") is not None else {}),
        requireArtifact=bool(values.get("requireArtifact")),
        neverArtifact=bool(values.get("neverArtifact")),
        **({"artifactWriter": values["artifactWriter"]} if values.get("artifactWriter") else {}),
    )
    cardinality_fields = _cardinality_fields(guarded.get("cardinality"))
    record = values.get("record")
    if callable(record):
        entry = {
            "type": "tool_execution_completed",
            "assistant_message_index": values.get("assistantMessageIndex"),
            "tool_use_id": tool_use["id"],
            "tool_name": tool_name,
            "is_error": bool(guarded["result"].get("isError")),
            "content": guarded["result"]["content"],
            **cardinality_fields,
        }
        if values.get("rejected"):
            entry["rejected"] = True
        pending = record(entry)
        if inspect.isawaitable(pending):
            await pending
    emit: Callable[..., Any] | None = values.get("emit")
    if callable(emit):
        event_data = {
            "tool_use_id": tool_use["id"],
            "name": tool_name,
            "output": guarded["result"]["content"],
            "error": bool(guarded["result"].get("isError")),
            "session_id": values["sessionId"],
            **cardinality_fields,
        }
        if values.get("rejected"):
            event_data["rejected"] = True
        emit("tool.completed", event_data)
        for diagnostic in guarded["diagnostics"]:
            emit(
                "tool.result_guard",
                {
                    "session_id": values["sessionId"],
                    "tool_use_id": tool_use["id"],
                    "name": tool_name,
                    "kind": diagnostic["kind"],
                    "original_bytes": diagnostic["originalBytes"],
                    "final_bytes": diagnostic["finalBytes"],
                    "limit_bytes": diagnostic["limitBytes"],
                    **({"artifact_id": diagnostic["artifactId"]} if diagnostic.get("artifactId") else {}),
                    **cardinality_fields,
                },
            )
    block = {
        "type": "tool_result",
        "tool_use_id": tool_use["id"],
        "is_error": bool(guarded["result"].get("isError")),
        "content": guarded["result"]["content"],
    }
    blocks = [block]
    capabilities = resolveRuntimeProviderCapabilities(values["config"])
    for attachment in guarded["result"].get("attachments") or []:
        if not isinstance(attachment, dict) or attachment.get("type") != "image":
            continue
        try:
            eligibility = imageAttachmentEligibility(
                capabilities=capabilities,
                mimeType=attachment["mimeType"],
                byteLength=attachment["byteLength"],
            )
            image_block = imageAttachmentToBlock(attachment) if eligibility["eligible"] else None
            reason = "missing_attachment_data" if eligibility["eligible"] else eligibility["reason"]
        except Exception:
            image_block = None
            reason = "invalid_attachment_metadata"
        if not image_block:
            if callable(emit):
                emit(
                    "tool.attachment_dropped",
                    {
                        "session_id": values["sessionId"],
                        "tool_name": tool_name,
                        "tool_use_id": tool_use["id"],
                        "type": "image",
                        "file_path": sliceUtf8Prefix(str(attachment.get("filePath") or ""), 1_024),
                        "mime_type": attachment.get("mimeType"),
                        "bytes": attachment.get("byteLength"),
                        "reason": reason,
                    },
                )
            continue
        blocks.append(image_block)
        if callable(emit):
            emit(
                "tool.attachment_included",
                {
                    "session_id": values["sessionId"],
                    "tool_name": tool_name,
                    "tool_use_id": tool_use["id"],
                    "type": "image",
                    "file_path": sliceUtf8Prefix(str(attachment.get("filePath") or ""), 1_024),
                    "mime_type": attachment.get("mimeType"),
                    "bytes": attachment.get("byteLength"),
                    "sha256": attachment.get("sha256"),
                },
            )
    return {**guarded, "blocks": blocks}


def createToolResultFinalizer(args: dict[str, Any] | None = None, **kwargs: Any) -> Callable[..., Any]:
    values = {**(args or {}), **kwargs}

    async def finish(
        tool_use: dict[str, Any],
        outcome: dict[str, Any],
        *,
        tool_name: str | None = None,
        blocked_by_plan_contract: bool = False,
        rejected: bool = False,
        side_effects_may_have_occurred: bool = False,
        max_bytes: int | None = None,
        require_artifact: bool = False,
        never_artifact: bool = False,
    ) -> dict[str, Any]:
        completed = await finalizeToolDispatchResult(
            config=values["config"],
            sessionId=values["sessionId"],
            assistantMessageIndex=values.get("assistantMessageIndex"),
            toolUse=tool_use,
            toolName=tool_name,
            outcome=outcome,
            sideEffectsMayHaveOccurred=side_effects_may_have_occurred,
            maxBytes=max_bytes,
            requireArtifact=require_artifact,
            neverArtifact=never_artifact,
            rejected=rejected,
            emit=values.get("emit"),
        )
        block = completed["blocks"][0]
        if len(completed["blocks"]) > 1:
            block["_additionalBlocks"] = completed["blocks"][1:]
        if completed["result"].get("pause") == "approval":
            block["_pauseMessage"] = (
                completed["result"].get("content") or ""
            ).strip() or "Approval required before continuing."
        if blocked_by_plan_contract:
            block["_blockedByPlanContract"] = True
        return block

    return finish


def _cardinality_fields(cardinality: dict[str, Any] | None) -> dict[str, Any]:
    if not cardinality:
        return {}
    return {
        "original_item_count": cardinality["originalItemCount"],
        "returned_item_count": cardinality["returnedItemCount"],
        "omitted_item_count": cardinality["omittedItemCount"],
        "items_truncated": cardinality["itemsTruncated"],
    }


