from __future__ import annotations

from tests.support.tess_core_case import *


OPENAI_REQUEST = {
    "model": "test-model",
    "system": "test",
    "messages": [{"role": "user", "content": "continue"}],
    "tools": [
        {
            "name": "Read",
            "description": "Read a file.",
            "input_schema": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        }
    ],
}

def _chat_factory(events):
    async def create(_params):
        return AsyncChunks(events)

    return lambda _options: SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))
    )


def _responses_factory(events):
    async def create(_params):
        return AsyncChunks(events)

    return lambda _options: SimpleNamespace(responses=SimpleNamespace(create=create))


def _send_chat(events):
    return run(
        testing.send_openai_chat_completions_request(
            {
                "token": "test-token",
                "request": OPENAI_REQUEST,
                "runtime": {},
                "maxAttempts": 1,
                "clientFactory": _chat_factory(events),
            }
        )
    )


def _send_responses(events):
    return run(
        testing.send_openai_responses_request(
            {
                "token": "test-token",
                "mode": "responses",
                "request": OPENAI_REQUEST,
                "runtime": {},
                "maxAttempts": 1,
                "clientFactory": _responses_factory(events),
            }
        )
    )


class Package31ProviderTerminalCompletionTests(TessCoreTestCase):
    def assert_incomplete(self, error, mode, assistant_bytes):
        self.assertIsInstance(error, core.ProviderIncompleteResponseError)
        self.assertEqual(error.name, "ProviderIncompleteResponseError")
        self.assertEqual(error.record["category"], "protocol")
        self.assertEqual(error.details["mode"], mode)
        self.assertEqual(error.details["partialAssistantBytes"], assistant_bytes)
        self.assertLess(len(error.record["rawMessage"]), 700)

    def test_chat_rejects_clean_eof_before_terminal_choice(self):
        with self.assertRaises(core.ProviderIncompleteResponseError) as raised:
            _send_chat([])
        self.assert_incomplete(raised.exception, "chat_completions", 0)
        self.assertTrue(raised.exception.record["retryable"])
        self.assertEqual(raised.exception.details["openItems"], 0)

    def test_chat_rejects_partial_text_eof(self):
        with self.assertRaises(core.ProviderIncompleteResponseError) as raised:
            _send_chat(
                [
                    {
                        "choices": [
                            {
                                "index": 0,
                                "delta": {"content": "partial"},
                                "finish_reason": None,
                            }
                        ]
                    }
                ]
            )
        self.assert_incomplete(raised.exception, "chat_completions", 7)
        self.assertFalse(raised.exception.record["retryable"])
        self.assertEqual(raised.exception.details["openItems"], 1)

    def test_chat_reconstructs_tool_json_at_every_fragment_boundary(self):
        raw = json.dumps({"path": "π/😀/file.ts"}, ensure_ascii=False, separators=(",", ":"))
        for split in range(len(raw) + 1):
            response = _send_chat(
                [
                    {
                        "choices": [
                            {
                                "index": 0,
                                "delta": {
                                    "tool_calls": [
                                        {
                                            "index": 0,
                                            "id": "call-1",
                                            "function": {
                                                "name": "Read",
                                                "arguments": raw[:split],
                                            },
                                        }
                                    ]
                                },
                                "finish_reason": None,
                            }
                        ]
                    },
                    {
                        "choices": [
                            {
                                "index": 0,
                                "delta": {
                                    "tool_calls": [
                                        {
                                            "index": 0,
                                            "function": {"arguments": raw[split:]},
                                        }
                                    ]
                                },
                                "finish_reason": "tool_calls",
                            }
                        ]
                    },
                ]
            )
            self.assertEqual(
                response["content"],
                [
                    {
                        "type": "tool_use",
                        "id": "call-1",
                        "name": "Read",
                        "input": {"path": "π/😀/file.ts"},
                    }
                ],
            )

    def test_chat_never_repairs_malformed_terminal_tool_arguments(self):
        with self.assertRaises(core.ProviderIncompleteResponseError) as raised:
            _send_chat(
                [
                    {
                        "choices": [
                            {
                                "index": 0,
                                "delta": {
                                    "tool_calls": [
                                        {
                                            "index": 0,
                                            "id": "call-1",
                                            "function": {
                                                "name": "Read",
                                                "arguments": '{"path":',
                                            },
                                        }
                                    ]
                                },
                                "finish_reason": "tool_calls",
                            }
                        ]
                    }
                ]
            )
        self.assert_incomplete(raised.exception, "chat_completions", 0)
        self.assertEqual(raised.exception.details["partialToolArgumentBytes"], 8)

    def test_chat_validates_all_parallel_calls_before_publishing(self):
        response = _send_chat(
            [
                {
                    "choices": [
                        {
                            "index": 0,
                            "delta": {
                                "tool_calls": [
                                    {
                                        "index": 0,
                                        "id": "call-1",
                                        "function": {
                                            "name": "Read",
                                            "arguments": '{"path":"one"}',
                                        },
                                    },
                                    {
                                        "index": 1,
                                        "id": "call-2",
                                        "function": {
                                            "name": "Read",
                                            "arguments": '{"path":"two"}',
                                        },
                                    },
                                ]
                            },
                            "finish_reason": "tool_calls",
                        }
                    ]
                }
            ]
        )
        self.assertEqual([block["id"] for block in response["content"]], ["call-1", "call-2"])

    def test_responses_requires_response_completed(self):
        with self.assertRaises(core.ProviderIncompleteResponseError) as raised:
            _send_responses([{"type": "response.output_text.delta", "delta": "partial"}])
        self.assert_incomplete(raised.exception, "responses", 7)

    def test_responses_requires_started_items_to_finish(self):
        with self.assertRaises(core.ProviderIncompleteResponseError) as raised:
            _send_responses(
                [
                    {
                        "type": "response.output_item.added",
                        "item": {"id": "item-1", "type": "message"},
                    },
                    {"type": "response.completed", "response": {"output": []}},
                ]
            )
        self.assert_incomplete(raised.exception, "responses", 0)
        self.assertEqual(raised.exception.details["openItems"], 1)

    def test_responses_dispatches_tool_after_completed_item_lifecycle(self):
        raw = json.dumps({"path": "src/index.ts"}, separators=(",", ":"))
        response = _send_responses(
            [
                {
                    "type": "response.output_item.added",
                    "item": {
                        "id": "item-1",
                        "type": "function_call",
                        "call_id": "call-1",
                    },
                },
                {
                    "type": "response.function_call_arguments.done",
                    "item_id": "item-1",
                    "name": "Read",
                    "arguments": raw,
                },
                {
                    "type": "response.output_item.done",
                    "item": {
                        "id": "item-1",
                        "type": "function_call",
                        "call_id": "call-1",
                        "name": "Read",
                        "arguments": raw,
                    },
                },
                {
                    "type": "response.completed",
                    "response": {
                        "output": [
                            {
                                "id": "item-1",
                                "type": "function_call",
                                "call_id": "call-1",
                                "name": "Read",
                                "arguments": raw,
                            }
                        ]
                    },
                },
            ]
        )
        self.assertEqual(
            response["content"],
            [
                {
                    "type": "tool_use",
                    "id": "call-1",
                    "name": "Read",
                    "input": {"path": "src/index.ts"},
                }
            ],
        )

    def test_responses_resolves_name_from_output_item_added(self):
        raw = '{"path":"src/index.ts"}'
        response = _send_responses(
            [
                {
                    "type": "response.output_item.added",
                    "item": {
                        "id": "item-1",
                        "type": "function_call",
                        "call_id": "call-1",
                        "name": "Read",
                    },
                },
                {
                    "type": "response.function_call_arguments.done",
                    "item_id": "item-1",
                    "arguments": raw,
                },
                {
                    "type": "response.output_item.done",
                    "item": {
                        "id": "item-1",
                        "type": "function_call",
                        "call_id": "call-1",
                        "name": "Read",
                        "arguments": raw,
                    },
                },
                {
                    "type": "response.completed",
                    "response": {
                        "output": [
                            {
                                "id": "item-1",
                                "type": "function_call",
                                "call_id": "call-1",
                                "name": "Read",
                                "arguments": raw,
                            }
                        ]
                    },
                },
            ]
        )
        self.assertEqual(response["content"][0]["name"], "Read")

    def test_responses_rejects_arguments_done_without_any_name(self):
        with self.assertRaises(core.ProviderIncompleteResponseError) as raised:
            _send_responses(
                [
                    {
                        "type": "response.output_item.added",
                        "item": {
                            "id": "item-1",
                            "type": "function_call",
                            "call_id": "call-1",
                        },
                    },
                    {
                        "type": "response.function_call_arguments.done",
                        "item_id": "item-1",
                        "arguments": "{}",
                    },
                ]
            )
        self.assertIn("without an item id and name", raised.exception.record["rawMessage"])

    def test_responses_rejects_malformed_terminal_function_snapshot(self):
        with self.assertRaises(core.ProviderIncompleteResponseError) as raised:
            _send_responses(
                [
                    {
                        "type": "response.completed",
                        "response": {
                            "output": [
                                {
                                    "type": "function_call",
                                    "name": "Read",
                                    "arguments": "{}",
                                }
                            ]
                        },
                    }
                ]
            )
        self.assert_incomplete(raised.exception, "responses", 0)

    def test_responses_incomplete_and_cancelled_are_non_success(self):
        terminals = [
            (
                {
                    "type": "response.incomplete",
                    "response": {"incomplete_details": {"reason": "max_output_tokens"}},
                },
                "protocol",
            ),
            (
                {
                    "type": "response.cancelled",
                    "response": {"error": {"message": "cancelled"}},
                },
                "cancelled",
            ),
        ]
        for terminal, category in terminals:
            with self.subTest(category=category):
                with self.assertRaises(core.ProviderRequestError) as raised:
                    _send_responses([terminal])
                self.assertEqual(raised.exception.record["category"], category)

if __name__ == "__main__":
    unittest.main()
