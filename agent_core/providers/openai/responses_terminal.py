from __future__ import annotations

from typing import Any

from .normalization import _apply_openai_usage
from .reconcile import (
    OpenAiEmptyCompletedResponse,
    OpenAiReconciliationConflict,
    reconciliation_conflict_failure,
    resolve_terminal_projection,
)
from .recovery import commit_openai_delivery
from .responses_session import extract_turn_state as _extract_turn_state
from ..._utils import get_value
from .transport import _plain_openai_value


async def handle_response_completed(context: dict[str, Any]) -> dict[str, Any]:
    """Reconcile terminal output with accumulated blocks, then commit delivery.

    Authoritative blocks are selected before ``response_committed`` is set and
    before any sink operation. An empty textual projection never reaches the
    sink: tool-only responses skip textual reconcile/commit entirely, and a
    response with neither text nor tool use raises ``empty_completed_response``.
    """
    incomplete_response = context["incomplete_response"]
    if context["open_output_items"] or context["pending_by_item_id"]:
        raise incomplete_response(
            "response.completed arrived while output items were still open"
        )
    completed = get_value(context["event"], "response", {}) or {}
    completed_status = str(get_value(completed, "status", "") or "").strip()
    if completed_status and completed_status != "completed":
        raise incomplete_response(
            f"response.completed carried non-completed status {completed_status}"
        )
    response_id = context["response_id"]
    completed_id = str(get_value(completed, "id", "") or "")
    if completed_id:
        response_id = completed_id
    output = get_value(completed, "output", [])
    output = output if isinstance(output, list) else []
    accumulated = [
        context["completed_item_records"][item_id]
        for item_id in context["completed_item_order"]
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
            provider_label=context["provider_label"],
            phase=context["phase"],
            attempt=context["attempt"],
            had_output=context["had_output"],
            had_tool_use=context["had_tool_use"],
            logical_call_id=context["logical_call_id"],
            response_id=response_id,
        ) from conflict
    except OpenAiEmptyCompletedResponse as empty:
        if not context["reasoning_text"]:
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
    _apply_openai_usage(context["usage"], get_value(completed, "usage"))
    has_text = bool(selection["text"])
    partial_output_commit = await commit_openai_delivery(
        context["delivery"],
        generation=context["attempt"],
        authoritative_text=selection["text"],
        reasoning_text=context["reasoning_text"],
        response_id=response_id,
        provider_label=context["provider_label"],
        phase=context["phase"],
        had_output=has_text,
        had_tool_use=selection["had_tool_use"],
        logical_call_id=context["logical_call_id"],
        has_text=has_text,
        runtime=context["runtime"],
    )
    return {
        "blocks": selection["blocks"],
        "authoritative_text": selection["text"],
        "had_output": has_text,
        "had_tool_use": selection["had_tool_use"],
        "stop_reason": selection["stop_reason"],
        "response_id": response_id,
        "completed_response_output": _plain_openai_value(output),
        "completed_response_id": completed_id.strip() or None,
        "completed_turn_state": (
            _extract_turn_state(completed) or context["completed_turn_state"]
        ),
        "reconciliation": selection["diagnostics"],
        "partial_output_commit": partial_output_commit,
    }
