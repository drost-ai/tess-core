from __future__ import annotations

from typing import Any

from ..errors import createProviderFailureRecord, createProviderRequestError
from .normalization import _parse_complete_json_object
from ..._utils import get_value

TERMINAL_RECONCILIATION_SCHEMA_VERSION = 1

# Terminal output item kinds that carry no assistant-visible content and are
# expected on the Codex route. They are skipped silently; genuinely unknown
# kinds are counted as unsupported bounded diagnostics instead.
_KNOWN_NON_CONTENT_ITEM_TYPES = frozenset({"reasoning"})


class OpenAiReconciliationConflict(Exception):
    """Accumulated and terminal projections contradict each other."""

    def __init__(self, conflict_kind: str, diagnostics: dict[str, Any]) -> None:
        super().__init__(conflict_kind)
        self.name = "OpenAiReconciliationConflict"
        self.conflict_kind = conflict_kind
        self.diagnostics = diagnostics


class OpenAiEmptyCompletedResponse(Exception):
    """Terminal completion carried no usable text or tool use anywhere."""

    def __init__(self, diagnostics: dict[str, Any]) -> None:
        super().__init__("empty_completed_response")
        self.name = "OpenAiEmptyCompletedResponse"
        self.diagnostics = diagnostics


def message_projection_record(item_id: str, text: str) -> dict[str, Any]:
    return {"identity": item_id or None, "kind": "message", "text": text}


def close_openai_output_item(context: dict[str, Any]) -> bool:
    """Close one streamed output item and record its projection identity.

    Returns True when the item completed a function call. Mutates the loop
    state dictionaries supplied in ``context``; raises via the provided
    ``incomplete_response`` factory on any structural violation.
    """
    event = context["event"]
    incomplete_response = context["incomplete_response"]
    item = get_value(event, "item", {})
    item_id = str(get_value(item, "id", None) or get_value(event, "item_id", "") or "")
    open_output_items = context["open_output_items"]
    if not item_id or item_id not in open_output_items:
        raise incomplete_response(
            "an output item completed without a matching start event"
        )
    pending_by_item_id = context["pending_by_item_id"]
    tool_argument_bytes_by_item = context["tool_argument_bytes_by_item"]
    completed_tool = False
    item_type = str(get_value(item, "type", "") or "")
    if item_type == "function_call":
        pending = pending_by_item_id.get(item_id)
        call_id = str(
            get_value(item, "call_id", "")
            or context["item_to_call_id"].get(item_id, "")
            or ""
        )
        name = str(get_value(item, "name", "") or (pending or {}).get("name") or "")
        raw_value = get_value(item, "arguments")
        raw = raw_value if isinstance(raw_value, str) else str((pending or {}).get("raw") or "")
        if not call_id or not name:
            raise incomplete_response(
                f"function-call output item {item_id} completed without a call id and name"
            )
        if call_id in context["emitted_tool_ids"]:
            raise incomplete_response(f"function call {call_id} completed more than once")
        input_value = (pending or {}).get("input")
        if not isinstance(input_value, dict) or raw != (pending or {}).get("raw"):
            try:
                input_value = _parse_complete_json_object(raw)
            except Exception as exc:
                tool_argument_bytes_by_item[item_id] = len(raw.encode("utf-8"))
                raise incomplete_response(str(exc) or "tool arguments were malformed") from exc
        tool_argument_bytes_by_item[item_id] = len(raw.encode("utf-8"))
        context["emitted_tool_ids"].add(call_id)
        context["blocks"].append(
            {
                "type": "tool_use",
                "id": call_id,
                "name": name,
                "input": input_value,
            }
        )
        context["completed_item_records"][item_id] = function_call_projection_record(
            item_id, call_id=call_id, name=name, input_value=input_value,
        )
        context["completed_item_order"].append(item_id)
        completed_tool = True
    elif item_type == "message":
        parts = []
        for content in get_value(item, "content", []) or []:
            text_value = get_value(content, "text")
            if get_value(content, "type") == "output_text" and isinstance(text_value, str):
                parts.append(text_value)
        context["completed_item_records"][item_id] = message_projection_record(
            item_id, "".join(parts) or context["item_text"].get(item_id, "")
        )
        context["completed_item_order"].append(item_id)
    del open_output_items[item_id]
    context["completed_output_items"].add(item_id)
    pending_by_item_id.pop(item_id, None)
    return completed_tool


