from __future__ import annotations

import json
from typing import Any, Iterable

from .payloads.cursor import encodePayloadCursor, fingerprintPayloadSource, resolvePayloadCursor
from .payloads.structured_value import summarizeStructuredValue
from .payloads.tools import _persist_artifact, getToolPayloadPolicy
from .payloads.utf8_bounds import sliceUtf8Prefix, sliceUtf8Window, utf8ByteLength


DEFAULT_INLINE_BYTES = 8_000
MAX_MCP_RESOURCE_PAGE = 40
MAX_MCP_CONTENTS = 12


async def shapeMcpResourceList(**args: Any) -> dict[str, Any]:
    inline_bytes = _inline_budget(args.get("context"))
    limit = min(_limit(args.get("requestedLimit"), MAX_MCP_RESOURCE_PAGE), MAX_MCP_RESOURCE_PAGE)
    resources = sorted(args.get("resources", []), key=_resource_sort_key)
    fingerprint = fingerprintPayloadSource(
        _resource_list_cursor_parts(resources, args.get("server"))
    )
    cursor = resolvePayloadCursor(
        family="mcp_resource_list",
        sourceFingerprint=fingerprint,
        cursor=args.get("cursor"),
        legacyOffset=_offset(args.get("offset")) if "offset" in args else None,
        legacyBase=0,
    )
    if any(
        cursor.get(key) is not None
        for key in ("byteOffset", "sourceByteOffset", "recordBytes", "snapshotFingerprint")
    ):
        raise ValueError("MCP resource-list cursors must point to a resource boundary.")
    offset = cursor["recordOffset"]
    candidates = resources[offset : offset + limit]
    page: list[Any] = []
    for candidate in candidates:
        summary = summarizeStructuredValue(
            candidate,
            {
                "maxDepth": 2,
                "maxKeys": 12,
                "maxItems": 16,
                "maxStringBytes": 600,
                "maxBytes": 1_200,
            },
        )
        next_page = [*page, summary["value"]]
        if utf8ByteLength(json.dumps(next_page, ensure_ascii=False, separators=(",", ":"))) > max(
            1_000, inline_bytes - 2_000
        ):
            break
        page.append(summary["value"])
    skipped = 1 if not page and candidates else 0
    advanced = len(page) + skipped
    next_offset = offset + advanced if offset + advanced < len(resources) else None
    next_cursor = (
        encodePayloadCursor(
            family="mcp_resource_list",
            sourceFingerprint=fingerprint,
            position={"recordOffset": next_offset},
        )
        if next_offset is not None
        else None
    )
    truncated = next_offset is not None or skipped > 0
    artifact = (
        await _persist_raw_mcp_artifact(
            context=args.get("context"), tool_name="ListMcpResources", value=resources
        )
        if truncated
        else None
    )
    return _without_none(
        {
            "type": "mcp_resource_list",
            "server": args.get("server"),
            "totalResources": len(resources),
            "offset": offset,
            "limit": limit,
            "returnedResources": len(page),
            "advancedResources": advanced,
            "skippedResources": skipped or None,
            "skipReason": (
                "One resource summary exceeded the inline page budget and was explicitly skipped."
                if skipped
                else None
            ),
            "resources": page,
            "truncated": truncated,
            "nextCursor": next_cursor,
            "nextOffset": next_offset,
            "artifact": artifact,
            "artifactUnavailable": True if truncated and not artifact else None,
            "recovery": (
                {
                    "type": "tool_artifact",
                    "artifactId": artifact["artifactId"],
                    "filePath": artifact["filePath"],
                }
                if artifact
                else {"type": "mcp_resource_list", "server": args.get("server"), "nextOffset": next_offset}
                if truncated
                else None
            ),
        }
    )


