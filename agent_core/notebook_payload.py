from __future__ import annotations

from typing import Any

from .payload_cursor import encodePayloadCursor
from .structured_value import summarizeStructuredValue
from .tool_payloads import _persist_artifact, getToolPayloadPolicy
from .utf8_bounds import sliceUtf8Prefix, utf8ByteLength


MAX_NOTEBOOK_CELLS_PER_PAGE = 20
MAX_CELL_SOURCE_BYTES = 2_000
MAX_OUTPUT_SAMPLE_BYTES = 600
MAX_OUTPUTS_PER_CELL = 12


async def shapeNotebookPayload(**args: Any) -> dict[str, Any]:
    notebook = args.get("notebook")
    if not isinstance(notebook, dict) or not isinstance(notebook.get("cells"), list):
        raise ValueError("Notebook JSON must contain a cells array.")
    policy = getToolPayloadPolicy(args["config"])
    offset = _offset(args.get("offset"))
    requested_limit = _limit(args.get("requestedLimit"), MAX_NOTEBOOK_CELLS_PER_PAGE)
    limit = min(requested_limit, MAX_NOTEBOOK_CELLS_PER_PAGE)
    cells = notebook["cells"][offset : offset + limit]
    recovery_text: list[str] = []
    remaining = max(1_000, policy["readInlineBytes"] - 3_000)
    page: list[dict[str, Any]] = []
    for relative_index, raw_cell in enumerate(cells):
        cell = raw_cell if isinstance(raw_cell, dict) else {}
        source = _summarize_text_sequence(
            cell.get("source"), min(MAX_CELL_SOURCE_BYTES, remaining)
        )
        remaining = max(0, remaining - source["returnedBytes"])
        raw_outputs = cell.get("outputs") if isinstance(cell.get("outputs"), list) else []
        outputs = [
            _summarize_output(
                output,
                offset + relative_index,
                output_index,
                recovery_text,
            )
            for output_index, output in enumerate(raw_outputs[:MAX_OUTPUTS_PER_CELL])
        ]
        page.append(
            _without_none(
                {
                    "index": offset + relative_index,
                    "cellType": cell.get("cell_type")
                    if isinstance(cell.get("cell_type"), str)
                    else "unknown",
                    "executionCount": cell.get("execution_count")
                    if isinstance(cell.get("execution_count"), (int, float))
                    or cell.get("execution_count") is None
                    else None,
                    "source": source["text"],
                    "sourceBytes": source["originalBytes"],
                    "sourceTruncated": source["truncated"] or None,
                    "outputCount": len(raw_outputs),
                    "outputs": outputs,
                    "outputsOmitted": len(raw_outputs) - len(outputs)
                    if len(raw_outputs) > len(outputs)
                    else None,
                }
            )
        )
    recovery = "\n\n".join(recovery_text)
    output_artifact = None
    output_artifact_unavailable = False
    if utf8ByteLength(recovery) > policy["artifactThresholdBytes"]:
        try:
            output_artifact = await _persist_artifact(
                {
                    "sessionId": args["sessionId"],
                    "toolName": "Read",
                    "family": "read",
                    "content": recovery,
                    "contentType": "text",
                    "itemCount": len(recovery_text),
                }
            )
        except Exception:
            output_artifact_unavailable = True
    next_offset = offset + len(page) if offset + len(page) < len(notebook["cells"]) else None
    next_cursor = (
        encodePayloadCursor(
            family="read_notebook",
            sourceFingerprint=args["sourceFingerprint"],
            position={"recordOffset": next_offset},
        )
        if next_offset is not None
        else None
    )
    metadata = summarizeStructuredValue(
        notebook.get("metadata"),
        {
            "maxDepth": 3,
            "maxItems": 20,
            "maxKeys": 20,
            "maxStringBytes": 300,
            "maxBytes": 1_500,
        },
    )
    changed = bool(args.get("changedDuringRead"))
    return _without_none(
        {
            "type": "notebook",
            "file": {
                "filePath": args["filePath"],
                "sourceBytes": args["sourceBytes"],
                "nbformat": _finite_number(notebook.get("nbformat")),
                "nbformatMinor": _finite_number(notebook.get("nbformat_minor")),
                "metadata": metadata["value"],
                "metadataTruncated": metadata["truncated"] or None,
                "totalCells": len(notebook["cells"]),
                "offset": offset,
                "limit": limit,
                "requestedLimit": requested_limit,
                "returnedCells": len(page),
                "cells": page,
                "truncated": next_offset is not None,
                "nextCursor": next_cursor,
                "nextOffset": next_offset,
                "changedDuringRead": changed or None,
                "consistencyNote": (
                    "The notebook changed while it was read; this page came from the opened file snapshot."
                    if changed
                    else None
                ),
                "outputArtifact": output_artifact,
                "outputArtifactUnavailable": output_artifact_unavailable or None,
                "recovery": {
                    "type": "source_file",
                    "filePath": args["filePath"],
                    "note": "The notebook file is the durable source for omitted cells, source, and outputs.",
                },
            },
        }
    )