def function_call_projection_record(
    item_id: str,
    *,
    call_id: str,
    name: str,
    input_value: dict[str, Any],
) -> dict[str, Any]:
    return {
        "identity": call_id or item_id or None,
        "kind": "function_call",
        "call_id": call_id,
        "name": name,
        "input": input_value,
    }


def normalize_terminal_projection(output: Any) -> dict[str, Any]:
    """Normalize response.completed output through stable item identities."""
    absent = not isinstance(output, list)
    records: list[dict[str, Any]] = []
    unsupported = 0
    malformed = False
    for item in output if isinstance(output, list) else []:
        item_type = get_value(item, "type")
        if item_type == "message":
            item_id = str(get_value(item, "id", "") or "")
            parts = []
            for content in get_value(item, "content", []) or []:
                text = get_value(content, "text")
                if get_value(content, "type") == "output_text" and isinstance(text, str):
                    parts.append(text)
            records.append(message_projection_record(item_id, "".join(parts)))
            continue
        if item_type == "function_call":
            item_id = str(get_value(item, "id", "") or "")
            call_id = str(get_value(item, "call_id", "") or item_id or "")
            name = str(get_value(item, "name", "") or "")
            raw = get_value(item, "arguments")
            raw = raw if isinstance(raw, str) else ""
            if not call_id or not name:
                malformed = True
                continue
            try:
                input_value = _parse_complete_json_object(raw)
            except Exception:
                malformed = True
                continue
            records.append(
                {
                    "identity": call_id or item_id or None,
                    "kind": "function_call",
                    "call_id": call_id,
                    "name": name,
                    "input": input_value,
                }
            )
            continue
        if item_type in _KNOWN_NON_CONTENT_ITEM_TYPES:
            continue
        unsupported += 1
    return {
        "absent": absent,
        "records": records,
        "unsupported": unsupported,
        "malformed": malformed,
    }


def select_authoritative_projection(
    accumulated: list[dict[str, Any]],
    terminal: dict[str, Any],
) -> dict[str, Any]:
    """Select the authoritative block set per the Package 131 contract."""
    a_records = list(accumulated)
    t_records: list[dict[str, Any]] = list(terminal["records"])
    unsupported = int(terminal["unsupported"])
    pairs, a_only, t_only = _pair_records(a_records, t_records)
    diagnostics = _diagnostics(
        a_records,
        t_records,
        selected_source="conflict",
        omission_count=len(a_only),
        unsupported=unsupported,
    )
    for a_record, t_record, _a_index, _t_index in pairs:
        conflict_kind = _records_conflict(a_record, t_record)
        if conflict_kind is not None:
            diagnostics["conflict_kind"] = conflict_kind
            raise OpenAiReconciliationConflict(conflict_kind, diagnostics)
    if terminal["malformed"]:
        if a_records:
            return _selection(a_records, "incremental", a_records, t_records, a_only, unsupported)
        diagnostics["conflict_kind"] = "terminal_malformed"
        raise OpenAiReconciliationConflict("terminal_malformed", diagnostics)
    if not t_records:
        if unsupported and not a_records:
            diagnostics["conflict_kind"] = "unproven_empty_terminal"
            raise OpenAiReconciliationConflict("unproven_empty_terminal", diagnostics)
        if not a_records:
            raise OpenAiEmptyCompletedResponse(diagnostics)
        return _selection(a_records, "incremental", a_records, t_records, a_only, unsupported)
    if not a_records and unsupported:
        diagnostics["conflict_kind"] = "unproven_terminal_content"
        raise OpenAiReconciliationConflict("unproven_terminal_content", diagnostics)
    if not a_only and not t_only:
        return _selection(t_records, "terminal", a_records, t_records, a_only, unsupported)
    if not t_only:
        return _selection(a_records, "incremental", a_records, t_records, a_only, unsupported)
    if not a_only:
        return _selection(t_records, "terminal", a_records, t_records, a_only, unsupported)
    if not _consistent_shared_order(pairs):
        diagnostics["conflict_kind"] = "ordering_conflict"
        raise OpenAiReconciliationConflict("ordering_conflict", diagnostics)
    union = _union_order(a_records, t_records, pairs, a_only, t_only)
    return _selection(union, "union", a_records, t_records, a_only, unsupported)


