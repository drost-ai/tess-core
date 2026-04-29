from __future__ import annotations

from .shared import *


def _normalize_tool_call_result(server: str, tool_name: str, value: Any) -> dict[str, Any]:
    result = value if isinstance(value, dict) else {}
    blocks = result.get("content") if isinstance(result.get("content"), list) else None
    text_parts = []
    for block in blocks or []:
        if isinstance(block, dict) and isinstance(block.get("text"), str):
            text_parts.append(block["text"])
        elif isinstance(block, dict) and isinstance(block.get("content"), str):
            text_parts.append(block["content"])
        else:
            text_parts.append(pretty_json(block))
    if text_parts:
        text = "\n".join(text_parts)
    elif "structuredContent" in result:
        text = pretty_json(result["structuredContent"])
    elif isinstance(value, str):
        text = value
    else:
        text = pretty_json(value)
    return {"content": text, "isError": bool(result.get("isError")), "meta": {"mcp_server": server, "mcp_tool": tool_name}}


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
