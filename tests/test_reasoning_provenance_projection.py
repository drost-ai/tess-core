from __future__ import annotations

import unittest
from types import SimpleNamespace
from typing import Any

from tests.support.agent_core_case import (
    AgentCoreTestCase,
    AsyncChunks,
    core,
    run,
    testing,
)
from tests.test_package43_review_regressions import _Sink, _completed, _request


class ReasoningProvenanceProjectionTests(AgentCoreTestCase):
    def test_projects_only_closed_reasoning_provenance(self):
        summary = core.create_runtime_event(
            {
                "event": "message.assistant.thinking.partial",
                "data": {
                    "thinking_delta": "inspect",
                    "thinking": "inspect repository",
                    "reasoning_kind": "summary",
                    "user_visible": True,
                    "provider_event_type": "response.reasoning_summary_text.delta",
                },
            }
        )
        self.assertEqual(
            summary["payload"],
            {
                "thinking": "inspect repository",
                "thinkingDelta": "inspect",
                "reasoningKind": "summary",
                "userVisible": True,
                "providerEventType": "response.reasoning_summary_text.delta",
            },
        )

        hidden = core.create_runtime_event(
            {
                "event": "message.assistant.thinking.partial",
                "data": {
                    "thinking": "private",
                    "reasoning_kind": "raw_thinking",
                    "user_visible": False,
                    "provider_event_type": "response.reasoning_text.delta",
                },
            }
        )
        self.assertEqual(hidden["payload"]["reasoningKind"], "raw_thinking")
        self.assertFalse(hidden["payload"]["userVisible"])

    def test_summary_snapshot_never_contains_raw_reasoning_text(self) -> None:
        events: list[tuple[str, dict[str, Any]]] = []

        async def create(_params: dict[str, Any]) -> AsyncChunks:
            return AsyncChunks(
                [
                    {
                        "type": "response.reasoning_text.delta",
                        "delta": "private chain of thought",
                    },
                    {
                        "type": "response.reasoning_summary_text.delta",
                        "delta": "safe summary",
                    },
                    *_completed("done").chunks,
                ]
            )

        response = run(
            testing.sendOpenAiResponsesRequest(
                token="test",
                mode="codex",
                request=_request(),
                runtime={
                    "includePartialMessages": True,
                    "partialOutputSink": _Sink(),
                    "emitEvent": lambda event, data: events.append((event, data)),
                },
                maxAttempts=1,
                clientFactory=lambda _options: SimpleNamespace(
                    responses=SimpleNamespace(create=create)
                ),
            )
        )

        self.assertEqual(response["content"], [{"type": "text", "text": "done"}])
        thinking = [
            data
            for event, data in events
            if event == "message.assistant.thinking.partial"
        ]
        raw = next(
            item for item in thinking if item["reasoning_kind"] == "raw_thinking"
        )
        summary = next(item for item in thinking if item["reasoning_kind"] == "summary")
        self.assertEqual(raw["thinking"], "private chain of thought")
        self.assertEqual(summary["thinking"], "safe summary")
        self.assertNotIn("private chain of thought", summary["thinking"])


if __name__ == "__main__":
    unittest.main()
