"""Assemble one Interactions response from its server-sent events.

An interaction streams as steps. Each step opens with ``step.start``, grows through
``step.delta`` events and closes with ``step.stop``; ``interaction.completed`` then
gives the final status and usage. The steps become the runtime's blocks: a
``model_output`` step is a text block, a ``function_call`` step a tool_use block, and
each ``thought`` step is kept, exactly as received, on the block that follows it.

Google adds event and delta types over time. One this module does not know is skipped.
"""
from __future__ import annotations

import copy
import json
from typing import Any

from .params import THOUGHTS_AFTER, THOUGHTS_BEFORE

# What an in-stream error code would have been as an HTTP status, for the codes that have one.
ERROR_CODE_STATUS = {
    "invalid_request": 400, "failed_precondition": 400, "parameter_unknown": 400, "out_of_range": 416,
    "authentication": 401, "payment_required": 402, "permission_denied": 403,
    "not_found": 404, "model_not_found": 404, "already_exists": 409, "aborted": 409,
    "rate_limit_exceeded": 429, "quota_exceeded": 429, "too_many_requests": 429,
    "api_error": 500, "internal": 500, "internal_server_error": 500, "unimplemented": 501,
    "service_unavailable": 503, "deadline_exceeded": 504, "gateway_timeout": 504,
}
# The model's output was withheld by policy. Asking again gets the same answer.
BLOCKED_CODES = frozenset({
    "safety", "recitation", "language", "prohibited_content", "spii", "blocklist",
    "image_safety", "image_prohibited_content", "image_recitation", "image_other", "content_blocked",
})
# The model produced something malformed. A fresh attempt may not.
GENERATION_ERROR_CODES = frozenset({
    "malformed_function_call", "malformed_tool_call", "unexpected_tool_call",
    "too_many_tool_calls", "missing_thought_signature", "no_image",
})


