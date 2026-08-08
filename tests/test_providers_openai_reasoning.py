from __future__ import annotations

from tests.support.agent_core_case import *

from agent_core_harness.fake_providers.server import openaiChatSseBody
from tests.test_providers_openai_wire import _args, _server


class ReasoningVisibilityTests(unittest.TestCase):
    def test_profile_reasoning_summary_reaches_responses_wire(self):
        server, requests = _server()
        try:
            run(
                core.sendOpenAiResponsesRequest(
                    _args(
                        server,
                        config={
                            "providerProfile": {"openAiChatReasoningSummary": "auto"}
                        },
                        request={**_args(server)["request"], "effort": "high"},
                    )
                )
            )
        finally:
            server.close()
        self.assertEqual(
            requests[0]["body"]["reasoning"], {"effort": "high", "summary": "auto"}
        )

    def test_no_summary_without_profile_option(self):
        server, requests = _server()
        try:
            run(
                core.sendOpenAiResponsesRequest(
                    _args(
                        server,
                        request={**_args(server)["request"], "effort": "high"},
                    )
                )
            )
        finally:
            server.close()
        self.assertEqual(requests[0]["body"]["reasoning"], {"effort": "high"})

    def test_chat_reasoning_content_emits_thinking_partials(self):
        chunks = [
            {
                "choices": [
                    {
                        "index": 0,
                        "delta": {"reasoning_content": "Let me think. "},
                        "finish_reason": None,
                    }
                ]
            },
            {
                "choices": [
                    {
                        "index": 0,
                        "delta": {"reasoning_content": "17*23 is 391."},
                        "finish_reason": None,
                    }
                ]
            },
            {
                "choices": [
                    {
                        "index": 0,
                        "delta": {"content": "391"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 4, "completion_tokens": 8},
            },
        ]
        script = {
            "chat_completions": [
                {
                    "contentType": "text/event-stream",
                    "rawBody": openaiChatSseBody(chunks),
                }
            ]
        }
        server, _requests = _server(script)
        events: list[tuple[str, dict]] = []
        runtime = {
            "includePartialMessages": True,
            "emitEvent": lambda event, data: events.append((event, data)),
        }
        try:
            response = run(
                core.sendOpenAiChatCompletionsRequest(_args(server, runtime=runtime))
            )
        finally:
            server.close()
        self.assertEqual(response["content"], [{"type": "text", "text": "391"}])
        thinking = [data for event, data in events if event == "message.assistant.thinking.partial"]
        self.assertEqual(len(thinking), 2)
        self.assertEqual(thinking[0]["thinking_delta"], "Let me think. ")
        self.assertEqual(thinking[1]["thinking"], "Let me think. 17*23 is 391.")
        self.assertEqual(thinking[1]["reasoning_kind"], "raw_thinking")



if __name__ == "__main__":
    unittest.main()
