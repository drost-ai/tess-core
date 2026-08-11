from __future__ import annotations

import inspect
from typing import Any, Callable

from .result_guard import bounded_tool_identity, guard_tool_call_result
from .attachments import (
    image_attachment_eligibility,
    image_attachment_to_block,
    resolve_runtime_provider_capabilities,
)
from .utf8_bounds import slice_utf8_prefix


COMMS_INLINE_TOOLS = {"SendMessage", "Agent"}


def _comms_inline_budget(tool_name: str, requested: Any) -> dict[str, Any]:
    """Inter-agent results are conversation: give them the comms inline
    allowance instead of the raw-output class default."""
    if requested is not None or tool_name not in COMMS_INLINE_TOOLS:
        return {"maxBytes": requested} if requested is not None else {}
    # Call-time import: the config package imports payloads at module load.
    from ..config._knobs import knobValue

    # skipStructuredShaping: inter-agent replies are JSON envelopes around
    # conversational text — the item summarizer must not re-truncate them
    # under the byte allowance.
    return {"maxBytes": int(knobValue("comms.inline_result_bytes")), "allowRaise": True, "skipStructuredShaping": True}


async def finalize_tool_dispatch_result(args: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
    values = {**(args or {}), **kwargs}
    tool_use = values["toolUse"]
    tool_name = bounded_tool_identity(values.get("toolName") or tool_use["name"])
    guarded = await guard_tool_call_result(
        config=values["config"],
        sessionId=values["sessionId"],
        toolName=tool_name,
        outcome=values["outcome"],
        sideEffectsMayHaveOccurred=values.get("sideEffectsMayHaveOccurred"),
        **_comms_inline_budget(tool_name, values.get("maxBytes")),
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
    capabilities = resolve_runtime_provider_capabilities(values["config"])
    for attachment in guarded["result"].get("attachments") or []:
        if not isinstance(attachment, dict) or attachment.get("type") != "image":
            continue
        try:
            eligibility = image_attachment_eligibility(
                capabilities=capabilities,
                mimeType=attachment["mimeType"],
                byteLength=attachment["byteLength"],
            )
            image_block = image_attachment_to_block(attachment) if eligibility["eligible"] else None
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
                        "file_path": slice_utf8_prefix(str(attachment.get("filePath") or ""), 1_024),
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
                    "file_path": slice_utf8_prefix(str(attachment.get("filePath") or ""), 1_024),
                    "mime_type": attachment.get("mimeType"),
                    "bytes": attachment.get("byteLength"),
                    "sha256": attachment.get("sha256"),
                },
            )
    return {**guarded, "blocks": blocks}


def _cardinality_fields(cardinality: dict[str, Any] | None) -> dict[str, Any]:
    if not cardinality:
        return {}
    return {
        "original_item_count": cardinality["originalItemCount"],
        "returned_item_count": cardinality["returnedItemCount"],
        "omitted_item_count": cardinality["omittedItemCount"],
        "items_truncated": cardinality["itemsTruncated"],
    }


