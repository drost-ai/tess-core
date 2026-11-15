from __future__ import annotations

from collections.abc import Callable
from typing import Any

from ._attempt_state import ResponsesAttemptState
from .normalization import _apply_openai_usage
from .reconcile import (
    OpenAiEmptyCompletedResponse,
    OpenAiReconciliationConflict,
    message_projection_record,
    normalize_terminal_projection,
    reconciliation_conflict_failure,
    resolve_terminal_projection,
)
from .recovery import commit_openai_delivery
from .responses_session import extract_turn_state as _extract_turn_state
from ..partial_output import PartialOutputDelivery
from ..._utils import get_value
from .transport import _plain_openai_value


async def handle_response_completed(
    state: ResponsesAttemptState,
    *,
    event: Any,
    incomplete_response: Callable[[str], Any],
    provider_label: str,
    attempt: int,
    logical_call_id: str,
    delivery: PartialOutputDelivery | None,
    runtime: dict[str, Any],
) -> None:
    """Reconcile terminal output with accumulated blocks, then commit delivery.

    Authoritative blocks are selected before ``response_committed`` is set and
    before any sink operation. An empty textual projection never reaches the
    sink: tool-only responses skip textual reconcile/commit entirely, and a
    response with neither text nor tool use raises ``empty_completed_response``.
    On success the terminal results are committed into ``state``; on any
    failure ``state`` keeps its pre-terminal values (usage excepted).
    """
    if state.open_output_items or state.pending_by_item_id:
        raise incomplete_response(
            "response.completed arrived while output items were still open"
        )
    completed = get_value(event, "response", {}) or {}
    completed_status = str(get_value(completed, "status", "") or "").strip()
    if completed_status and completed_status != "completed":
        raise incomplete_response(
            f"response.completed carried non-completed status {completed_status}"
        )
    await _commit_terminal_response(
        state,
        completed,
        incomplete_response=incomplete_response,
        provider_label=provider_label,
        attempt=attempt,
        logical_call_id=logical_call_id,
        delivery=delivery,
        runtime=runtime,
        stop_reason_override=None,
    )


async def handle_response_incomplete(
    state: ResponsesAttemptState,
    *,
    event: Any,
    incomplete_response: Callable[[str], Any],
    provider_label: str,
    attempt: int,
    logical_call_id: str,
    delivery: PartialOutputDelivery | None,
    runtime: dict[str, Any],
) -> bool:
    """Commit a truncated response as a valid turn when output is salvageable.

    ``response.incomplete`` is the provider's normal max-tokens/content-filter
    truncation. Open message items close with their streamed text; a truncated
    function call has no executable arguments and is dropped. Returns False
    when nothing is salvageable — the caller then fails the turn exactly as
    before.
    """
    completed = get_value(event, "response", {}) or {}
    details = get_value(completed, "incomplete_details", {}) or {}
    reason = str(get_value(details, "reason", "") or "").strip()
    for item_id, item_type in list(state.open_output_items.items()):
        if item_type == "message":
            state.completed_item_records[item_id] = message_projection_record(
                item_id, state.item_text.get(item_id, "")
            )
            state.completed_item_order.append(item_id)
        state.completed_output_items.add(item_id)
        state.pending_by_item_id.pop(item_id, None)
        del state.open_output_items[item_id]
    accumulated = [
        state.completed_item_records[item_id]
        for item_id in state.completed_item_order
    ]
    salvageable = any(
        record.get("text")
        or record.get("refusal")
        or record.get("kind") == "function_call"
        for record in accumulated
    ) or bool(
        normalize_terminal_projection(get_value(completed, "output", []))["records"]
    )
    if not salvageable:
        return False
    await _commit_terminal_response(
        state,
        completed,
        incomplete_response=incomplete_response,
        provider_label=provider_label,
        attempt=attempt,
        logical_call_id=logical_call_id,
        delivery=delivery,
        runtime=runtime,
        stop_reason_override=(
            "content_filter" if reason == "content_filter" else "max_output_tokens"
        ),
    )
    return True


async def _commit_terminal_response(
    state: ResponsesAttemptState,
    completed: Any,
    *,
    incomplete_response: Callable[[str], Any],
    provider_label: str,
    attempt: int,
    logical_call_id: str,
    delivery: PartialOutputDelivery | None,
    runtime: dict[str, Any],
    stop_reason_override: str | None,
) -> None:
    response_id = state.response_id
    completed_id = str(get_value(completed, "id", "") or "")
    if completed_id:
        response_id = completed_id
    output = get_value(completed, "output", [])
    output = output if isinstance(output, list) else []
    accumulated = [
        state.completed_item_records[item_id]
        for item_id in state.completed_item_order
    ]
    try:
        selection = resolve_terminal_projection(accumulated, output)
    except OpenAiReconciliationConflict as conflict:
        if not accumulated:
            raise incomplete_response(
                "terminal projection could not be verified: "
                f"{conflict.conflict_kind}"
            ) from conflict
        raise reconciliation_conflict_failure(
            conflict,
            provider_label=provider_label,
            phase=state.phase,
            attempt=attempt,
            had_output=state.had_output,
            had_tool_use=state.had_tool_use,
            logical_call_id=logical_call_id,
            response_id=response_id,
        ) from conflict
    except OpenAiEmptyCompletedResponse as empty:
        if not state.reasoning_text:
            raise incomplete_response("empty_completed_response") from empty
        # Reasoning-only completion: the provider streamed reasoning and
        # deliberately produced no assistant text or tool use. This is the
        # existing suppression contract, not a malformed response.
        selection = {
            "records": [],
            "blocks": [],
            "text": "",
            "had_tool_use": False,
            "stop_reason": "stop",
            "diagnostics": {
                **empty.diagnostics,
                "selected_source": "suppressed_empty",
            },
        }
    _apply_openai_usage(state.usage, get_value(completed, "usage"))
    has_text = bool(selection["text"])
    partial_output_commit = await commit_openai_delivery(
        delivery,
        generation=attempt,
        authoritative_text=selection["text"],
        reasoning_text=state.reasoning_text,
        response_id=response_id,
        provider_label=provider_label,
        phase=state.phase,
        had_output=has_text,
        had_tool_use=selection["had_tool_use"],
        logical_call_id=logical_call_id,
        has_text=has_text,
        runtime=runtime,
    )
    state.blocks = selection["blocks"]
    state.emitted_text = selection["text"]
    state.had_output = has_text
    state.had_tool_use = selection["had_tool_use"]
    # A truncated turn with a complete tool call is still actionable: the
    # tool-calls stop reason wins so the agent loop executes it.
    state.stop_reason = (
        selection["stop_reason"]
        if stop_reason_override is None or selection["had_tool_use"]
        else stop_reason_override
    )
    state.response_id = response_id
    state.completed_response_output = _plain_openai_value(output)
    state.completed_response_id = completed_id.strip() or None
    state.completed_turn_state = (
        _extract_turn_state(completed) or state.completed_turn_state
    )
    state.reconciliation = selection["diagnostics"]
    state.partial_output_commit = partial_output_commit
    state.response_committed = True
    state.saw_completed = True
