from __future__ import annotations

from tests.support.agent_core_case import *

from agent_core.agent.provider_turn import isEmptyAssistantResponse
from agent_core.providers.openai.params import is_codex_base_url
from agent_core.providers.openai.transport import _infer_openai_responses_mode
from agent_core_harness.fake_providers.server import (
    FakeProviderHttpServer,
    openaiChatSseBody,
    openaiResponsesSseBody,
)
from tests.test_providers_openai_wire import _args, _server


def _message_item(item_id, text):
    return {
        "id": item_id,
        "type": "message",
        "content": [{"type": "output_text", "text": text}],
    }


class ResponseIncompleteTests(unittest.TestCase):
    def _run(self, events):
        script = {
            "responses": [
                {
                    "contentType": "text/event-stream",
                    "rawBody": openaiResponsesSseBody(events),
                }
            ]
        }
        server, _requests = _server(script)
        try:
            return run(core.send_openai_responses_request(_args(server)))
        finally:
            server.close()

    def test_truncated_text_resolves_as_max_output_tokens(self):
        response = self._run(
            [
                {"type": "response.created", "response": {"id": "resp_trunc"}},
                {
                    "type": "response.output_item.added",
                    "item": {"id": "item_1", "type": "message"},
                },
                {
                    "type": "response.output_text.delta",
                    "item_id": "item_1",
                    "delta": "partial answer",
                },
                {
                    "type": "response.incomplete",
                    "response": {
                        "id": "resp_trunc",
                        "status": "incomplete",
                        "incomplete_details": {"reason": "max_output_tokens"},
                        "output": [_message_item("item_1", "partial answer")],
                        "usage": {"input_tokens": 5, "output_tokens": 9},
                    },
                },
            ]
        )
        self.assertEqual(response["stopReason"], "max_output_tokens")
        self.assertEqual(
            response["content"], [{"type": "text", "text": "partial answer"}]
        )
        self.assertEqual(response["usage"]["output_tokens"], 9)

    def test_truncation_mid_open_item_salvages_streamed_text(self):
        response = self._run(
            [
                {"type": "response.created", "response": {"id": "resp_open"}},
                {
                    "type": "response.output_item.added",
                    "item": {"id": "item_1", "type": "message"},
                },
                {
                    "type": "response.output_text.delta",
                    "item_id": "item_1",
                    "delta": "cut mid-sent",
                },
                {
                    "type": "response.incomplete",
                    "response": {
                        "id": "resp_open",
                        "status": "incomplete",
                        "incomplete_details": {"reason": "max_output_tokens"},
                        "output": [],
                    },
                },
            ]
        )
        self.assertEqual(response["stopReason"], "max_output_tokens")
        self.assertEqual(
            response["content"], [{"type": "text", "text": "cut mid-sent"}]
        )

    def test_content_filter_reason_maps_to_content_filter(self):
        response = self._run(
            [
                {"type": "response.created", "response": {"id": "resp_filter"}},
                {
                    "type": "response.output_item.added",
                    "item": {"id": "item_1", "type": "message"},
                },
                {
                    "type": "response.output_text.delta",
                    "item_id": "item_1",
                    "delta": "partially fine",
                },
                {
                    "type": "response.incomplete",
                    "response": {
                        "id": "resp_filter",
                        "status": "incomplete",
                        "incomplete_details": {"reason": "content_filter"},
                        "output": [_message_item("item_1", "partially fine")],
                    },
                },
            ]
        )
        self.assertEqual(response["stopReason"], "content_filter")

    def test_empty_incomplete_still_fails_as_protocol(self):
        script = {
            "responses": [
                {
                    "contentType": "text/event-stream",
                    "rawBody": openaiResponsesSseBody(
                        [
                            {
                                "type": "response.created",
                                "response": {"id": "resp_empty"},
                            },
                            {
                                "type": "response.incomplete",
                                "response": {
                                    "id": "resp_empty",
                                    "status": "incomplete",
                                    "incomplete_details": {
                                        "reason": "max_output_tokens"
                                    },
                                    "output": [],
                                },
                            },
                        ]
                    ),
                }
            ]
        }
        server, _requests = _server(script)
        try:
            with self.assertRaises(core.ProviderRequestError) as ctx:
                run(core.send_openai_responses_request(_args(server)))
        finally:
            server.close()
        self.assertEqual(ctx.exception.record["category"], "protocol")
        self.assertFalse(ctx.exception.record["retryable"])
        self.assertEqual(
            ctx.exception.record["streamEventType"], "response.incomplete"
        )


