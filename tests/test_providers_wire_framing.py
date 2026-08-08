from __future__ import annotations

from tests.support.agent_core_case import *

from agent_core_harness.fake_providers.server import (
    FakeProviderHttpServer,
    openaiResponsesSseBody,
)
from tests.test_providers_openai_wire import _args as _openai_args
from tests.test_providers_openai_wire import _server as _openai_server


def _anthropic_server(script=None, record_all_headers=False):
    requests: list[dict] = []
    server = FakeProviderHttpServer(
        provider="anthropic",
        script=script,
        request_recorder=requests,
        record_all_headers=record_all_headers,
    ).start()
    return server, requests


def _anthropic_config(server):
    return {
        "provider": "anthropic",
        "anthropicBaseUrl": server.base_url,
        "apiKey": "framing-key",
        "customHeaders": {},
        "providerCapabilities": {},
    }


def _anthropic_request():
    return {
        "model": "claude-sonnet-4-6",
        "system": "Framing test.",
        "messages": [{"role": "user", "content": "hello"}],
        "tools": [],
    }


def _send_anthropic(server, **overrides):
    args = {
        "config": _anthropic_config(server),
        "request": _anthropic_request(),
        "runtime": {"streamProviderMessages": True, "emitEvent": lambda *_: None},
        "maxAttempts": 1,
    }
    args.update(overrides)
    return run(core.sendAnthropicMessagesRequest(args))


def _anthropic_sse(events):
    return "".join(
        f"event: {event.get('type', 'message')}\ndata: {json.dumps(event)}\n\n"
        for event in events
    )


_ANTHROPIC_OK_EVENTS = [
    {
        "type": "message_start",
        "message": {"id": "msg_framing", "model": "claude-sonnet-4-6", "usage": {"input_tokens": 2}},
    },
    {
        "type": "content_block_start",
        "index": 0,
        "content_block": {"type": "text", "text": ""},
    },
    {
        "type": "content_block_delta",
        "index": 0,
        "delta": {"type": "text_delta", "text": "framed"},
    },
    {"type": "content_block_stop", "index": 0},
    {
        "type": "message_delta",
        "delta": {"stop_reason": "end_turn"},
        "usage": {"output_tokens": 1},
    },
    {"type": "message_stop"},
]


class AnthropicChunkedFramingTests(unittest.TestCase):
    def test_chunked_transfer_encoding_stream_completes(self):
        body = _anthropic_sse(_ANTHROPIC_OK_EVENTS)
        pieces = [body[index : index + 40] for index in range(0, len(body), 40)]
        server, _requests = _anthropic_server(
            {
                "messages": [
                    {
                        "contentType": "text/event-stream",
                        "chunked": True,
                        "chunks": [{"raw": piece} for piece in pieces],
                    }
                ]
            }
        )
        try:
            response = _send_anthropic(server)
        finally:
            server.close()
        self.assertEqual(response["content"], [{"type": "text", "text": "framed"}])
        self.assertEqual(response["stopReason"], "end_turn")

    def test_chunked_mid_body_truncation_classifies_transport_retryable(self):
        body = _anthropic_sse(_ANTHROPIC_OK_EVENTS)
        server, _requests = _anthropic_server(
            {
                "messages": [
                    {
                        "contentType": "text/event-stream",
                        "chunked": True,
                        "truncateAfterBytes": 60,
                        "rawBody": body,
                    }
                ]
            }
        )
        try:
            with self.assertRaises(core.ProviderRequestError) as ctx:
                _send_anthropic(server)
        finally:
            server.close()
        self.assertEqual(ctx.exception.record["category"], "transport")
        self.assertTrue(ctx.exception.record["retryable"])

    def test_content_length_mid_body_truncation_matches_chunked_policy(self):
        body = _anthropic_sse(_ANTHROPIC_OK_EVENTS)
        server, _requests = _anthropic_server(
            {
                "messages": [
                    {
                        "contentType": "text/event-stream",
                        "truncateAfterBytes": 60,
                        "rawBody": body,
                    }
                ]
            }
        )
        try:
            with self.assertRaises(core.ProviderRequestError) as ctx:
                _send_anthropic(server)
        finally:
            server.close()
        # Same wire shape, same verdict on both framings: a mid-body cut is a
        # retryable transport failure.
        self.assertEqual(ctx.exception.record["category"], "transport")
        self.assertTrue(ctx.exception.record["retryable"])

    def test_clean_body_without_terminal_event_stays_protocol(self):
        body = _anthropic_sse(_ANTHROPIC_OK_EVENTS[:-1])
        server, _requests = _anthropic_server(
            {"messages": [{"contentType": "text/event-stream", "rawBody": body}]}
        )
        try:
            with self.assertRaises(core.ProviderRequestError) as ctx:
                _send_anthropic(server)
        finally:
            server.close()
        self.assertEqual(ctx.exception.record["category"], "protocol")


