from __future__ import annotations

import json
import re
from .parsing import (
    _json_loads_strict,
    empty_compaction_result,
    empty_relief_summary,
    omit_none,
)
from .shared import _tool_result_content_string
from .rendering import (
    normalize_reinjection_lines,
    render_partial_compaction_summary,
    render_region_summary_reinjection_lines,
    summarize_continuation_region,
)
from .shared import relief_knob
from .tokens import (
    as_record,
    collapse_whitespace,
    estimate_messages_tokens,
    truncate,
)
from typing import Any
from ...storage.message_origin import (
    is_plain_user_turn_anchor as _is_plain_user_turn_anchor,
    synthetic_user_message as _synthetic_user_message,
)
from ...session.tool_boundaries import adjust_start_to_tool_boundary as _adjust_start_to_tool_boundary


def choose_preserved_raw_start(messages: list[dict[str, Any]]) -> int:
    fallback_start = max(0, len(messages) - relief_knob("context.relief_preserve_min_messages"))
    last_plain_user_index = find_last_plain_user_index(messages)
    if last_plain_user_index is None or last_plain_user_index <= 0:
        return _adjust_start_to_tool_boundary(messages, fallback_start)
    tail_tokens = estimate_messages_tokens(messages[last_plain_user_index:])
    candidate = last_plain_user_index if tail_tokens <= relief_knob("context.relief_raw_tail_tokens") else fallback_start
    return _adjust_start_to_tool_boundary(messages, candidate)


def find_last_plain_user_index(messages: list[dict[str, Any]]) -> int | None:
    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        if _is_plain_user_turn_anchor(message):
            return index
    return None


def apply_compact_prefix(messages: list[dict[str, Any]], options: dict[str, Any]) -> dict[str, Any]:
    if len(messages) <= int(options["preserveMinMessages"]):
        return {"messages": messages, "summary": empty_relief_summary()}
    preserved_raw_start_index = choose_preserved_raw_start(messages)
    if preserved_raw_start_index <= 0:
        return {"messages": messages, "summary": empty_relief_summary()}
    compacted_prefix = compact_historical_tail(messages[:preserved_raw_start_index], options)
    next_messages = [*compacted_prefix, *messages[preserved_raw_start_index:]]
    tokens_saved_estimate = max(0, estimate_messages_tokens(messages) - estimate_messages_tokens(next_messages))
    dropped_message_count = preserved_raw_start_index - len(compacted_prefix)
    applied = tokens_saved_estimate > 0 or dropped_message_count > 0
    return {
        "messages": next_messages,
        "summary": {
            **empty_relief_summary(),
            "applied": applied,
            "selectedMode": "microcompact",
            "appliedModes": ["microcompact"] if applied else [],
            "plannedModes": ["microcompact"] if applied else [],
            "pressureLevel": "elevated" if applied else "low",
            "preservedRawStartIndex": preserved_raw_start_index,
            "compactedMessageCount": len(compacted_prefix),
            "droppedMessageCount": dropped_message_count,
            "tokensSavedEstimate": tokens_saved_estimate,
        },
    }


def compact_historical_tail(messages: list[dict[str, Any]], options: dict[str, Any]) -> list[dict[str, Any]]:
    tool_uses: dict[str, str] = {}
    compaction_state = {"seenFilePaths": set(), "seenCommands": set()}
    output = []
    for message in messages:
        compacted = compact_message_for_continuation(message, tool_uses, compaction_state, options)
        if compacted:
            output.append(compacted)
    return output


def compact_message_for_continuation(message: dict[str, Any], toolUses: dict[str, str], compactionState: dict[str, set[str]], options: dict[str, Any]) -> dict[str, Any] | None:
    content = message.get("content")
    if isinstance(content, str):
        return {**message, "content": truncate(content, int(options["messageTextCharLimit"]))}
    if not isinstance(content, list):
        return message
    next_blocks: list[dict[str, Any]] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        block_type = block.get("type")
        if block_type == "text":
            next_blocks.append({**block, "text": truncate(str(block.get("text") or ""), int(options["messageTextCharLimit"]))})
            continue
        if block_type == "tool_use":
            tool_id = str(block.get("id") or "")
            tool_name = str(block.get("name") or "")
            toolUses[tool_id] = tool_name
            if tool_name in options["lowValueToolNames"]:
                continue
            input_value = block.get("input") if isinstance(block.get("input"), dict) else {}
            next_blocks.append({**block, "input": compact_tool_input(input_value, int(options["toolInputCharLimit"]))})
            continue
        if block_type == "tool_result":
            tool_name = toolUses.get(str(block.get("tool_use_id") or ""), "UnknownTool")
            if tool_name in options["lowValueToolNames"]:
                continue
            next_blocks.append(
                {
                    **block,
                    "content": summarize_tool_result_for_relief(_tool_result_content_string(block.get("content")), int(options["toolResultCharLimit"]), compactionState),
                }
            )
            continue
        next_blocks.append(block)
    return {**message, "content": next_blocks} if next_blocks else None


