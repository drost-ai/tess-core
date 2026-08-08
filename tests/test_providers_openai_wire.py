from __future__ import annotations

from tests.support.agent_core_case import *

from agent_core.providers.openai.responses import sendOpenAiRequest
from agent_core.providers.openai.transport import (
    _acquire_openai_client,
    _create_openai_client,
)
from agent_core.providers.openai._wire import OpenAiWireClient
from agent_core.providers.turn_session import createProviderTurnSession
from agent_core.providers.watchdog import AbortSignal
from agent_core_harness.fake_providers.server import (
    FakeProviderHttpServer,
    openaiChatSseBody,
    openaiResponsesSseBody,
)


def _server(script=None):
    requests: list[dict] = []
    server = FakeProviderHttpServer(
        provider="openai",
        script=script,
        request_recorder=requests,
        record_all_headers=True,
    ).start()
    return server, requests


def _request():
    return {
        "model": "gpt-5.4",
        "system": "Wire test system.",
        "messages": [{"role": "user", "content": "hello"}],
        "tools": [],
    }


def _args(server, **overrides):
    args = {
        "token": "wire-test-token",
        "baseUrl": server.base_url,
        "mode": "responses",
        "request": _request(),
        "runtime": {},
        "maxAttempts": 1,
    }
    args.update(overrides)
    return args


class OpenAiWireResponsesTests(unittest.TestCase):
    def test_responses_mode_completes_over_real_socket_without_factory(self):
        server, requests = _server()
        try:
            response = run(core.sendOpenAiResponsesRequest(_args(server)))
        finally:
            server.close()
        self.assertEqual(response["content"], [{"type": "text", "text": "ok"}])
        self.assertEqual(response["stopReason"], "stop")
        self.assertEqual(response["usage"]["input_tokens"], 1)
        self.assertEqual(len(requests), 1)
        record = requests[0]
        self.assertEqual(record["path"], "/v1/responses")
        self.assertEqual(record["api"], "responses")
        self.assertEqual(
            record["headers"].get("authorization"), "Bearer wire-test-token"
        )
        self.assertEqual(record["headers"].get("accept"), "text/event-stream")
        self.assertEqual(record["body"]["model"], "gpt-5.4")
        self.assertTrue(record["body"]["stream"])

    def test_chat_mode_completes_over_real_socket_without_factory(self):
        server, requests = _server()
        try:
            response = run(core.sendOpenAiChatCompletionsRequest(_args(server)))
        finally:
            server.close()
        self.assertEqual(response["content"], [{"type": "text", "text": "ok"}])
        self.assertEqual(response["stopReason"], "stop")
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0]["path"], "/v1/chat/completions")
        self.assertEqual(requests[0]["api"], "chat_completions")

    def test_send_openai_request_infers_mode_when_missing(self):
        server, requests = _server()
        try:
            args = _args(server)
            del args["mode"]
            response = run(sendOpenAiRequest(args))
        finally:
            server.close()
        self.assertEqual(response["content"], [{"type": "text", "text": "ok"}])
        self.assertEqual(requests[0]["api"], "responses")

    def test_function_call_stream_produces_tool_use(self):
        call_item = {
            "id": "item_fn",
            "type": "function_call",
            "call_id": "call_1",
            "name": "get_weather",
            "arguments": '{"city":"Wellington"}',
        }
        script = {
            "responses": [
                {
                    "contentType": "text/event-stream",
                    "rawBody": openaiResponsesSseBody(
                        [
                            {"type": "response.created", "response": {"id": "resp_fn"}},
                            {
                                "type": "response.output_item.added",
                                "item": {
                                    "id": "item_fn",
                                    "type": "function_call",
                                    "call_id": "call_1",
                                    "name": "get_weather",
                                },
                            },
                            {
                                "type": "response.function_call_arguments.done",
                                "item_id": "item_fn",
                                "name": "get_weather",
                                "arguments": '{"city":"Wellington"}',
                            },
                            {"type": "response.output_item.done", "item": call_item},
                            {
                                "type": "response.completed",
                                "response": {
                                    "id": "resp_fn",
                                    "output": [call_item],
                                    "usage": {"input_tokens": 2, "output_tokens": 2},
                                },
                            },
                        ]
                    ),
                }
            ]
        }
        server, _requests = _server(script)
        try:
            response = run(core.sendOpenAiResponsesRequest(_args(server)))
        finally:
            server.close()
        self.assertEqual(response["stopReason"], "tool_calls")
        self.assertEqual(
            response["content"],
            [
                {
                    "type": "tool_use",
                    "id": "call_1",
                    "name": "get_weather",
                    "input": {"city": "Wellington"},
                }
            ],
        )


