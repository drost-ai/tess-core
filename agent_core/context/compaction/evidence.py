from __future__ import annotations

import json
import os
from .parsing import as_record, parse_maybe_json
from .shared import (
    compaction_knob,
)
from .utils import collapse_whitespace, estimate_text_tokens, truncate
from typing import Any
from ...storage.message_origin import is_runtime_synthetic_message as _is_runtime_synthetic_message


def build_compaction_evidence(args: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
    args = {**(args or {}), **kwargs}
    source_messages = args.get("sourceMessages") or []
    source_start = int(args.get("sourceStart") or 0)
    cwd = args.get("cwd") or os.getcwd()
    token_budget = int(args.get("tokenBudget") or compaction_knob("compaction.evidence_token_budget"))
    tool_uses: dict[str, dict[str, Any]] = {}
    entries: list[dict[str, str]] = []
    for offset, message in enumerate(source_messages):
        entries.extend(build_evidence_entries_for_message(message, source_start + offset, tool_uses, cwd))
    rendered = [entry["full"] for entry in entries]
    transcript = "\n\n".join(rendered)
    token_estimate = estimate_text_tokens(transcript)
    included_entry_count = len(entries)
    omitted_entry_count = 0
    truncated = False

    for index, entry in enumerate(entries):
        if token_estimate <= token_budget:
            break
        if rendered[index] == entry["compact"]:
            continue
        rendered[index] = entry["compact"]
        transcript = "\n\n".join(rendered)
        token_estimate = estimate_text_tokens(transcript)
        truncated = True

    while rendered and token_estimate > token_budget:
        rendered.pop(0)
        omitted_entry_count += 1
        included_entry_count -= 1
        transcript = "\n\n".join(rendered)
        token_estimate = estimate_text_tokens(transcript)
        truncated = True

    if omitted_entry_count:
        transcript = "\n\n".join(
            [
                f"NOTE: {omitted_entry_count} older evidence entries were omitted to keep the compaction synthesis request within budget.",
                "\n\n".join(rendered),
            ]
        ).strip()
        token_estimate = estimate_text_tokens(transcript)

    return {
        "transcript": transcript,
        "entryCount": len(entries),
        "includedEntryCount": included_entry_count,
        "omittedEntryCount": omitted_entry_count,
        "tokenEstimate": token_estimate,
        "tokenBudget": token_budget,
        "truncated": truncated,
    }


def build_evidence_entries_for_message(message: dict[str, Any], messageIndex: int, toolUses: dict[str, dict[str, Any]], cwd: str) -> list[dict[str, str]]:
    content = message.get("content")
    role = message.get("role", "user")
    synthetic = _is_runtime_synthetic_message(message)
    text_label = (
        "synthetic_runtime_message"
        if synthetic
        else "user_message"
        if role == "user"
        else "assistant_text"
    )
    block_label = (
        "synthetic_runtime_text_block"
        if synthetic
        else "user_text_block"
        if role == "user"
        else "assistant_text_block"
    )
    if isinstance(content, str):
        return [build_text_evidence_entry({"messageIndex": messageIndex, "role": role, "label": text_label, "text": content})]
    if not isinstance(content, list):
        return []

    entries: list[dict[str, str]] = []
    text_blocks = "\n\n".join(str(block.get("text", "")) for block in content if block.get("type") == "text").strip()
    if text_blocks:
        entries.append(build_text_evidence_entry({"messageIndex": messageIndex, "role": role, "label": block_label, "text": text_blocks}))
    for block in content:
        if block.get("type") == "tool_use":
            tool_uses_id = str(block.get("id") or "")
            toolUses[tool_uses_id] = {"id": tool_uses_id, "name": block.get("name") or "UnknownTool", "input": block.get("input") or {}, "messageIndex": messageIndex}
            entries.append(build_tool_use_evidence_entry(messageIndex, str(block.get("name") or "UnknownTool"), as_record(block.get("input")) or {}))
        elif block.get("type") == "tool_result":
            entries.append(build_tool_result_evidence_entry(messageIndex, block, toolUses.get(str(block.get("tool_use_id") or "")), cwd))
    return entries


def build_text_evidence_entry(args: dict[str, Any]) -> dict[str, str]:
    normalized = collapse_whitespace(str(args.get("text") or ""))
    full_body = truncate(normalized, compaction_knob("compaction.message_text_char_limit"))
    compact_body = truncate(normalized, 480)
    return {
        "full": f"[{args['messageIndex']}] {args['role']} {args['label']}\n{full_body}",
        "compact": f"[{args['messageIndex']}] {args['role']} {args['label']}\n{compact_body}",
    }


def build_tool_use_evidence_entry(messageIndex: int, toolName: str, input: dict[str, Any]) -> dict[str, str]:
    serialized_input = truncate(json.dumps(input, indent=2, ensure_ascii=False), compaction_knob("compaction.tool_input_char_limit"))
    compact_input = truncate(json.dumps(input, ensure_ascii=False), 320)
    return {
        "full": f"[{messageIndex}] assistant tool_use {toolName}\ninput={serialized_input}",
        "compact": f"[{messageIndex}] assistant tool_use {toolName}\ninput={compact_input}",
    }


def build_tool_result_evidence_entry(messageIndex: int, block: dict[str, Any], toolUse: dict[str, Any] | None, cwd: str) -> dict[str, str]:
    tool_name = (toolUse or {}).get("name") or "UnknownTool"
    content = str(block.get("content") or "")
    full = summarize_tool_result_for_compaction(content, cwd, compaction_knob("compaction.tool_result_char_limit"))
    compact = summarize_tool_result_for_compaction(content, cwd, compaction_knob("compaction.compact_tool_result_char_limit"))
    prefix = f"[{messageIndex}] user tool_result {tool_name}{' error' if block.get('is_error') else ''}"
    return {"full": f"{prefix}\n{full}", "compact": f"{prefix}\n{compact}"}


def summarize_tool_result_for_compaction(content: str, cwd: str, limit: int) -> str:
    parsed = parse_maybe_json(content)
    record = as_record(parsed)
    if not record:
        return truncate(collapse_whitespace(content), limit)
    file_record = as_record(record.get("file"))
    if file_record and isinstance(file_record.get("filePath"), str) and isinstance(file_record.get("content"), str):
        file_path = trim_path_evidence(file_record["filePath"], cwd)
        preview = build_file_preview(file_record["content"])
        parts = [
            f"file={file_path}",
            f"start_line={file_record['startLine']}" if isinstance(file_record.get("startLine"), (int, float)) and file_record.get("startLine") else None,
            f"total_lines={file_record['totalLines']}" if isinstance(file_record.get("totalLines"), (int, float)) and file_record.get("totalLines") else None,
            f"num_lines={file_record['numLines']}" if isinstance(file_record.get("numLines"), (int, float)) and file_record.get("numLines") else None,
            f"preview={preview}" if preview else None,
        ]
        return truncate("\n".join(part for part in parts if part), limit)

    if isinstance(record.get("command"), str) or "stdout" in record or "stderr" in record:
        parts = [
            f"command={collapse_whitespace(record['command'])}" if isinstance(record.get("command"), str) else None,
            f"cwd={record['cwd']}" if isinstance(record.get("cwd"), str) else None,
            f"exit_code={record['exitCode']}" if isinstance(record.get("exitCode"), (int, float)) and not isinstance(record.get("exitCode"), bool) else None,
            f"timed_out={record['timedOut']}" if isinstance(record.get("timedOut"), bool) else None,
            f"stdout={truncate(collapse_whitespace(record['stdout']), compaction_knob("compaction.command_preview_char_limit"))}" if isinstance(record.get("stdout"), str) else None,
            f"stderr={truncate(collapse_whitespace(record['stderr']), compaction_knob("compaction.command_preview_char_limit"))}" if isinstance(record.get("stderr"), str) else None,
        ]
        return truncate("\n".join(part for part in parts if part), limit)

    if isinstance(record.get("matches"), list):
        matches = [trim_path_evidence(value, cwd) for value in record["matches"] if isinstance(value, str)][:compaction_knob("compaction.match_preview_limit")]
        parts = [
            f"type={record['type']}" if isinstance(record.get("type"), str) else None,
            f"pattern={record['pattern']}" if isinstance(record.get("pattern"), str) else None,
            f"query={record['query']}" if isinstance(record.get("query"), str) else None,
            f"path={trim_path_evidence(record['path'], cwd)}" if isinstance(record.get("path"), str) else None,
            f"match_count={len(record['matches'])}",
            f"matches={', '.join(matches)}" if matches else None,
        ]
        return truncate("\n".join(part for part in parts if part), limit)
    return truncate(json.dumps(compact_structured_record(record, cwd), indent=2, ensure_ascii=False), limit)


def compact_structured_record(record: dict[str, Any], cwd: str) -> dict[str, Any]:
    next_record: dict[str, Any] = {}
    keys = ["type", "path", "pattern", "query", "cwd", "command", "exitCode", "timedOut", "message", "detail", "note", "url", "title", "status", "startLine", "totalLines", "numLines"]
    for key in keys:
        value = record.get(key)
        if isinstance(value, str):
            next_record[key] = trim_path_evidence(value, cwd) if "path" in key.lower() else truncate(value, 400)
        elif isinstance(value, (int, float, bool)):
            next_record[key] = value
    if isinstance(record.get("matches"), list):
        next_record["matches"] = record["matches"][:compaction_knob("compaction.match_preview_limit")]
        next_record["matchCount"] = len(record["matches"])
    if isinstance(record.get("content"), str):
        next_record["contentPreview"] = truncate(collapse_whitespace(record["content"]), 600)
    return next_record or {"preview": truncate(json.dumps(record, ensure_ascii=False), 600)}


def build_file_preview(content: str) -> str:
    lines = content.split("\n")
    if len(lines) <= compaction_knob("compaction.file_preview_lines") * 2:
        return truncate(collapse_whitespace(content), compaction_knob("compaction.command_preview_char_limit"))
    head = " ".join(lines[:compaction_knob("compaction.file_preview_lines")]).strip()
    tail = " ".join(lines[-compaction_knob("compaction.file_preview_lines"):]).strip()
    return truncate(f"{collapse_whitespace(head)} ... {collapse_whitespace(tail)}", compaction_knob("compaction.command_preview_char_limit"))


def trim_path_evidence(filePath: str, cwd: str) -> str:
    normalized = cwd if cwd.endswith(os.sep) else f"{cwd}{os.sep}"
    return filePath[len(normalized) :] if filePath.startswith(normalized) else filePath
