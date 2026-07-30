from __future__ import annotations

from .shared import *
from ...message_origin import isRuntimeSyntheticMessage as _is_runtime_synthetic_message


def buildCompactionEvidence(args: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
    args = {**(args or {}), **kwargs}
    source_messages = args.get("sourceMessages") or []
    source_start = int(args.get("sourceStart") or 0)
    cwd = args.get("cwd") or os.getcwd()
    token_budget = int(args.get("tokenBudget") or COMPACTION_EVIDENCE_TOKEN_BUDGET)
    tool_uses: dict[str, dict[str, Any]] = {}
    entries: list[dict[str, str]] = []
    for offset, message in enumerate(source_messages):
        entries.extend(buildEvidenceEntriesForMessage(message, source_start + offset, tool_uses, cwd))
    rendered = [entry["full"] for entry in entries]
    transcript = "\n\n".join(rendered)
    token_estimate = estimateTextTokens(transcript)
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
        token_estimate = estimateTextTokens(transcript)
        truncated = True

    while rendered and token_estimate > token_budget:
        rendered.pop(0)
        omitted_entry_count += 1
        included_entry_count -= 1
        transcript = "\n\n".join(rendered)
        token_estimate = estimateTextTokens(transcript)
        truncated = True

    if omitted_entry_count:
        transcript = "\n\n".join(
            [
                f"NOTE: {omitted_entry_count} older evidence entries were omitted to keep the compaction synthesis request within budget.",
                "\n\n".join(rendered),
            ]
        ).strip()
        token_estimate = estimateTextTokens(transcript)

    return {
        "transcript": transcript,
        "entryCount": len(entries),
        "includedEntryCount": included_entry_count,
        "omittedEntryCount": omitted_entry_count,
        "tokenEstimate": token_estimate,
        "tokenBudget": token_budget,
        "truncated": truncated,
    }


def buildEvidenceEntriesForMessage(message: dict[str, Any], messageIndex: int, toolUses: dict[str, dict[str, Any]], cwd: str) -> list[dict[str, str]]:
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
        return [buildTextEvidenceEntry({"messageIndex": messageIndex, "role": role, "label": text_label, "text": content})]
    if not isinstance(content, list):
        return []

    entries: list[dict[str, str]] = []
    text_blocks = "\n\n".join(str(block.get("text", "")) for block in content if block.get("type") == "text").strip()
    if text_blocks:
        entries.append(buildTextEvidenceEntry({"messageIndex": messageIndex, "role": role, "label": block_label, "text": text_blocks}))
    for block in content:
        if block.get("type") == "tool_use":
            tool_uses_id = str(block.get("id") or "")
            toolUses[tool_uses_id] = {"id": tool_uses_id, "name": block.get("name") or "UnknownTool", "input": block.get("input") or {}, "messageIndex": messageIndex}
            entries.append(buildToolUseEvidenceEntry(messageIndex, str(block.get("name") or "UnknownTool"), asRecord(block.get("input")) or {}))
        elif block.get("type") == "tool_result":
            entries.append(buildToolResultEvidenceEntry(messageIndex, block, toolUses.get(str(block.get("tool_use_id") or "")), cwd))
    return entries


def buildTextEvidenceEntry(args: dict[str, Any]) -> dict[str, str]:
    normalized = collapseWhitespace(str(args.get("text") or ""))
    full_body = truncate(normalized, COMPACTION_MESSAGE_TEXT_CHAR_LIMIT)
    compact_body = truncate(normalized, 480)
    return {
        "full": f"[{args['messageIndex']}] {args['role']} {args['label']}\n{full_body}",
        "compact": f"[{args['messageIndex']}] {args['role']} {args['label']}\n{compact_body}",
    }


def buildToolUseEvidenceEntry(messageIndex: int, toolName: str, input: dict[str, Any]) -> dict[str, str]:
    serialized_input = truncate(json.dumps(input, indent=2, ensure_ascii=False), COMPACTION_TOOL_INPUT_CHAR_LIMIT)
    compact_input = truncate(json.dumps(input, ensure_ascii=False), 320)
    return {
        "full": f"[{messageIndex}] assistant tool_use {toolName}\ninput={serialized_input}",
        "compact": f"[{messageIndex}] assistant tool_use {toolName}\ninput={compact_input}",
    }


def buildToolResultEvidenceEntry(messageIndex: int, block: dict[str, Any], toolUse: dict[str, Any] | None, cwd: str) -> dict[str, str]:
    tool_name = (toolUse or {}).get("name") or "UnknownTool"
    content = str(block.get("content") or "")
    full = summarizeToolResultForCompaction(content, cwd, COMPACTION_TOOL_RESULT_CHAR_LIMIT)
    compact = summarizeToolResultForCompaction(content, cwd, COMPACTION_COMPACT_TOOL_RESULT_CHAR_LIMIT)
    prefix = f"[{messageIndex}] user tool_result {tool_name}{' error' if block.get('is_error') else ''}"
    return {"full": f"{prefix}\n{full}", "compact": f"{prefix}\n{compact}"}


def summarizeToolResultForCompaction(content: str, cwd: str, limit: int) -> str:
    parsed = parseMaybeJson(content)
    record = asRecord(parsed)
    if not record:
        return truncate(collapseWhitespace(content), limit)
    file_record = asRecord(record.get("file"))
    if file_record and isinstance(file_record.get("filePath"), str) and isinstance(file_record.get("content"), str):
        file_path = trimPathEvidence(file_record["filePath"], cwd)
        preview = buildFilePreview(file_record["content"])
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
            f"command={collapseWhitespace(record['command'])}" if isinstance(record.get("command"), str) else None,
            f"cwd={record['cwd']}" if isinstance(record.get("cwd"), str) else None,
            f"exit_code={record['exitCode']}" if isinstance(record.get("exitCode"), (int, float)) and not isinstance(record.get("exitCode"), bool) else None,
            f"timed_out={record['timedOut']}" if isinstance(record.get("timedOut"), bool) else None,
            f"stdout={truncate(collapseWhitespace(record['stdout']), COMPACTION_COMMAND_PREVIEW_CHAR_LIMIT)}" if isinstance(record.get("stdout"), str) else None,
            f"stderr={truncate(collapseWhitespace(record['stderr']), COMPACTION_COMMAND_PREVIEW_CHAR_LIMIT)}" if isinstance(record.get("stderr"), str) else None,
        ]
        return truncate("\n".join(part for part in parts if part), limit)

    if isinstance(record.get("matches"), list):
        matches = [trimPathEvidence(value, cwd) for value in record["matches"] if isinstance(value, str)][:COMPACTION_MATCH_PREVIEW_LIMIT]
        parts = [
            f"type={record['type']}" if isinstance(record.get("type"), str) else None,
            f"pattern={record['pattern']}" if isinstance(record.get("pattern"), str) else None,
            f"query={record['query']}" if isinstance(record.get("query"), str) else None,
            f"path={trimPathEvidence(record['path'], cwd)}" if isinstance(record.get("path"), str) else None,
            f"match_count={len(record['matches'])}",
            f"matches={', '.join(matches)}" if matches else None,
        ]
        return truncate("\n".join(part for part in parts if part), limit)
    return truncate(json.dumps(compactStructuredRecord(record, cwd), indent=2, ensure_ascii=False), limit)