class AnthropicSseTextFramingTests(unittest.TestCase):
    def test_crlf_line_endings_parse(self):
        body = _anthropic_sse(_ANTHROPIC_OK_EVENTS).replace("\n", "\r\n")
        server, _requests = _anthropic_server(
            {"messages": [{"contentType": "text/event-stream", "rawBody": body}]}
        )
        try:
            response = _send_anthropic(server)
        finally:
            server.close()
        self.assertEqual(response["content"], [{"type": "text", "text": "framed"}])

    def test_mid_line_fragmentation_across_writes(self):
        body = _anthropic_sse(_ANTHROPIC_OK_EVENTS)
        split = body.index('"text_delta"')
        server, _requests = _anthropic_server(
            {
                "messages": [
                    {
                        "contentType": "text/event-stream",
                        "chunks": [
                            {"raw": body[:split]},
                            {"delayMs": 20, "raw": body[split : split + 7]},
                            {"delayMs": 20, "raw": body[split + 7 :]},
                        ],
                    }
                ]
            }
        )
        try:
            response = _send_anthropic(server)
        finally:
            server.close()
        self.assertEqual(response["content"], [{"type": "text", "text": "framed"}])

    def test_ping_events_and_comment_lines_are_ignored(self):
        events = list(_ANTHROPIC_OK_EVENTS)
        body = (
            ": keep-alive comment\n\n"
            + _anthropic_sse(events[:2])
            + "event: ping\ndata: {\"type\": \"ping\"}\n\n"
            + _anthropic_sse(events[2:])
        )
        server, _requests = _anthropic_server(
            {"messages": [{"contentType": "text/event-stream", "rawBody": body}]}
        )
        try:
            response = _send_anthropic(server)
        finally:
            server.close()
        self.assertEqual(response["content"], [{"type": "text", "text": "framed"}])

    def test_error_event_after_content_classifies_from_error_type(self):
        body = _anthropic_sse(
            _ANTHROPIC_OK_EVENTS[:3]
            + [
                {
                    "type": "error",
                    "error": {
                        "type": "rate_limit_error",
                        "message": "Throttled mid-stream",
                    },
                }
            ]
        )
        server, _requests = _anthropic_server(
            {"messages": [{"contentType": "text/event-stream", "rawBody": body}]}
        )
        try:
            with self.assertRaises(core.ProviderRequestError) as ctx:
                _send_anthropic(server)
        finally:
            server.close()
        record = ctx.exception.record
        self.assertEqual(record["category"], "rate_limit")
        self.assertTrue(record["hadAssistantOutput"])


