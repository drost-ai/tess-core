"""Scripted Gemini Interactions streams, for tests that must not reach the network."""
from __future__ import annotations

import json
from typing import Any

DEFAULT_GOOGLE_USAGE = {"total_input_tokens": 1, "total_output_tokens": 1, "total_thought_tokens": 0, "total_cached_tokens": 0, "total_tokens": 2}


def googleInteractionEvents(
    steps: list[dict[str, Any]],
    *,
    status: str | None = None,
    usage: dict[str, Any] | None = None,
    interaction_id: str = "v1_fake_interaction",
    model: str = "gemini-4-hydrogen",
) -> list[dict[str, Any]]:
    """The events of one streamed interaction, from a short description of its steps.

    A step is ``{"type": "text", "text": ...}``, ``{"type": "thought", "signature": ...,
    "summary": ...}`` or ``{"type": "function_call", "id": ..., "name": ...,
    "arguments": {...}}``. ``chunks`` on a text step, or ``argumentChunks`` on a function
    call, splits its content across deltas. The status defaults to ``requires_action``
    when a function is called and ``completed`` otherwise.
    """
    events: list[dict[str, Any]] = [
        {"event_type": "interaction.created", "interaction": {"id": interaction_id, "status": "in_progress", "object": "interaction", "model": model}},
        {"event_type": "interaction.status_update", "interaction_id": interaction_id, "status": "in_progress"},
    ]
    for index, step in enumerate(steps):
        kind = step["type"]
        if kind == "text":
            events.append({"event_type": "step.start", "index": index, "step": {"type": "model_output"}})
            for chunk in step.get("chunks") or [step.get("text", "")]:
                events.append({"event_type": "step.delta", "index": index, "delta": {"type": "text", "text": chunk}})
        elif kind == "thought":
            events.append({"event_type": "step.start", "index": index, "step": {"type": "thought"}})
            if step.get("summary"):
                events.append({"event_type": "step.delta", "index": index, "delta": {"type": "thought_summary", "content": {"type": "text", "text": step["summary"]}}})
            events.append({"event_type": "step.delta", "index": index, "delta": {"type": "thought_signature", "signature": step.get("signature", "sig")}})
        elif kind == "function_call":
            events.append({"event_type": "step.start", "index": index, "step": {"type": "function_call", "id": step["id"], "name": step["name"], "arguments": {}}})
            for chunk in step.get("argumentChunks") or [json.dumps(step.get("arguments") or {})]:
                events.append({"event_type": "step.delta", "index": index, "delta": {"type": "arguments_delta", "arguments": chunk}})
        else:
            events.append({"event_type": "step.start", "index": index, "step": dict(step)})
        events.append({"event_type": "step.stop", "index": index})
    if status is None:
        status = "requires_action" if any(step["type"] == "function_call" for step in steps) else "completed"
    events.append({
        "event_type": "interaction.completed",
        "interaction": {"id": interaction_id, "status": status, "usage": dict(usage or DEFAULT_GOOGLE_USAGE), "object": "interaction", "model": model},
    })
    return events


def googleInteractionsSseBody(events: list[dict[str, Any]], *, done: bool = True) -> str:
    """Render interaction events as the SSE frames the API sends, ending with [DONE]."""
    frames = [f"event: {event.get('event_type') or 'message'}\ndata: {json.dumps(event)}\n\n" for event in events]
    if done:
        frames.append("event: done\ndata: [DONE]\n\n")
    return "".join(frames)


def googleErrorBody(code: str, message: str = "scripted failure") -> dict[str, Any]:
    return {"error": {"code": code, "message": message}}


class FakeGoogleClient:
    """A stand-in for the wire client: each ``interactions.create`` call plays the next script.

    A script is a list of events, or an exception to raise. The request bodies are kept in
    ``requests`` in the order they were sent.
    """

    def __init__(self, scripts: list[Any]) -> None:
        self._scripts = list(scripts)
        self.requests: list[dict[str, Any]] = []
        self.options: list[dict[str, Any]] = []
        self.interactions = self
        self.closed_streams = 0

    def factory(self, options: dict[str, Any]) -> FakeGoogleClient:
        self.options.append(dict(options))
        return self

    async def create(self, params: dict[str, Any]) -> Any:
        self.requests.append(json.loads(json.dumps(params)))
        script = self._scripts[min(len(self.requests) - 1, len(self._scripts) - 1)]
        if isinstance(script, BaseException):
            raise script
        if callable(script):
            script = script(params)
        return _FakeGoogleStream(list(script), self)


class _FakeGoogleStream:
    def __init__(self, events: list[Any], owner: FakeGoogleClient) -> None:
        self._events = events
        self._owner = owner
        self._closed = False

    def __aiter__(self) -> _FakeGoogleStream:
        return self

    async def __anext__(self) -> Any:
        if self._closed or not self._events:
            raise StopAsyncIteration
        event = self._events.pop(0)
        if isinstance(event, BaseException):
            raise event
        if callable(event):
            event = await event()
        return event

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._owner.closed_streams += 1
