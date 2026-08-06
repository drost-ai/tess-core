from __future__ import annotations

from tests.support.agent_core_case import *

import agent_core.providers.anthropic.request as anthropic_request_module
from agent_core.providers.anthropic_cache import countAnthropicCacheControls
from agent_core.providers.anthropic.sse import normalizeAnthropicSseResponse
from agent_core.runtime.paths import resolveRuntimePaths
from agent_core.providers.usage_costs import estimateUsageCost


class AnthropicPromptCacheTransportTests(AgentCoreTestCase):
    def setUp(self):
        self.root, self.home, self.workspace, self.previous = self.temp_context()

    def tearDown(self):
        self.restore_home(self.root, self.previous)

    def _config(self):
        return {
            "cwd": str(self.workspace),
            "runtimeHome": str(self.home),
            "provider": "anthropic",
            "providerKind": "anthropic",
            "providerProfileName": "official",
            "providerProfile": {"kind": "anthropic", "name": "official", "auth": {}},
            "apiKey": "test-key",
            "customHeaders": {},
            "model": "claude-opus-4-8",
            "promptCache": {
                "enabled": True,
                "mode": "explicit",
                "ttl": "1h",
                "providerDiagnostics": True,
            },
        }

    def test_json_transport_attaches_terminal_diagnostics_observation(self):
        config = self._config()
        paths = resolveRuntimePaths(candidate=str(self.home))
        request = {
            "model": "claude-opus-4-8",
            "system": "stable system",
            "messages": [{"role": "user", "content": "hello"}],
            "tools": [],
            "maxOutputTokens": 100,
            "promptCache": {
                **config["promptCache"],
                "dispatchContext": {
                    "sessionId": "transport-session",
                    "paths": paths,
                    "dispatchOrder": {
                        "runtimeGeneration": 1,
                        "messageCount": 1,
                        "reactiveAttempt": 0,
                        "startedAtMs": 1,
                        "localSequence": 1,
                        "nonce": "transport-nonce",
                    },
                    "preparation": "normal",
                },
            },
        }
        captured = {}

        async def fake_send(_config, payload, _timeout, _stream, header_features, turn_session=None):
            captured["payload"] = payload
            captured["headerFeatures"] = header_features
            return (
                json.dumps(
                    {
                        "id": "msg_transport",
                        "diagnostics": None,
                        "model": "claude-opus-4-8",
                        "content": [{"type": "text", "text": "ok"}],
                        "usage": {
                            "input_tokens": 10,
                            "output_tokens": 2,
                            "cache_creation_input_tokens": 4,
                            "cache_creation": {
                                "ephemeral_5m_input_tokens": 1,
                                "ephemeral_1h_input_tokens": 3,
                            },
                        },
                        "stop_reason": "end_turn",
                    }
                ),
                "application/json",
            )

        with mock.patch.object(anthropic_request_module, "_send_anthropic_messages_async", fake_send):
            response = run(
                anthropic_request_module.sendAnthropicMessagesRequest(
                    {"config": config, "request": request, "runtime": {}, "maxAttempts": 1}
                )
            )
        self.assertEqual(captured["headerFeatures"], {"providerDiagnostics": True})
        self.assertEqual(captured["payload"]["diagnostics"], {"previous_message_id": None})
        self.assertEqual(response["promptCache"]["responseId"], "msg_transport")
        self.assertEqual(response["promptCache"]["providerDiagnosis"], "warmup_no_comparison")
        self.assertEqual(response["usage"]["cache_creation_1h_input_tokens"], 3)
        self.assertTrue(response["usage"]["cache_creation_ttl_detail_complete"])

    def test_stream_transport_preserves_ttl_detail_without_leaking_internal_metadata(self):
        raw = "\n\n".join(
            [
                'event: message_start\ndata: {"type":"message_start","message":{"id":"msg_stream","model":"claude-opus-4-8","diagnostics":null,"usage":{"input_tokens":10,"cache_creation_input_tokens":4,"cache_creation":{"ephemeral_5m_input_tokens":1,"ephemeral_1h_input_tokens":3}}}}',
                'event: content_block_start\ndata: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":"done"}}',
                'event: content_block_stop\ndata: {"type":"content_block_stop","index":0}',
                'event: message_delta\ndata: {"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":2}}',
                'event: message_stop\ndata: {"type":"message_stop"}',
            ]
        )
        internal = normalizeAnthropicSseResponse(
            raw,
            {"model": "claude-opus-4-8", "messages": [], "tools": []},
            {"_capturePromptCacheMetadata": True},
        )
        self.assertEqual(internal["usage"]["cache_creation_5m_input_tokens"], 1)
        self.assertEqual(internal["usage"]["cache_creation_1h_input_tokens"], 3)
        self.assertTrue(internal["usage"]["cache_creation_ttl_detail_complete"])
        self.assertEqual(internal["_anthropicResponseId"], "msg_stream")
        self.assertTrue(internal["_anthropicDiagnosticsFieldPresent"])
        public = normalizeAnthropicSseResponse(
            raw,
            {"model": "claude-opus-4-8", "messages": [], "tools": []},
        )
        self.assertFalse(any(key.startswith("_anthropic") for key in public))

    def test_local_compatibility_capture_c5_is_inert_and_accepts_aggregate_usage(self):
        captures = []

        class FixtureResponse:
            headers = {"content-type": "application/json"}

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            def read(self):
                return json.dumps(
                    {
                        "model": "compatible-model",
                        "content": [{"type": "text", "text": "ok"}],
                        "stop_reason": "end_turn",
                        "usage": {
                            "input_tokens": 10,
                            "output_tokens": 2,
                            "cache_creation_input_tokens": 100,
                            "unknown_provider_usage": 999,
                        },
                        "unknown_response_field": {"future": True},
                    }
                ).encode("utf-8")

        def fixture_urlopen(request, **_kwargs):
            captures.append(
                {
                    "url": request.full_url,
                    "headers": {key.lower(): value for key, value in request.header_items()},
                    "payload": json.loads(request.data.decode("utf-8")),
                }
            )
            return FixtureResponse()

        base_request = {
            "model": "compatible-model",
            "system": "stable",
            "messages": [{"role": "user", "content": "one"}, {"role": "user", "content": "volatile"}],
            "tools": [{"name": "Read", "description": "read", "input_schema": {"type": "object"}}],
            "maxOutputTokens": 100,
        }
        kimi = {
            **self._config(),
            "model": "compatible-model",
            "anthropicBaseUrl": "https://api.kimi.com/coding/",
            "promptCache": {"enabled": True, "mode": "auto", "providerDiagnostics": True},
        }
        compatible = {
            **self._config(),
            "model": "compatible-model",
            "anthropicBaseUrl": "https://compatible.example/v1",
            "customHeaders": {"Anthropic-Beta": "cache-diagnosis-2026-04-07,cache-scope-future-token"},
            "promptCache": {
                "enabled": True, "mode": "explicit", "explicit": True,
                "providerDiagnostics": True, "scope": "global",
            },
        }
        with mock.patch.object(anthropic_request_module, "urlopen", fixture_urlopen):
            first = run(anthropic_request_module.sendAnthropicMessagesRequest(
                {"config": kimi, "request": base_request, "runtime": {}, "maxAttempts": 1}
            ))
            second = run(anthropic_request_module.sendAnthropicMessagesRequest(
                {"config": compatible, "request": base_request, "runtime": {}, "maxAttempts": 1}
            ))
        self.assertEqual(
            [capture["url"] for capture in captures],
            [
                "https://api.kimi.com/coding/v1/messages?beta=true",
                "https://compatible.example/v1/messages?beta=true",
            ],
        )
        self.assertEqual(
            captures[0]["headers"]["anthropic-beta"],
            "claude-code-20250219,fine-grained-tool-streaming-2025-05-14",
        )
        self.assertEqual(
            captures[1]["headers"]["anthropic-beta"],
            (
                "fine-grained-tool-streaming-2025-05-14,"
                "cache-diagnosis-2026-04-07,cache-scope-future-token"
            ),
        )
        self.assertEqual(countAnthropicCacheControls(captures[0]["payload"]), 0)
        self.assertNotIn("diagnostics", captures[0]["payload"])
        self.assertEqual(countAnthropicCacheControls(captures[1]["payload"]), 1)
        self.assertNotIn("diagnostics", captures[1]["payload"])
        self.assertNotIn("global", json.dumps(captures[1]["payload"]))
        self.assertIn("cache-scope-future-token", captures[1]["headers"]["anthropic-beta"])
        self.assertFalse(first["usage"]["cache_creation_ttl_detail_complete"])
        self.assertFalse(second["usage"]["cache_creation_ttl_detail_complete"])
        self.assertNotIn("unknown_provider_usage", second["usage"])
        self.assertNotIn("unknown_response_field", second)
        pricing = estimateUsageCost(second["usage"], "claude-sonnet-4-6")
        self.assertEqual(pricing["cacheWriteUsd"], 0.000375)
        self.assertEqual(pricing["totalUsd"], 0.000435)
        self.assertTrue(pricing["cacheWriteEstimated"])

    def test_loopback_compatibility_capture_c5_uses_real_http_wire(self):
        captures = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *_args):
                return

            def do_POST(self):
                length = int(self.headers.get("content-length") or 0)
                captures.append(
                    {
                        "path": self.path,
                        "headers": {
                            key.lower(): value for key, value in self.headers.items()
                        },
                        "payload": json.loads(
                            self.rfile.read(length).decode("utf-8")
                        ),
                    }
                )
                body = json.dumps(
                    {
                        "model": "compatible-model",
                        "content": [{"type": "text", "text": "ok"}],
                        "stop_reason": "end_turn",
                        "usage": {
                            "input_tokens": 10,
                            "output_tokens": 2,
                            "cache_creation_input_tokens": 100,
                            "unknown_provider_usage": 999,
                        },
                        "unknown_response_field": {"future": True},
                    }
                ).encode("utf-8")
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base_url = f"http://127.0.0.1:{server.server_address[1]}/v1"
            request = {
                "model": "compatible-model",
                "system": "stable",
                "messages": [
                    {"role": "user", "content": "one"},
                    {"role": "user", "content": "volatile"},
                ],
                "tools": [
                    {
                        "name": "Read",
                        "description": "read",
                        "input_schema": {"type": "object"},
                    }
                ],
                "maxOutputTokens": 100,
            }
            suppressed = {
                **self._config(),
                "model": "compatible-model",
                "anthropicBaseUrl": base_url,
                "promptCache": {
                    "enabled": True,
                    "mode": "auto",
                    "compatibility": "claude_code",
                    "providerDiagnostics": True,
                },
            }
            explicit = {
                **self._config(),
                "model": "compatible-model",
                "anthropicBaseUrl": base_url,
                "customHeaders": {
                    "Anthropic-Beta": (
                        "cache-diagnosis-2026-04-07,"
                        "cache-scope-future-token"
                    )
                },
                "promptCache": {
                    "enabled": True,
                    "mode": "explicit",
                    "explicit": True,
                    "compatibility": "compatible",
                    "providerDiagnostics": True,
                    "scope": "global",
                },
            }
            first = run(
                anthropic_request_module.sendAnthropicMessagesRequest(
                    {
                        "config": suppressed,
                        "request": request,
                        "runtime": {},
                        "maxAttempts": 1,
                    }
                )
            )
            second = run(
                anthropic_request_module.sendAnthropicMessagesRequest(
                    {
                        "config": explicit,
                        "request": request,
                        "runtime": {},
                        "maxAttempts": 1,
                    }
                )
            )
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

        self.assertEqual(
            [capture["path"] for capture in captures],
            ["/v1/messages?beta=true"] * 2,
        )
        for capture in captures:
            self.assertEqual(capture["headers"].get("x-api-key"), "test-key")
            self.assertEqual(capture["headers"].get("content-type"), "application/json")
            self.assertEqual(capture["headers"].get("anthropic-version"), "2023-06-01")
        self.assertEqual(
            captures[0]["headers"]["anthropic-beta"],
            "fine-grained-tool-streaming-2025-05-14",
        )
        self.assertEqual(
            captures[1]["headers"]["anthropic-beta"],
            (
                "fine-grained-tool-streaming-2025-05-14,"
                "cache-diagnosis-2026-04-07,cache-scope-future-token"
            ),
        )
        self.assertEqual(countAnthropicCacheControls(captures[0]["payload"]), 0)
        self.assertNotIn("diagnostics", captures[0]["payload"])
        self.assertEqual(countAnthropicCacheControls(captures[1]["payload"]), 1)
        self.assertNotIn("diagnostics", captures[1]["payload"])
        self.assertNotIn("global", json.dumps(captures[1]["payload"]))
        self.assertIn(
            "cache-scope-future-token",
            captures[1]["headers"]["anthropic-beta"],
        )
        self.assertFalse(first["usage"]["cache_creation_ttl_detail_complete"])
        self.assertFalse(second["usage"]["cache_creation_ttl_detail_complete"])
        self.assertEqual(first["usage"]["cache_creation_input_tokens"], 100)
        self.assertEqual(second["usage"]["cache_creation_input_tokens"], 100)
        self.assertNotIn("unknown_provider_usage", second["usage"])
        self.assertNotIn("unknown_response_field", second)
        pricing = estimateUsageCost(second["usage"], "claude-sonnet-4-6")
        self.assertEqual(pricing["cacheWriteUsd"], 0.000375)
        self.assertEqual(pricing["totalUsd"], 0.000435)
        self.assertTrue(pricing["cacheWriteEstimated"])


if __name__ == "__main__":
    unittest.main()