async def shapeMcpResourceRead(**args: Any) -> dict[str, Any]:
    inline_bytes = _inline_budget(args.get("context"))
    result = args.get("value") if isinstance(args.get("value"), dict) else {}
    contents = result.get("contents") if isinstance(result.get("contents"), list) else []
    fingerprint = fingerprintPayloadSource(
        _resource_read_cursor_parts(args["server"], args["uri"], contents)
    )
    cursor = resolvePayloadCursor(
        family="mcp_resource_read",
        sourceFingerprint=fingerprint,
        cursor=args.get("cursor"),
        legacyOffset=_offset(args.get("offset")) if "offset" in args else None,
        legacyBase=0,
    )
    if any(
        cursor.get(key) is not None
        for key in ("sourceByteOffset", "recordBytes", "snapshotFingerprint")
    ):
        raise ValueError("MCP resource cursors use content-record and text-byte offsets only.")
    offset = cursor["recordOffset"]
    limit = min(_limit(args.get("requestedLimit"), MAX_MCP_CONTENTS), MAX_MCP_CONTENTS)
    remaining = max(1_000, int(inline_bytes * 0.5))
    text_truncated = False
    index = min(offset, len(contents))
    next_position = None
    shaped: list[dict[str, Any]] = []
    while index < len(contents) and len(shaped) < limit:
        content = contents[index] if isinstance(contents[index], dict) else {}
        text_start = cursor.get("byteOffset", 0) if index == offset else 0
        remaining_slots = max(1, min(limit - len(shaped), len(contents) - index))
        fair_budget = max(1, remaining // remaining_slots)
        text_budget = min(remaining, max(256, fair_budget))
        text = (
            sliceUtf8Window(content["text"], text_start, text_budget)
            if isinstance(content.get("text"), str)
            else None
        )
        if text:
            remaining = max(0, remaining - text["returnedBytes"])
        extra_source = {
            key: value
            for key, value in content.items()
            if key not in ("text", "blob", "data")
        }
        extra = summarizeStructuredValue(
            extra_source,
            {
                "maxDepth": 3,
                "maxKeys": 16,
                "maxItems": 24,
                "maxStringBytes": 500,
                "maxBytes": 1_000,
            },
        )["value"]
        text_has_more = bool(text and text.get("nextByteOffset") is not None)
        text_windowed = bool(
            text
            and (
                text["startByteOffset"] > 0
                or text_has_more
                or text["skippedBytes"] > 0
            )
        )
        text_truncated = text_truncated or text_windowed
        shaped.append(
            _without_none(
                {
                    "index": index,
                    **(extra if isinstance(extra, dict) else {}),
                    "text": text["value"] if text else None,
                    "textBytes": text["sourceBytes"] if text else None,
                    "textByteOffset": text["startByteOffset"]
                    if text and text["startByteOffset"]
                    else None,
                    "textReturnedBytes": text["returnedBytes"] if text else None,
                    "textSkippedBytes": text["skippedBytes"]
                    if text and text["skippedBytes"]
                    else None,
                    "textTruncated": text_windowed or None,
                    "binaryPayloadInlined": False,
                }
            )
        )
        if text_has_more:
            next_position = {
                "recordOffset": index,
                "byteOffset": text["nextByteOffset"],
            }
            break
        index += 1
    if next_position is None and index < len(contents):
        next_position = {"recordOffset": index}
    next_cursor = (
        encodePayloadCursor(
            family="mcp_resource_read",
            sourceFingerprint=fingerprint,
            position=next_position,
        )
        if next_position
        else None
    )
    next_offset = (
        next_position["recordOffset"] + (1 if next_position.get("byteOffset") else 0)
        if next_position
        else None
    )
    acquisition_truncated = bool(args.get("acquisitionTruncated"))
    truncated = bool(
        acquisition_truncated
        or text_truncated
        or offset > 0
        or next_position
        or len(shaped) < len(contents)
    )
    artifact = (
        await _persist_raw_mcp_artifact(
            context=args.get("context"), tool_name="ReadMcpResource", value=args.get("value")
        )
        if truncated
        else None
    )
    return _without_none(
        {
            "type": "mcp_resource",
            "server": sliceUtf8Prefix(args["server"], 256),
            "uri": sliceUtf8Prefix(args["uri"], 2_000),
            "totalContents": len(contents),
            "offset": offset,
            "limit": limit,
            "returnedContents": len(shaped),
            "contents": shaped,
            "truncated": truncated,
            "nextCursor": next_cursor,
            "nextOffset": next_offset,
            "cursorCompatibility": (
                {
                    "legacyNextOffsetSkipsRemainder": True,
                    "note": "Use nextCursor to continue this content record byte-for-byte; nextOffset advances to the following content record.",
                }
                if next_position and next_position.get("byteOffset")
                else None
            ),
            "acquisitionTruncated": acquisition_truncated or None,
            "artifact": artifact,
            "artifactUnavailable": True if truncated and not artifact else None,
            "artifactContainsPartialResponse": True
            if acquisition_truncated and artifact
            else None,
            "recovery": (
                {
                    "type": "tool_artifact",
                    "artifactId": artifact["artifactId"],
                    "filePath": artifact["filePath"],
                }
                if artifact
                else {"type": "mcp_resource", "server": args["server"], "uri": args["uri"]}
            ),
        }
    )


async def _persist_raw_mcp_artifact(
    *, context: dict[str, Any] | None, tool_name: str, value: Any
) -> dict[str, Any] | None:
    if not context:
        return None
    try:
        content = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2)
        return await _persist_artifact(
            {
                "sessionId": context["sessionId"],
                "toolName": tool_name,
                "family": "generic",
                "content": content,
                "contentType": "text" if isinstance(value, str) else "json",
            }
        )
    except Exception:
        return None


def _resource_list_cursor_parts(resources: list[Any], server: str | None) -> Iterable[str]:
    yield "mcp_resource_list"
    yield server or ""
    for value in resources:
        record = value if isinstance(value, dict) else {}
        yield str(record.get("server", ""))
        yield str(record.get("uri", ""))
        yield str(record.get("name", ""))


def _resource_read_cursor_parts(server: str, uri: str, contents: list[Any]) -> Iterable[str]:
    yield "mcp_resource_read"
    yield server
    yield uri
    yield str(len(contents))
    for value in contents:
        record = value if isinstance(value, dict) else {}
        yield str(record.get("uri", ""))
        yield str(record.get("mimeType", ""))
        yield record.get("text", "") if isinstance(record.get("text"), str) else ""


def _resource_sort_key(value: Any) -> str:
    record = value if isinstance(value, dict) else {}
    return "\0".join(str(record.get(key, "")) for key in ("server", "uri", "name"))


def _inline_budget(context: dict[str, Any] | None) -> int:
    return getToolPayloadPolicy(context["config"])["textInlineBytes"] if context else DEFAULT_INLINE_BYTES


def _offset(value: Any) -> int:
    return max(0, int(value)) if isinstance(value, (int, float)) else 0


def _limit(value: Any, fallback: int) -> int:
    return int(value) if isinstance(value, (int, float)) and value > 0 else fallback


def _without_none(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _without_none(item) for key, item in value.items() if item is not None}
    if isinstance(value, list):
        return [_without_none(item) for item in value]
    return value


shape_mcp_resource_list = shapeMcpResourceList
shape_mcp_resource_read = shapeMcpResourceRead