class OpenAiWireErrorClassificationTests(unittest.TestCase):
    def _failure(self, script, api="responses"):
        server, _requests = _server(script)
        try:
            with self.assertRaises(core.ProviderRequestError) as ctx:
                if api == "chat_completions":
                    run(core.sendOpenAiChatCompletionsRequest(_args(server)))
                else:
                    run(core.sendOpenAiResponsesRequest(_args(server)))
        finally:
            server.close()
        return ctx.exception.record

    def test_http_429_classifies_rate_limit_with_retry_guidance(self):
        record = self._failure(
            {
                "responses": [
                    {
                        "status": 429,
                        "contentType": "application/json",
                        "body": {
                            "error": {
                                "message": "Rate limit reached for gpt-5.4",
                                "type": "rate_limit_error",
                                "code": "rate_limit_exceeded",
                            }
                        },
                        "headers": {
                            "retry-after": "2",
                            "x-request-id": "req_wire_429",
                        },
                    }
                ]
            }
        )
        self.assertEqual(record["category"], "rate_limit")
        self.assertTrue(record["retryable"])
        self.assertEqual(record["status"], 429)
        self.assertEqual(record["code"], "rate_limit_exceeded")
        self.assertEqual(record["requestId"], "req_wire_429")
        self.assertEqual(record["retryAfterMs"], 2000)

    def test_http_401_classifies_auth_non_retryable(self):
        record = self._failure(
            {
                "responses": [
                    {
                        "status": 401,
                        "contentType": "application/json",
                        "body": {
                            "error": {
                                "message": "Incorrect API key provided",
                                "type": "invalid_request_error",
                            }
                        },
                    }
                ]
            }
        )
        self.assertEqual(record["category"], "auth")
        self.assertFalse(record["retryable"])
        self.assertEqual(record["status"], 401)

    def test_http_500_classifies_server_retryable_on_chat(self):
        record = self._failure(
            {
                "chat_completions": [
                    {
                        "status": 500,
                        "contentType": "application/json",
                        "body": {"error": {"message": "The server had an error"}},
                    }
                ]
            },
            api="chat_completions",
        )
        self.assertEqual(record["category"], "server")
        self.assertTrue(record["retryable"])
        self.assertEqual(record["status"], 500)

    def test_in_band_error_event_classifies_rate_limit(self):
        record = self._failure(
            {
                "responses": [
                    {
                        "contentType": "text/event-stream",
                        "rawBody": openaiResponsesSseBody(
                            [
                                {
                                    "type": "response.created",
                                    "response": {"id": "resp_err"},
                                },
                                {
                                    "type": "error",
                                    "code": "rate_limit_exceeded",
                                    "message": "Rate limit reached",
                                },
                            ]
                        ),
                    }
                ]
            }
        )
        self.assertEqual(record["category"], "rate_limit")
        self.assertTrue(record["retryable"])
        self.assertEqual(record["streamEventType"], "error")

    def test_malformed_sse_event_classifies_protocol(self):
        record = self._failure(
            {
                "responses": [
                    {
                        "contentType": "text/event-stream",
                        "rawBody": "event: response.created\ndata: {not json\n\n",
                    }
                ]
            }
        )
        self.assertEqual(record["category"], "protocol")
        self.assertFalse(record["retryable"])

    def test_json_content_type_without_error_classifies_protocol(self):
        record = self._failure(
            {
                "responses": [
                    {
                        "status": 200,
                        "contentType": "application/json",
                        "body": {"unexpected": True},
                    }
                ]
            }
        )
        self.assertEqual(record["category"], "protocol")
        self.assertFalse(record["retryable"])


if __name__ == "__main__":
    unittest.main()