class OpenAiWireFramingTests(unittest.TestCase):
    def _ok_events(self):
        message = {
            "id": "item_1",
            "type": "message",
            "content": [{"type": "output_text", "text": "framed"}],
        }
        return [
            {"type": "response.created", "response": {"id": "resp_framing"}},
            {
                "type": "response.output_item.added",
                "item": {"id": "item_1", "type": "message"},
            },
            {
                "type": "response.output_text.delta",
                "item_id": "item_1",
                "delta": "framed",
            },
            {"type": "response.output_item.done", "item": message},
            {
                "type": "response.completed",
                "response": {
                    "id": "resp_framing",
                    "output": [message],
                    "usage": {"input_tokens": 1, "output_tokens": 1},
                },
            },
        ]

    def test_chunked_transfer_encoding_stream_completes(self):
        body = openaiResponsesSseBody(self._ok_events())
        pieces = [body[index : index + 48] for index in range(0, len(body), 48)]
        script = {
            "responses": [
                {
                    "contentType": "text/event-stream",
                    "chunked": True,
                    "chunks": [{"raw": piece} for piece in pieces],
                }
            ]
        }
        server, _requests = _openai_server(script)
        try:
            response = run(core.sendOpenAiResponsesRequest(_openai_args(server)))
        finally:
            server.close()
        self.assertEqual(response["content"], [{"type": "text", "text": "framed"}])

    def test_chunked_mid_body_truncation_classifies_transport_retryable(self):
        body = openaiResponsesSseBody(self._ok_events())
        script = {
            "responses": [
                {
                    "contentType": "text/event-stream",
                    "chunked": True,
                    "truncateAfterBytes": 80,
                    "rawBody": body,
                }
            ]
        }
        server, _requests = _openai_server(script)
        try:
            with self.assertRaises(core.ProviderRequestError) as ctx:
                run(core.sendOpenAiResponsesRequest(_openai_args(server)))
        finally:
            server.close()
        self.assertEqual(ctx.exception.record["category"], "transport")
        self.assertTrue(ctx.exception.record["retryable"])

    def test_missing_content_type_header_still_streams(self):
        # The chatgpt.com codex backend sends valid chunked SSE with no
        # content-type header at all — the parser, not the header, decides.
        script = {
            "responses": [
                {
                    "omitContentType": True,
                    "chunked": True,
                    "rawBody": openaiResponsesSseBody(self._ok_events()),
                }
            ]
        }
        server, _requests = _openai_server(script)
        try:
            response = run(core.sendOpenAiResponsesRequest(_openai_args(server)))
        finally:
            server.close()
        self.assertEqual(response["content"], [{"type": "text", "text": "framed"}])

    def test_crlf_line_endings_parse(self):
        body = openaiResponsesSseBody(self._ok_events()).replace("\n", "\r\n")
        script = {
            "responses": [{"contentType": "text/event-stream", "rawBody": body}]
        }
        server, _requests = _openai_server(script)
        try:
            response = run(core.sendOpenAiResponsesRequest(_openai_args(server)))
        finally:
            server.close()
        self.assertEqual(response["content"], [{"type": "text", "text": "framed"}])

    def test_mid_line_fragmentation_across_writes(self):
        body = openaiResponsesSseBody(self._ok_events())
        split = body.index("output_text.delta")
        script = {
            "responses": [
                {
                    "contentType": "text/event-stream",
                    "chunks": [
                        {"raw": body[:split]},
                        {"delayMs": 20, "raw": body[split : split + 9]},
                        {"delayMs": 20, "raw": body[split + 9 :]},
                    ],
                }
            ]
        }
        server, _requests = _openai_server(script)
        try:
            response = run(core.sendOpenAiResponsesRequest(_openai_args(server)))
        finally:
            server.close()
        self.assertEqual(response["content"], [{"type": "text", "text": "framed"}])

    def test_comment_lines_and_ping_frames_are_ignored(self):
        events = self._ok_events()
        body = (
            ": proxy keep-alive\n\n"
            + openaiResponsesSseBody(events[:2])
            + "data: {\"type\": \"ping\"}\n\n"
            + openaiResponsesSseBody(events[2:])
        )
        script = {
            "responses": [{"contentType": "text/event-stream", "rawBody": body}]
        }
        server, _requests = _openai_server(script)
        try:
            response = run(core.sendOpenAiResponsesRequest(_openai_args(server)))
        finally:
            server.close()
        self.assertEqual(response["content"], [{"type": "text", "text": "framed"}])

    def test_error_event_after_content_keeps_output_flags(self):
        events = self._ok_events()[:3] + [
            {"type": "error", "code": "server_error", "message": "upstream blew up"}
        ]
        script = {
            "responses": [
                {
                    "contentType": "text/event-stream",
                    "rawBody": openaiResponsesSseBody(events),
                }
            ]
        }
        server, _requests = _openai_server(script)
        try:
            with self.assertRaises(core.ProviderRequestError) as ctx:
                run(core.sendOpenAiResponsesRequest(_openai_args(server)))
        finally:
            server.close()
        record = ctx.exception.record
        self.assertEqual(record["category"], "server")
        self.assertTrue(record["hadAssistantOutput"])


if __name__ == "__main__":
    unittest.main()