class GoogleStreamError(Exception):
    """An error the API reported inside the stream, or a terminal status that is not a response."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message or code)
        self.code = code


class GoogleIncompleteStream(Exception):
    """The stream ended, or a step closed, without what a complete response needs."""


class InteractionAccumulator:
    def __init__(self) -> None:
        self.blocks: list[dict[str, Any]] = []
        self.usage: dict[str, Any] = {}
        self.raw_usage: dict[str, Any] = {}
        self.status: str | None = None
        self.interaction_id: str | None = None
        self.text = ""
        self.thinking = ""
        self.had_output = False
        self.had_tool_use = False
        self._open: dict[int, dict[str, Any]] = {}
        self._closed: dict[int, dict[str, Any]] = {}
        self._call_ids: set[str] = set()

    @property
    def open_steps(self) -> int:
        return len(self._open)

    @property
    def partial_argument_bytes(self) -> int:
        return sum(len(str(step.get("arguments_text") or "").encode("utf-8")) for step in self._open.values())

    def feed(self, event: Any) -> list[tuple[str, str]]:
        """Apply one event. Returns what it added: ("text" | "thinking" | "tool", delta)."""
        if not isinstance(event, dict):
            return []
        kind = event.get("event_type")
        if kind == "error":
            error = event.get("error") if isinstance(event.get("error"), dict) else {}
            raise GoogleStreamError(str(error.get("code") or "error"), str(error.get("message") or ""))
        if kind == "interaction.created":
            interaction = event.get("interaction") if isinstance(event.get("interaction"), dict) else {}
            if isinstance(interaction.get("id"), str):
                self.interaction_id = interaction["id"]
            return []
        if kind == "interaction.completed":
            interaction = event.get("interaction") if isinstance(event.get("interaction"), dict) else {}
            self.status = str(interaction.get("status") or "completed")
            if isinstance(interaction.get("id"), str):
                self.interaction_id = interaction["id"]
            if isinstance(interaction.get("usage"), dict):
                self._apply_usage(interaction["usage"])
            return []
        index = event.get("index")
        if not isinstance(index, int) or isinstance(index, bool):
            return []
        if kind == "step.start":
            return self._start(index, event.get("step") if isinstance(event.get("step"), dict) else {})
        if kind == "step.delta":
            return self._delta(index, event.get("delta") if isinstance(event.get("delta"), dict) else {})
        if kind == "step.stop":
            return self._stop(index)
        return []

    def _start(self, index: int, step: dict[str, Any]) -> list[tuple[str, str]]:
        kind = str(step.get("type") or "")
        state: dict[str, Any] = {"type": kind}
        self._open[index] = state
        if kind == "model_output":
            state["text"] = ""
            return self._add_text(state, _content_text(step.get("content")))
        if kind == "thought":
            state["signature"] = str(step.get("signature") or "")
            state["summary"] = []
            added = ""
            for item in step.get("summary") if isinstance(step.get("summary"), list) else []:
                added += self._add_summary(state, item)
            return [("thinking", added)] if added else []
        if kind == "function_call":
            state["id"] = str(step.get("id") or "")
            state["name"] = str(step.get("name") or "")
            state["arguments"] = step.get("arguments") if isinstance(step.get("arguments"), dict) else {}
            state["arguments_text"] = ""
            return [("tool", "")]
        return []

    def _delta(self, index: int, delta: dict[str, Any]) -> list[tuple[str, str]]:
        state = self._open.get(index)
        if state is None:
            return []
        kind = delta.get("type")
        if state["type"] == "model_output" and kind == "text":
            return self._add_text(state, str(delta.get("text") or ""))
        if state["type"] == "thought" and kind == "thought_summary":
            added = self._add_summary(state, delta.get("content"))
            return [("thinking", added)] if added else []
        if state["type"] == "thought" and kind == "thought_signature":
            state["signature"] += str(delta.get("signature") or "")
            return []
        if state["type"] == "function_call" and kind == "arguments_delta":
            arguments = delta.get("arguments")
            if isinstance(arguments, str):
                state["arguments_text"] += arguments
            elif isinstance(arguments, dict):
                state["arguments"] = arguments
            return [("tool", "")]
        return []

    def _stop(self, index: int) -> list[tuple[str, str]]:
        state = self._open.pop(index, None)
        if state is None:
            return []
        # Steps are kept by index: two that overlap in the stream still come out in order.
        if state["type"] == "thought":
            if state["signature"] or state["summary"]:
                thought: dict[str, Any] = {"type": "thought", "signature": state["signature"]}
                if state["summary"]:
                    thought["summary"] = state["summary"]
                self._closed[index] = thought
        elif state["type"] == "model_output":
            if state["text"]:
                self._closed[index] = {"type": "text", "text": state["text"]}
        elif state["type"] == "function_call":
            self._closed[index] = self._tool_use(state)
            self.had_tool_use = True
        return []

    def _tool_use(self, state: dict[str, Any]) -> dict[str, Any]:
        if not state["id"] or not state["name"]:
            raise GoogleIncompleteStream("a function call closed without its id and name")
        if state["id"] in self._call_ids:
            raise GoogleIncompleteStream("two function calls carried the same id")
        self._call_ids.add(state["id"])
        arguments = state["arguments"]
        if state["arguments_text"].strip():
            try:
                arguments = json.loads(state["arguments_text"])
            except ValueError as exc:
                raise GoogleIncompleteStream("a function call closed before its arguments were complete JSON") from exc
        if not isinstance(arguments, dict):
            raise GoogleIncompleteStream("function-call arguments were not a JSON object")
        return {"type": "tool_use", "id": state["id"], "name": state["name"], "input": arguments}

    def _add_text(self, state: dict[str, Any], text: str) -> list[tuple[str, str]]:
        if not text:
            return []
        state["text"] += text
        self.text += text
        self.had_output = True
        return [("text", text)]

    def _add_summary(self, state: dict[str, Any], item: Any) -> str:
        if not isinstance(item, dict):
            return ""
        summary = state["summary"]
        if item.get("type") == "text":
            text = str(item.get("text") or "")
            if summary and summary[-1].get("type") == "text":
                summary[-1]["text"] += text
            else:
                summary.append({"type": "text", "text": text})
            self.thinking += text
            return text
        summary.append(copy.deepcopy(item))
        return ""

    def _apply_usage(self, usage: dict[str, Any]) -> None:
        self.raw_usage = copy.deepcopy(usage)
        counts = {key: _count(usage.get(key)) for key in ("total_input_tokens", "total_output_tokens", "total_thought_tokens", "total_cached_tokens")}
        if counts["total_input_tokens"] is not None:
            self.usage["input_tokens"] = counts["total_input_tokens"]
        if counts["total_output_tokens"] is not None or counts["total_thought_tokens"] is not None:
            # Thinking is billed as output, and counts against max_output_tokens.
            self.usage["output_tokens"] = (counts["total_output_tokens"] or 0) + (counts["total_thought_tokens"] or 0)
        if counts["total_cached_tokens"] is not None:
            self.usage["cache_read_input_tokens"] = counts["total_cached_tokens"]
            self.usage["cache_hit_input_tokens"] = counts["total_cached_tokens"]

    def finish(self) -> str:
        """Close the response and return its stop reason. Raises if it is not a response."""
        if self.status is None:
            raise GoogleIncompleteStream("the stream ended before interaction.completed")
        if self._open:
            raise GoogleIncompleteStream("the interaction completed while steps were still open")
        self.blocks = []
        thoughts: list[dict[str, Any]] = []
        for index in sorted(self._closed):
            step = self._closed[index]
            if step["type"] == "thought":
                thoughts.append(step)
                continue
            if thoughts:
                step["provider_metadata"] = {THOUGHTS_BEFORE: thoughts}
                thoughts = []
            self.blocks.append(step)
        if thoughts and self.blocks:
            self.blocks[-1].setdefault("provider_metadata", {})[THOUGHTS_AFTER] = thoughts
        has_tool_use = any(block.get("type") == "tool_use" for block in self.blocks)
        if self.status == "requires_action":
            if not has_tool_use:
                raise GoogleIncompleteStream("the interaction requires action but carried no function call")
            return "tool_use"
        if self.status == "completed":
            return "tool_use" if has_tool_use else "end_turn"
        if self.status == "incomplete":
            return "tool_use" if has_tool_use else "max_tokens"
        raise GoogleStreamError(self.status, f"The interaction ended with status {self.status}.")


def _content_text(content: Any) -> str:
    if not isinstance(content, list):
        return ""
    return "".join(str(item.get("text") or "") for item in content if isinstance(item, dict) and item.get("type") == "text")


def _count(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        return None
    return int(value)