def resolve_terminal_projection(
    accumulated: list[dict[str, Any]],
    output: Any,
) -> dict[str, Any]:
    terminal = normalize_terminal_projection(output)
    return select_authoritative_projection(accumulated, terminal)


def reconciliation_conflict_failure(
    conflict: OpenAiReconciliationConflict,
    *,
    provider_label: str,
    phase: str,
    attempt: int,
    had_output: bool,
    had_tool_use: bool,
    logical_call_id: str,
    response_id: str | None,
) -> Any:
    return createProviderRequestError(
        createProviderFailureRecord(
            provider="openai",
            providerLabel=provider_label,
            phase=phase,
            category="protocol",
            retryable=True,
            attempt=attempt,
            hadAssistantOutput=had_output,
            hadToolUse=had_tool_use,
            rawMessage=f"Terminal reconciliation conflict: {conflict.conflict_kind}.",
            code="terminal_reconciliation_conflict",
            logicalCallId=logical_call_id,
            generation=attempt,
            responseId=response_id,
        )
    )


def _selection(
    records: list[dict[str, Any]],
    source: str,
    a_records: list[dict[str, Any]],
    t_records: list[dict[str, Any]],
    a_only: list[dict[str, Any]],
    unsupported: int,
) -> dict[str, Any]:
    blocks = projection_blocks(records)
    text = "".join(
        block["text"] for block in blocks if block["type"] == "text"
    )
    had_tool_use = any(block["type"] == "tool_use" for block in blocks)
    return {
        "records": records,
        "blocks": blocks,
        "text": text,
        "had_tool_use": had_tool_use,
        "stop_reason": "tool_calls" if had_tool_use else "stop",
        "diagnostics": _diagnostics(
            a_records,
            t_records,
            selected_source=source,
            omission_count=len(a_only),
            unsupported=unsupported,
        ),
    }


