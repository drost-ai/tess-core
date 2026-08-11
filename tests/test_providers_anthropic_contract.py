from __future__ import annotations

from tests.support.agent_core_case import *

from agent_core.providers.anthropic.payloads import (
    _normalize_anthropic_tool_definition,
    normalize_anthropic_content_blocks,
)
from agent_core.providers.anthropic.sse import normalize_anthropic_sse_response
from agent_core.providers.turn_session import create_provider_turn_session
from agent_core_harness.fake_providers.server import FakeProviderHttpServer


def _server(script=None):
    requests: list[dict] = []
    server = FakeProviderHttpServer(
        provider="anthropic", script=script, request_recorder=requests
    ).start()
    return server, requests


def _config(server, **overrides):
    config = {
        "provider": "anthropic",
        "anthropicBaseUrl": server.base_url,
        "apiKey": "contract-key",
        "customHeaders": {},
        "providerCapabilities": {},
    }
    config.update(overrides)
    return config


def _request(**overrides):
    request = {
        "model": "claude-sonnet-4-6",
        "system": "Contract test.",
        "messages": [{"role": "user", "content": "hello"}],
        "tools": [],
    }
    request.update(overrides)
    return request


def _sse_body(events):
    return "".join(
        f"event: {event.get('type', 'message')}\ndata: {json.dumps(event)}\n\n"
        for event in events
    )


def _messages_sse(content_events, stop_reason="end_turn"):
    return _sse_body(
        [
            {
                "type": "message_start",
                "message": {
                    "id": "msg_contract",
                    "model": "claude-sonnet-4-6",
                    "usage": {"input_tokens": 3},
                },
            },
            *content_events,
            {
                "type": "message_delta",
                "delta": {"stop_reason": stop_reason},
                "usage": {"output_tokens": 2},
            },
            {"type": "message_stop"},
        ]
    )


class ThinkingRoundTripTests(unittest.TestCase):
    def test_thinking_blocks_survive_payload_normalization(self):
        blocks = normalize_anthropic_content_blocks(
            [
                {"type": "thinking", "thinking": "chain", "signature": "sig=="},
                {"type": "redacted_thinking", "data": "opaque=="},
                {"type": "text", "text": "answer"},
            ]
        )
        self.assertEqual(
            blocks,
            [
                {"type": "thinking", "thinking": "chain", "signature": "sig=="},
                {"type": "redacted_thinking", "data": "opaque=="},
                {"type": "text", "text": "answer"},
            ],
        )

    def test_signatureless_thinking_still_round_trips(self):
        blocks = normalize_anthropic_content_blocks(
            [{"type": "thinking", "thinking": "compat backend"}]
        )
        self.assertEqual(blocks, [{"type": "thinking", "thinking": "compat backend"}])

    def test_internal_keys_are_stripped_from_passthrough_blocks(self):
        blocks = normalize_anthropic_content_blocks(
            [
                {
                    "type": "server_tool_use",
                    "id": "srvtoolu_1",
                    "name": "web_search",
                    "input": {"query": "x"},
                    "_input_buffer": "{}",
                }
            ]
        )
        self.assertEqual(
            blocks,
            [
                {
                    "type": "server_tool_use",
                    "id": "srvtoolu_1",
                    "name": "web_search",
                    "input": {"query": "x"},
                }
            ],
        )

    def test_second_turn_request_carries_thinking_on_the_wire(self):
        server, requests = _server()
        try:
            run(
                core.send_anthropic_messages_request(
                    config=_config(server),
                    request=_request(
                        messages=[
                            {"role": "user", "content": "solve it"},
                            {
                                "role": "assistant",
                                "content": [
                                    {
                                        "type": "thinking",
                                        "thinking": "step one",
                                        "signature": "sig==",
                                    },
                                    {
                                        "type": "tool_use",
                                        "id": "toolu_1",
                                        "name": "calc",
                                        "input": {"a": 1},
                                    },
                                ],
                            },
                            {
                                "role": "user",
                                "content": [
                                    {
                                        "type": "tool_result",
                                        "tool_use_id": "toolu_1",
                                        "content": "2",
                                    }
                                ],
                            },
                        ]
                    ),
                    runtime={},
                    maxAttempts=1,
                )
            )
        finally:
            server.close()
        wire_messages = requests[0]["body"]["messages"]
        assistant_content = wire_messages[1]["content"]
        self.assertEqual(
            assistant_content[0],
            {"type": "thinking", "thinking": "step one", "signature": "sig=="},
        )
        self.assertEqual(assistant_content[1]["type"], "tool_use")