def compactStructuredRecord(record: dict[str, Any], cwd: str) -> dict[str, Any]:
    next_record: dict[str, Any] = {}
    keys = ["type", "path", "pattern", "query", "cwd", "command", "exitCode", "timedOut", "message", "detail", "note", "url", "title", "status", "startLine", "totalLines", "numLines"]
    for key in keys:
        value = record.get(key)
        if isinstance(value, str):
            next_record[key] = trimPathEvidence(value, cwd) if "path" in key.lower() else truncate(value, 400)
        elif isinstance(value, (int, float, bool)):
            next_record[key] = value
    if isinstance(record.get("matches"), list):
        next_record["matches"] = record["matches"][:COMPACTION_MATCH_PREVIEW_LIMIT]
        next_record["matchCount"] = len(record["matches"])
    if isinstance(record.get("content"), str):
        next_record["contentPreview"] = truncate(collapseWhitespace(record["content"]), 600)
    return next_record or {"preview": truncate(json.dumps(record, ensure_ascii=False), 600)}


def buildFilePreview(content: str) -> str:
    lines = content.split("\n")
    if len(lines) <= COMPACTION_FILE_PREVIEW_LINES * 2:
        return truncate(collapseWhitespace(content), COMPACTION_COMMAND_PREVIEW_CHAR_LIMIT)
    head = " ".join(lines[:COMPACTION_FILE_PREVIEW_LINES]).strip()
    tail = " ".join(lines[-COMPACTION_FILE_PREVIEW_LINES:]).strip()
    return truncate(f"{collapseWhitespace(head)} ... {collapseWhitespace(tail)}", COMPACTION_COMMAND_PREVIEW_CHAR_LIMIT)


def trimPathEvidence(filePath: str, cwd: str) -> str:
    normalized = cwd if cwd.endswith(os.sep) else f"{cwd}{os.sep}"
    return filePath[len(normalized) :] if filePath.startswith(normalized) else filePath