class TrailingEventTests(unittest.TestCase):
    def test_stray_event_after_completed_does_not_fail_the_turn(self):
        message = _message_item("item_1", "done")
        script = {
            "responses": [
                {
                    "contentType": "text/event-stream",
                    "rawBody": openaiResponsesSseBody(
                        [
                            {"type": "response.created", "response": {"id": "r"}},
                            {
                                "type": "response.output_item.added",
                                "item": {"id": "item_1", "type": "message"},
                            },
                            {
                                "type": "response.output_text.delta",
                                "item_id": "item_1",
                                "delta": "done",
                            },
                            {"type": "response.output_item.done", "item": message},
                            {
                                "type": "response.completed",
                                "response": {
                                    "id": "r",
                                    "output": [message],
                                    "usage": {
                                        "input_tokens": 1,
                                        "output_tokens": 1,
                                    },
                                },
                            },
                            {
                                "type": "response.output_text.delta",
                                "item_id": "item_1",
                                "delta": "stray",
                            },
                            {"type": "ping"},
                        ]
                    ),
                }
            ]
        }
        server, _requests = _server(script)
        try:
            response = run(core.send_openai_responses_request(_args(server)))
        finally:
            server.close()
        self.assertEqual(response["content"], [{"type": "text", "text": "done"}])

    def test_duplicate_terminal_still_fails(self):
        message = _message_item("item_1", "done")
        completed = {
            "type": "response.completed",
            "response": {
                "id": "r",
                "output": [message],
                "usage": {"input_tokens": 1, "output_tokens": 1},
            },
        }
        script = {
            "responses": [
                {
                    "contentType": "text/event-stream",
                    "rawBody": openaiResponsesSseBody(
                        [
                            {"type": "response.created", "response": {"id": "r"}},
                            {
                                "type": "response.output_item.added",
                                "item": {"id": "item_1", "type": "message"},
                            },
                            {"type": "response.output_item.done", "item": message},
                            completed,
                            completed,
                        ]
                    ),
                }
            ]
        }
        server, _requests = _server(script)
        try:
            with self.assertRaises(core.ProviderRequestError) as ctx:
                run(core.send_openai_responses_request(_args(server)))
        finally:
            server.close()
        self.assertEqual(ctx.exception.record["category"], "protocol")


class ChatInBandErrorTests(unittest.TestCase):
    def _failure(self, chunks):
        script = {
            "chat_completions": [
                {
                    "contentType": "text/event-stream",
                    "rawBody": openaiChatSseBody(chunks, done=False),
                }
            ]
        }
        server, _requests = _server(script)
        try:
            with self.assertRaises(core.ProviderRequestError) as ctx:
                run(core.send_openai_chat_completions_request(_args(server)))
        finally:
            server.close()
        return ctx.exception.record

    def test_error_chunk_with_http_code_classifies_rate_limit(self):
        record = self._failure(
            [
                {
                    "error": {
                        "message": "Rate limit exceeded, retry shortly",
                        "code": 429,
                    }
                }
            ]
        )
        self.assertEqual(record["category"], "rate_limit")
        self.assertTrue(record["retryable"])
        self.assertEqual(record["status"], 429)

    def test_error_chunk_without_status_classifies_from_text(self):
        record = self._failure(
            [
                {
                    "choices": [
                        {
                            "index": 0,
                            "delta": {"content": "partial"},
                            "finish_reason": None,
                        }
                    ]
                },
                {
                    "error": {
                        "message": "The upstream server had an internal error",
                        "type": "server_error",
                    }
                },
            ]
        )
        self.assertEqual(record["category"], "server")
        self.assertTrue(record["hadAssistantOutput"])