class ToolDefinitionPassthroughTests(unittest.TestCase):
    def test_server_tool_definitions_pass_through_untouched(self):
        tool = {"type": "web_search_20250305", "name": "web_search", "max_uses": 3}
        self.assertEqual(_normalize_anthropic_tool_definition(tool), tool)

    def test_client_tool_extras_ride_through(self):
        normalized = _normalize_anthropic_tool_definition(
            {
                "name": "lookup",
                "description": "d",
                "input_schema": {"type": "object", "properties": {}},
                "strict": True,
                "defer_loading": True,
            }
        )
        self.assertEqual(normalized["name"], "lookup")
        self.assertTrue(normalized["strict"])
        self.assertTrue(normalized["defer_loading"])
        self.assertEqual(
            normalized["input_schema"], {"type": "object", "properties": {}}
        )

    def test_tool_definitions_reach_the_wire_faithfully(self):
        server, requests = _server()
        try:
            run(
                core.send_anthropic_messages_request(
                    config=_config(server),
                    request=_request(
                        tools=[
                            {
                                "name": "lookup",
                                "description": "d",
                                "input_schema": {
                                    "type": "object",
                                    "properties": {},
                                },
                                "strict": True,
                            },
                            {
                                "type": "web_search_20250305",
                                "name": "web_search",
                                "max_uses": 2,
                            },
                        ]
                    ),
                    runtime={},
                    maxAttempts=1,
                )
            )
        finally:
            server.close()
        wire_tools = requests[0]["body"]["tools"]
        self.assertTrue(wire_tools[0]["strict"])
        server_tool = wire_tools[1]
        server_tool.pop("cache_control", None)
        self.assertEqual(
            server_tool,
            {"type": "web_search_20250305", "name": "web_search", "max_uses": 2},
        )


class ServerBlockPreservationTests(unittest.TestCase):
    def test_sse_stream_preserves_thinking_redacted_and_server_tool_blocks(self):
        raw = _messages_sse(
            [
                {
                    "type": "content_block_start",
                    "index": 0,
                    "content_block": {"type": "thinking", "thinking": ""},
                },
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "thinking_delta", "thinking": "reasoning"},
                },
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "signature_delta", "signature": "sig=="},
                },
                {"type": "content_block_stop", "index": 0},
                {
                    "type": "content_block_start",
                    "index": 1,
                    "content_block": {"type": "redacted_thinking", "data": "opaque=="},
                },
                {"type": "content_block_stop", "index": 1},
                {
                    "type": "content_block_start",
                    "index": 2,
                    "content_block": {
                        "type": "server_tool_use",
                        "id": "srvtoolu_1",
                        "name": "web_search",
                        "input": {},
                    },
                },
                {
                    "type": "content_block_delta",
                    "index": 2,
                    "delta": {
                        "type": "input_json_delta",
                        "partial_json": '{"query": "weather"}',
                    },
                },
                {"type": "content_block_stop", "index": 2},
                {
                    "type": "content_block_start",
                    "index": 3,
                    "content_block": {
                        "type": "web_search_tool_result",
                        "tool_use_id": "srvtoolu_1",
                        "content": [
                            {
                                "type": "web_search_result",
                                "url": "https://example.com",
                                "title": "t",
                            }
                        ],
                    },
                },
                {"type": "content_block_stop", "index": 3},
                {
                    "type": "content_block_start",
                    "index": 4,
                    "content_block": {"type": "text", "text": ""},
                },
                {
                    "type": "content_block_delta",
                    "index": 4,
                    "delta": {"type": "text_delta", "text": "It is sunny."},
                },
                {"type": "content_block_stop", "index": 4},
            ]
        )
        response = normalize_anthropic_sse_response(
            raw, {"model": "claude-sonnet-4-6"}, {}, attempt=1
        )
        types = [block["type"] for block in response["content"]]
        self.assertEqual(
            types,
            [
                "thinking",
                "redacted_thinking",
                "server_tool_use",
                "web_search_tool_result",
                "text",
            ],
        )
        self.assertEqual(response["content"][0]["signature"], "sig==")
        self.assertEqual(response["content"][1]["data"], "opaque==")
        self.assertEqual(response["content"][2]["input"], {"query": "weather"})
        self.assertNotIn("_input_buffer", response["content"][2])
        self.assertEqual(
            response["content"][3]["content"][0]["url"], "https://example.com"
        )
        self.assertEqual(response["stopReason"], "end_turn")


