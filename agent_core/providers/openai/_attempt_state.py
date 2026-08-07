from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..openai_turn_session import PreparedOpenAiRequest as _PreparedOpenAiRequest


@dataclass
class ResponsesAttemptState:
    """Per-attempt state for one OpenAI Responses streaming attempt.

    Constructing a fresh instance at the top of each retry attempt is what
    guarantees no stream state leaks between attempts; every field here is
    attempt-scoped. Attempt-invariant request context (mode, delivery,
    logical call id, timeouts) stays in sendOpenAiResponsesRequest locals.
    """

    phase: str = "request_start"
    had_output: bool = False
    had_tool_use: bool = False
    had_provisional_tool_call: bool = False
    blocks: list[dict[str, Any]] = field(default_factory=list)
    usage: dict[str, Any] = field(default_factory=dict)
    stop_reason: str = "stop"
    emitted_text: str = ""
    reasoning_text: str = ""
    reasoning_summary_text: str = ""
    raw_reasoning_text: str = ""
    emitted_tool_ids: set[str] = field(default_factory=set)
    pending_by_item_id: dict[str, dict[str, Any]] = field(default_factory=dict)
    item_to_call_id: dict[str, str] = field(default_factory=dict)
    item_to_name: dict[str, str] = field(default_factory=dict)
    open_output_items: dict[str, str] = field(default_factory=dict)
    completed_output_items: set[str] = field(default_factory=set)
    tool_argument_bytes_by_item: dict[str, int] = field(default_factory=dict)
    item_text: dict[str, str] = field(default_factory=dict)
    completed_item_order: list[str] = field(default_factory=list)
    completed_item_records: dict[str, dict[str, Any]] = field(default_factory=dict)
    open_message_item: str | None = None
    reconciliation: dict[str, Any] | None = None
    saw_completed: bool = False
    completed_response_output: list[Any] = field(default_factory=list)
    completed_response_id: str | None = None
    completed_turn_state: str | None = None
    active_stream: Any = None
    prepared_attempt: _PreparedOpenAiRequest | None = None
    response_committed: bool = False
    response_id: str | None = None
    sequence_number: int | None = None
    partial_output_commit: dict[str, Any] | None = None
    preview_published: bool = False