def _summarize_output(
    value: Any,
    cell_index: int,
    output_index: int,
    recovery_text: list[str],
) -> dict[str, Any]:
    output = value if isinstance(value, dict) else {}
    output_type = output.get("output_type") if isinstance(output.get("output_type"), str) else "unknown"
    text = _summarize_text_sequence(output.get("text"), MAX_OUTPUT_SAMPLE_BYTES)
    if text["originalBytes"]:
        recovery_text.append(
            f"Cell {cell_index}, output {output_index} ({output_type})\n{text['fullText']}"
        )
    data = output.get("data") if isinstance(output.get("data"), dict) else {}
    mime_summaries: list[dict[str, Any]] = []
    for mime_type in sorted(data):
        payload = data[mime_type]
        if mime_type.startswith("image/") or mime_type.startswith("application/"):
            mime_summaries.append(
                {
                    "mimeType": mime_type,
                    "encodedBytes": utf8ByteLength(_text_sequence(payload)),
                    "inlined": False,
                }
            )
            continue
        summary = _summarize_text_sequence(payload, MAX_OUTPUT_SAMPLE_BYTES)
        if summary["originalBytes"]:
            recovery_text.append(
                f"Cell {cell_index}, output {output_index} ({mime_type})\n{summary['fullText']}"
            )
        mime_summaries.append(
            _without_none(
                {
                    "mimeType": mime_type,
                    "text": summary["text"],
                    "bytes": summary["originalBytes"],
                    "truncated": summary["truncated"] or None,
                }
            )
        )
    traceback = _summarize_text_sequence(output.get("traceback"), MAX_OUTPUT_SAMPLE_BYTES)
    if traceback["originalBytes"]:
        recovery_text.append(
            f"Cell {cell_index}, output {output_index} (traceback)\n{traceback['fullText']}"
        )
    error = None
    if isinstance(output.get("ename"), str) or isinstance(output.get("evalue"), str):
        error = _without_none(
            {
                "name": sliceUtf8Prefix(output.get("ename", ""), 120) or None,
                "value": sliceUtf8Prefix(output.get("evalue", ""), 400) or None,
                "traceback": traceback["text"] or None,
                "tracebackBytes": traceback["originalBytes"] or None,
                "tracebackTruncated": traceback["truncated"] or None,
            }
        )
    return _without_none(
        {
            "outputType": output_type,
            "name": sliceUtf8Prefix(output.get("name", ""), 120)
            if isinstance(output.get("name"), str)
            else None,
            "text": text["text"] or None,
            "textBytes": text["originalBytes"] or None,
            "textTruncated": text["truncated"] or None,
            "mimeTypes": mime_summaries,
            "error": error,
        }
    )


def _summarize_text_sequence(value: Any, max_bytes: int) -> dict[str, Any]:
    full = _text_sequence(value)
    fitted = sliceUtf8Prefix(full, max(0, max_bytes))
    return {
        "text": fitted,
        "fullText": full,
        "originalBytes": utf8ByteLength(full),
        "returnedBytes": utf8ByteLength(fitted),
        "truncated": fitted != full,
    }


def _text_sequence(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(item for item in value if isinstance(item, str))
    return ""


def _offset(value: Any) -> int:
    return max(0, int(value)) if isinstance(value, (int, float)) else 0


def _limit(value: Any, fallback: int) -> int:
    return int(value) if isinstance(value, (int, float)) and value > 0 else fallback


def _finite_number(value: Any) -> int | float | None:
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _without_none(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _without_none(item) for key, item in value.items() if item is not None}
    if isinstance(value, list):
        return [_without_none(item) for item in value]
    return value


shape_notebook_payload = shapeNotebookPayload