def apply_partial_compaction(messages: list[dict[str, Any]], reactiveAttempt: int = 0) -> dict[str, Any]:
    if len(messages) <= relief_knob("context.relief_preserve_min_messages") + 4:
        return empty_compaction_result(messages)
    keep_count = max(2, 4 - reactiveAttempt)
    preserved_raw_start_index = _adjust_start_to_tool_boundary(messages, min(max(0, len(messages) - keep_count), choose_preserved_raw_start(messages) + (reactiveAttempt * 4)))
    if preserved_raw_start_index <= 2:
        return empty_compaction_result(messages)
    region = messages[:preserved_raw_start_index]
    region_summary = summarize_continuation_region(region)
    reinjection_lines = normalize_reinjection_lines(render_region_summary_reinjection_lines(region_summary))
    summary_message = _synthetic_user_message(
        render_partial_compaction_summary(region_summary, reactiveAttempt),
        kind="partial_compaction_summary",
    )
    next_messages = [summary_message, *messages[preserved_raw_start_index:]]
    return {
        "applied": True,
        "messages": next_messages,
        "preservedRawStartIndex": preserved_raw_start_index,
        "compactedMessageCount": 1,
        "droppedMessageCount": max(0, preserved_raw_start_index - 1),
        "tokensSavedEstimate": max(0, estimate_messages_tokens(messages) - estimate_messages_tokens(next_messages)),
        "reinjectionLines": reinjection_lines,
        "reinjectionSummary": " | ".join(reinjection_lines[:4]),
    }


def compact_tool_input(input: dict[str, Any], limit: int) -> dict[str, Any]:
    serialized = json.dumps(input, ensure_ascii=False, separators=(",", ":"))
    if len(serialized) <= limit:
        return input
    return {"summary": truncate(serialized, limit)}


def summarize_tool_result_for_relief(content: str, limit: int, compactionState: dict[str, set[str]] | None = None) -> str:
    try:
        parsed = _json_loads_strict(content)
    except Exception:
        return truncate(re.sub(r"\s+", " ", content), limit)
    if not isinstance(parsed, dict):
        return truncate(json.dumps(parsed, ensure_ascii=False, separators=(",", ":")), limit)

    file_record = as_record(parsed.get("file"))
    if file_record and isinstance(file_record.get("filePath"), str) and isinstance(file_record.get("content"), str):
        seen = compactionState.setdefault("seenFilePaths", set()) if compactionState is not None else set()
        duplicate = file_record["filePath"] in seen
        seen.add(file_record["filePath"])
        preview = truncate(re.sub(r"\s+", " ", file_record["content"]), limit)
        return json.dumps(
            omit_none(
                {
                    "type": parsed.get("type") or "text",
                    "file": omit_none(
                        {
                            "filePath": file_record.get("filePath"),
                            "startLine": file_record.get("startLine"),
                            "totalLines": file_record.get("totalLines"),
                            "numLines": file_record.get("numLines"),
                            "preview": None if duplicate else preview,
                            "note": "Repeated file content omitted in microcompact history." if duplicate else None,
                        }
                    ),
                }
            ),
            indent=2,
            ensure_ascii=False,
        )

    if isinstance(parsed.get("command"), str) or "stdout" in parsed or "stderr" in parsed:
        command = parsed.get("command") if isinstance(parsed.get("command"), str) else None
        seen = compactionState.setdefault("seenCommands", set()) if compactionState is not None else set()
        duplicate = bool(command and command in seen)
        if command:
            seen.add(command)
        return json.dumps(
            omit_none(
                {
                    "command": truncate(command, 280) if command else None,
                    "cwd": parsed.get("cwd"),
                    "exitCode": parsed.get("exitCode"),
                    "timedOut": parsed.get("timedOut"),
                    "stdoutPreview": None
                    if duplicate
                    else truncate(re.sub(r"\s+", " ", parsed["stdout"]), 320)
                    if isinstance(parsed.get("stdout"), str)
                    else None,
                    "stderrPreview": truncate(re.sub(r"\s+", " ", parsed["stderr"]), 240) if isinstance(parsed.get("stderr"), str) else None,
                    "note": "Repeated command output omitted in microcompact history." if duplicate else None,
                }
            ),
            indent=2,
            ensure_ascii=False,
        )
    return truncate(json.dumps(parsed, ensure_ascii=False, separators=(",", ":")), limit)
