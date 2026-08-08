from __future__ import annotations

import base64
import re
from typing import Any

from .._utils import pretty_json
from ..payloads.attachments import createImageToolResultAttachment
from .blobs import _save_blob_content
from .protocol import _without_none_values

MCP_RESULT_ATTACHMENT_MAX = 8


async def _normalize_tool_call_result(server: str, tool_name: str, value: Any) -> dict[str, Any]:
    result = value if isinstance(value, dict) else {}
    blocks = result.get("content") if isinstance(result.get("content"), list) else None
    text_parts = []
    attachments: list[dict[str, Any]] = []
    for index, block in enumerate(blocks or []):
        if isinstance(block, dict) and isinstance(block.get("text"), str):
            text_parts.append(block["text"])
        elif isinstance(block, dict) and isinstance(block.get("content"), str):
            text_parts.append(block["content"])
        elif (
            isinstance(block, dict)
            and block.get("type") in ("image", "audio")
            and isinstance(block.get("data"), str)
        ):
            text_parts.append(
                await _spill_binary_block(server, tool_name, index, block, attachments)
            )
        elif isinstance(block, dict) and isinstance(block.get("resource"), dict):
            text_parts.append(await _embedded_resource_part(server, block["resource"]))
        else:
            text_parts.append(pretty_json(block))
    structured = _bounded_structured_content(result.get("structuredContent")) if "structuredContent" in result else None
    if text_parts and structured is not None:
        text = pretty_json({"type": "mcp_tool_result", "text": "\n".join(text_parts), "structured": structured})
    elif text_parts:
        text = "\n".join(text_parts)
    elif "structuredContent" in result:
        text = pretty_json({"type": "mcp_tool_result", "structured": structured})
    elif isinstance(value, str):
        text = value
    else:
        text = pretty_json(value)
    return {
        "content": text,
        "isError": bool(result.get("isError")),
        **({"structuredContent": structured} if "structuredContent" in result else {}),
        **({"attachments": attachments} if attachments else {}),
        "meta": {"mcp_server": server[:256], "mcp_tool": tool_name[:256]},
    }


async def _spill_binary_block(
    server: str,
    tool_name: str,
    index: int,
    block: dict[str, Any],
    attachments: list[dict[str, Any]],
) -> str:
    kind = str(block.get("type"))
    mime = block.get("mimeType") if isinstance(block.get("mimeType"), str) else "application/octet-stream"
    try:
        raw = base64.b64decode(block["data"], validate=True)
    except Exception:
        return pretty_json({"type": kind, "mimeType": mime, "error": "invalid base64 data"})
    subtype = re.sub(r"[^A-Za-z0-9]+", "", mime.split("/")[-1])[:8] or "bin"
    saved = await _save_blob_content(server, f"{tool_name}-{kind}-{index}.{subtype}", block["data"])
    attached = False
    if kind == "image" and len(attachments) < MCP_RESULT_ATTACHMENT_MAX:
        attachments.append(
            createImageToolResultAttachment(filePath=saved, mimeType=mime, buffer=raw)
        )
        attached = True
    return pretty_json(
        {"type": kind, "mimeType": mime, "bytes": len(raw), "savedTo": saved, "attached": attached}
    )


async def _embedded_resource_part(server: str, resource: dict[str, Any]) -> str:
    if isinstance(resource.get("text"), str):
        return resource["text"]
    uri = resource.get("uri") if isinstance(resource.get("uri"), str) else "embedded"
    mime = resource.get("mimeType") if isinstance(resource.get("mimeType"), str) else None
    blob = resource.get("blob") if isinstance(resource.get("blob"), str) else None
    if blob:
        try:
            saved = await _save_blob_content(server, uri, blob)
        except Exception:
            return pretty_json(resource)
        return pretty_json(
            _without_none_values({"type": "resource", "uri": uri, "mimeType": mime, "savedTo": saved})
        )
    return pretty_json(resource)


def _bounded_structured_content(value: Any, *, depth: int = 0, _budget: list[int] | None = None) -> Any:
    budget = _budget if _budget is not None else [32_000]
    if budget[0] <= 0:
        return "[truncated]"
    if value is None or isinstance(value, (bool, int, float)):
        budget[0] -= 16
        return value
    if isinstance(value, str):
        encoded = value.encode("utf-8")[: min(1500, max(0, budget[0]))]
        budget[0] -= len(encoded)
        return encoded.decode("utf-8", errors="ignore")
    if depth >= 5:
        budget[0] -= 16
        return "[bounded]"
    if isinstance(value, list):
        out = []
        for item in value[:80]:
            if budget[0] <= 0:
                out.append("[truncated]")
                break
            out.append(_bounded_structured_content(item, depth=depth + 1, _budget=budget))
        return out
    if isinstance(value, dict):
        out = {}
        for key, item in list(value.items())[:40]:
            if budget[0] <= 0:
                out["__truncated__"] = True
                break
            bounded_key = str(key).encode("utf-8")[:256].decode("utf-8", errors="ignore")
            budget[0] -= len(bounded_key.encode("utf-8"))
            out[bounded_key] = _bounded_structured_content(item, depth=depth + 1, _budget=budget)
        return out
    text = str(value).encode("utf-8")[: min(1500, max(0, budget[0]))]
    budget[0] -= len(text)
    return text.decode("utf-8", errors="ignore")


async def _normalize_read_resource_output(server: str, value: Any) -> dict[str, Any]:
    result = value if isinstance(value, dict) else {}
    contents = result.get("contents") if isinstance(result.get("contents"), list) else []
    out = []
    for index, entry in enumerate(contents):
        content = entry if isinstance(entry, dict) else {}
        uri = content.get("uri") if isinstance(content.get("uri"), str) else f"{server}#{index}"
        mime_type = content.get("mimeType") if isinstance(content.get("mimeType"), str) else None
        if isinstance(content.get("text"), str):
            out.append(_without_none_values({"uri": uri, "mimeType": mime_type, "text": content["text"]}))
            continue
        blob = content.get("blob") if isinstance(content.get("blob"), str) else content.get("data") if isinstance(content.get("data"), str) else None
        if blob:
            out.append(_without_none_values({"uri": uri, "mimeType": mime_type, "blobSavedTo": await _save_blob_content(server, uri, blob)}))
        else:
            out.append(_without_none_values({"uri": uri, "mimeType": mime_type, "text": pretty_json(content)}))
    return {"contents": out}