def projection_blocks(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    for record in records:
        if record["kind"] == "message":
            if record["text"]:
                blocks.append({"type": "text", "text": record["text"]})
            continue
        blocks.append(
            {
                "type": "tool_use",
                "id": record["call_id"],
                "name": record["name"],
                "input": record["input"],
            }
        )
    return blocks


def _diagnostics(
    a_records: list[dict[str, Any]],
    t_records: list[dict[str, Any]],
    *,
    selected_source: str,
    omission_count: int,
    unsupported: int,
) -> dict[str, Any]:
    return {
        "incremental_block_count": len(a_records),
        "terminal_block_count": len(t_records),
        "selected_source": selected_source,
        "terminal_omission_count": omission_count,
        "unsupported_terminal_item_count": unsupported,
    }


def _pair_records(
    a_records: list[dict[str, Any]],
    t_records: list[dict[str, Any]],
) -> tuple[
    list[tuple[dict[str, Any], dict[str, Any], int, int]],
    list[dict[str, Any]],
    list[dict[str, Any]],
]:
    pairs: list[tuple[dict[str, Any], dict[str, Any], int, int]] = []
    a_matched: set[int] = set()
    t_matched: set[int] = set()
    t_by_identity: dict[str, tuple[int, dict[str, Any]]] = {}
    for t_index, t_record in enumerate(t_records):
        identity = t_record.get("identity")
        if identity and identity not in t_by_identity:
            t_by_identity[identity] = (t_index, t_record)
    for a_index, a_record in enumerate(a_records):
        identity = a_record.get("identity")
        if not identity or identity not in t_by_identity:
            continue
        t_index, t_record = t_by_identity[identity]
        pairs.append((a_record, t_record, a_index, t_index))
        a_matched.add(a_index)
        t_matched.add(t_index)
    # Secondary positional pairing for message records without stable ids.
    a_rest = [
        (index, record)
        for index, record in enumerate(a_records)
        if index not in a_matched and record["kind"] == "message"
    ]
    t_rest = [
        (index, record)
        for index, record in enumerate(t_records)
        if index not in t_matched and record["kind"] == "message"
    ]
    for (a_index, a_record), (t_index, t_record) in zip(a_rest, t_rest):
        pairs.append((a_record, t_record, a_index, t_index))
        a_matched.add(a_index)
        t_matched.add(t_index)
    a_only = [record for index, record in enumerate(a_records) if index not in a_matched]
    t_only = [record for index, record in enumerate(t_records) if index not in t_matched]
    return pairs, a_only, t_only


def _records_conflict(a_record: dict[str, Any], t_record: dict[str, Any]) -> str | None:
    if a_record["kind"] != t_record["kind"]:
        return "kind_mismatch"
    if a_record["kind"] == "message":
        return None if a_record["text"] == t_record["text"] else "text_mismatch"
    if a_record.get("call_id") != t_record.get("call_id"):
        return "tool_identity_mismatch"
    if a_record.get("name") != t_record.get("name"):
        return "tool_name_mismatch"
    return None if a_record.get("input") == t_record.get("input") else "tool_argument_mismatch"


def _consistent_shared_order(
    pairs: list[tuple[dict[str, Any], dict[str, Any], int, int]],
) -> bool:
    ordered = sorted(pairs, key=lambda pair: pair[2])
    t_indices = [pair[3] for pair in ordered]
    return t_indices == sorted(t_indices)


def _union_order(
    a_records: list[dict[str, Any]],
    t_records: list[dict[str, Any]],
    pairs: list[tuple[dict[str, Any], dict[str, Any], int, int]],
    a_only: list[dict[str, Any]],
    t_only: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Deterministic union: terminal order with accumulated-only items anchored.

    Complete accumulated blocks are never silently discarded. Items present
    only in the accumulated projection are inserted after the nearest paired
    accumulated predecessor, preserving the terminal ordering otherwise.
    """
    t_only = _deduplicated_terminal_tools(t_only, a_only)
    if not pairs:
        return list(a_records) + t_only
    t_only_kept = {id(record) for record in t_only}
    paired_t_indices = {t_index for _a, _t, _a_index, t_index in pairs}
    a_index_of = {id(record): index for index, record in enumerate(a_records)}
    paired_a_indices = sorted(a_index for _a, _t, a_index, _t_index in pairs)
    a_by_t_index = {t_index: a_index for _a, _t, a_index, t_index in pairs}

    def anchor(a_record: dict[str, Any]) -> int | None:
        predecessors = [
            index for index in paired_a_indices if index < a_index_of[id(a_record)]
        ]
        return max(predecessors) if predecessors else None

    inserted: set[int] = set()
    result: list[dict[str, Any]] = []
    for a_record in a_only:
        if anchor(a_record) is None:
            result.append(a_record)
            inserted.add(id(a_record))
    for t_index, t_record in enumerate(t_records):
        if t_index not in paired_t_indices and id(t_record) not in t_only_kept:
            continue
        result.append(t_record)
        paired_a = a_by_t_index.get(t_index)
        if paired_a is None:
            continue
        for a_record in a_only:
            if id(a_record) not in inserted and anchor(a_record) == paired_a:
                result.append(a_record)
                inserted.add(id(a_record))
    for a_record in a_only:
        if id(a_record) not in inserted:
            result.append(a_record)
    return result


def _deduplicated_terminal_tools(
    t_only: list[dict[str, Any]],
    a_only: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Drop terminal-only tool calls that repeat an accumulated call."""
    accumulated_signatures = {
        (record.get("name"), _stable_input_key(record.get("input")))
        for record in a_only
        if record["kind"] == "function_call"
    }
    deduplicated: list[dict[str, Any]] = []
    for record in t_only:
        if record["kind"] == "function_call":
            signature = (record.get("name"), _stable_input_key(record.get("input")))
            if signature in accumulated_signatures:
                continue
        deduplicated.append(record)
    return deduplicated


def _stable_input_key(value: Any) -> str:
    return repr(value)