class RefusalSurfacingTests(unittest.TestCase):
    def test_responses_refusal_surfaces_with_refusal_stop_reason(self):
        refusal_item = {
            "id": "item_1",
            "type": "message",
            "content": [
                {"type": "refusal", "refusal": "I can't help with that request."}
            ],
        }
        script = {
            "responses": [
                {
                    "contentType": "text/event-stream",
                    "rawBody": openaiResponsesSseBody(
                        [
                            {"type": "response.created", "response": {"id": "r"}},
                            {
                                "type": "response.output_item.added",
                                "item": {"id": "item_1", "type": "message"},
                            },
                            {
                                "type": "response.output_item.done",
                                "item": refusal_item,
                            },
                            {
                                "type": "response.completed",
                                "response": {
                                    "id": "r",
                                    "output": [refusal_item],
                                    "usage": {
                                        "input_tokens": 4,
                                        "output_tokens": 8,
                                    },
                                },
                            },
                        ]
                    ),
                }
            ]
        }
        server, _requests = _server(script)
        try:
            response = run(core.send_openai_responses_request(_args(server)))
        finally:
            server.close()
        self.assertEqual(response["stopReason"], "refusal")
        self.assertEqual(
            response["content"],
            [{"type": "text", "text": "I can't help with that request."}],
        )
        self.assertFalse(isEmptyAssistantResponse(response))

    def test_chat_refusal_delta_surfaces_with_refusal_stop_reason(self):
        script = {
            "chat_completions": [
                {
                    "contentType": "text/event-stream",
                    "rawBody": openaiChatSseBody(
                        [
                            {
                                "choices": [
                                    {
                                        "index": 0,
                                        "delta": {"refusal": "I can't do that."},
                                        "finish_reason": None,
                                    }
                                ]
                            },
                            {
                                "choices": [
                                    {
                                        "index": 0,
                                        "delta": {},
                                        "finish_reason": "stop",
                                    }
                                ],
                                "usage": {
                                    "prompt_tokens": 2,
                                    "completion_tokens": 3,
                                },
                            },
                        ]
                    ),
                }
            ]
        }
        server, _requests = _server(script)
        try:
            response = run(core.send_openai_chat_completions_request(_args(server)))
        finally:
            server.close()
        self.assertEqual(response["stopReason"], "refusal")
        self.assertEqual(
            response["content"], [{"type": "text", "text": "I can't do that."}]
        )
        self.assertFalse(isEmptyAssistantResponse(response))


class CodexDefaultTrapTests(unittest.TestCase):
    def test_no_base_url_is_not_codex(self):
        self.assertFalse(is_codex_base_url(None))
        self.assertFalse(is_codex_base_url(""))
        self.assertFalse(is_codex_base_url("   "))
        self.assertTrue(is_codex_base_url("https://chatgpt.com/backend-api/codex"))
        self.assertTrue(is_codex_base_url("https://chatgpt.com/backend-api/codex/"))

    def test_mode_inference_defaults_to_responses(self):
        self.assertEqual(_infer_openai_responses_mode(None), "responses")
        self.assertEqual(_infer_openai_responses_mode(""), "responses")
        self.assertEqual(
            _infer_openai_responses_mode("https://chatgpt.com/backend-api/codex"),
            "codex",
        )
        self.assertEqual(
            _infer_openai_responses_mode("https://api.openai.com/v1"), "responses"
        )


if __name__ == "__main__":
    unittest.main()