class RetryAfterCaptureTests(unittest.TestCase):
    def test_anthropic_429_captures_retry_after_ms(self):
        server, _requests = _server(
            {
                "messages": [
                    {
                        "status": 429,
                        "contentType": "application/json",
                        "body": {
                            "error": {
                                "type": "rate_limit_error",
                                "message": "Rate limited",
                            }
                        },
                        "headers": {"retry-after": "3"},
                    }
                ]
            }
        )
        try:
            with self.assertRaises(core.ProviderRequestError) as ctx:
                run(
                    core.send_anthropic_messages_request(
                        config=_config(server),
                        request=_request(),
                        runtime={},
                        maxAttempts=1,
                    )
                )
        finally:
            server.close()
        record = ctx.exception.record
        self.assertEqual(record["category"], "rate_limit")
        self.assertEqual(record["status"], 429)
        self.assertEqual(record["retryAfterMs"], 3000)


class SocketLifetimeTests(unittest.TestCase):
    def test_typed_midstream_failure_never_reuses_the_poisoned_connection(self):
        error_stream = _sse_body(
            [
                {
                    "type": "message_start",
                    "message": {"id": "msg_err", "model": "claude-sonnet-4-6"},
                },
                {
                    "type": "error",
                    "error": {"type": "overloaded_error", "message": "temporary"},
                },
            ]
        ) + "event: noise\ndata: {\"type\": \"ping\"}\n\n"
        good_stream = _messages_sse(
            [
                {
                    "type": "content_block_start",
                    "index": 0,
                    "content_block": {"type": "text", "text": ""},
                },
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "text_delta", "text": "recovered"},
                },
                {"type": "content_block_stop", "index": 0},
            ]
        )
        server, _requests = _server(
            {
                "messages": [
                    {"contentType": "text/event-stream", "rawBody": error_stream},
                    {"contentType": "text/event-stream", "rawBody": good_stream},
                ]
            }
        )

        async def exercise():
            config = _config(
                server, providerCapabilities={"anthropicHttpKeepAlive": True}
            )
            runtime = {
                "emitEvent": lambda *_: None,
                "streamProviderMessages": True,
            }
            turn = create_provider_turn_session(
                logical_turn_id="contract-lifetime",
                session={"id": "contract-lifetime", "provider": "anthropic"},
                config=config,
                runtime=runtime,
                model="claude-sonnet-4-6",
            )
            try:
                return await core.send_anthropic_messages_request(
                    config=config,
                    request=_request(),
                    runtime=runtime,
                    maxAttempts=2,
                    turnSession=turn,
                )
            finally:
                await turn.close()

        try:
            response = run(exercise())
        finally:
            server.close()
        self.assertEqual(response["content"], [{"type": "text", "text": "recovered"}])
        # The poisoned keep-alive connection must not be reused: the retry
        # opens a fresh connection.
        self.assertEqual(server.connection_count, 2)


if __name__ == "__main__":
    unittest.main()
