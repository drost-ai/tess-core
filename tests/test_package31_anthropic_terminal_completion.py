from __future__ import annotations

from tests.support.agent_core_case import *


REQUEST = {
    "model": "claude-test",
    "maxOutputTokens": 100,
    "system": "test",
    "messages": [{"role": "user", "content": "continue"}],
    "tools": [
        {
            "name": "Read",
            "description": "Read a file.",
            "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}},
        }
    ],
}


def _sse(events):
    return "\n\n".join(
        f"event: {event['type']}\ndata: {json.dumps(event, ensure_ascii=False)}"
        for event in events
    )


class Package31AnthropicTerminalCompletionTests(AgentCoreTestCase):
    def assert_incomplete(self, error, assistant_bytes):
        self.assertIsInstance(error, core.ProviderIncompleteResponseError)
        self.assertEqual(error.record["category"], "protocol")
        self.assertEqual(error.details["mode"], "messages")
        self.assertEqual(error.details["partialAssistantBytes"], assistant_bytes)
        self.assertLess(len(error.record["rawMessage"]), 700)

    def test_requires_message_stop_after_blocks_close(self):
        raw = _sse(
            [
                {
                    "type": "content_block_start",
                    "index": 0,
                    "content_block": {"type": "text", "text": "partial"},
                },
                {"type": "content_block_stop", "index": 0},
            ]
        )
        with self.assertRaises(core.ProviderIncompleteResponseError) as raised:
            testing.normalize_anthropic_sse_response(raw, REQUEST)
        self.assert_incomplete(raised.exception, 7)

    def test_rejects_message_stop_with_open_tool_block(self):
        raw = _sse(
            [
                {
                    "type": "content_block_start",
                    "index": 0,
                    "content_block": {
                        "type": "tool_use",
                        "id": "tool-1",
                        "name": "Read",
                        "input": {},
                    },
                },
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "input_json_delta", "partial_json": '{"path":'},
                },
                {"type": "message_stop"},
            ]
        )
        with self.assertRaises(core.ProviderIncompleteResponseError) as raised:
            testing.normalize_anthropic_sse_response(raw, REQUEST)
        self.assert_incomplete(raised.exception, 0)
        self.assertEqual(raised.exception.details["openItems"], 1)
        self.assertEqual(raised.exception.details["partialToolArgumentBytes"], 8)

    def test_accepts_closed_thinking_text_and_fragmented_tool_blocks(self):
        emitted = []
        events = [
            {
                "type": "message_start",
                "message": {"model": "claude-test", "usage": {"input_tokens": 2}},
            },
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "thinking", "thinking": ""},
            },
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "thinking_delta", "thinking": "think"},
            },
            {"type": "content_block_stop", "index": 0},
            {
                "type": "content_block_start",
                "index": 1,
                "content_block": {"type": "text", "text": ""},
            },
            {
                "type": "content_block_delta",
                "index": 1,
                "delta": {"type": "text_delta", "text": "done"},
            },
            {"type": "content_block_stop", "index": 1},
            {
                "type": "content_block_start",
                "index": 2,
                "content_block": {
                    "type": "tool_use",
                    "id": "tool-1",
                    "name": "Read",
                    "input": {},
                },
            },
            {
                "type": "content_block_delta",
                "index": 2,
                "delta": {"type": "input_json_delta", "partial_json": '{"path":"src/'},
            },
            {
                "type": "content_block_delta",
                "index": 2,
                "delta": {"type": "input_json_delta", "partial_json": 'index.ts"}'},
            },
            {"type": "content_block_stop", "index": 2},
            {
                "type": "message_delta",
                "delta": {"stop_reason": "tool_use"},
                "usage": {"output_tokens": 8},
            },
            {"type": "message_stop"},
        ]
        response = testing.normalize_anthropic_sse_response(
            _sse(events),
            REQUEST,
            {
                "includePartialMessages": True,
                "emitEvent": lambda event, data: emitted.append((event, data)),
            },
        )
        self.assertEqual(
            response["content"],
            [
                {"type": "thinking", "thinking": "think", "signature": None},
                {"type": "text", "text": "done"},
                {
                    "type": "tool_use",
                    "id": "tool-1",
                    "name": "Read",
                    "input": {"path": "src/index.ts"},
                },
            ],
        )
        self.assertEqual(response["stopReason"], "tool_use")
        self.assertEqual(response["usage"], {"input_tokens": 2, "output_tokens": 8})
        thinking = next(
            data
            for event, data in emitted
            if event == "message.assistant.thinking.partial"
        )
        self.assertEqual(thinking["reasoning_kind"], "raw_thinking")
        self.assertFalse(thinking["user_visible"])
        self.assertEqual(
            thinking["provider_event_type"],
            "content_block_delta.thinking_delta",
        )


if __name__ == "__main__":
    unittest.main()
