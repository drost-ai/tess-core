from __future__ import annotations

from tests.support.tess_core_case import *


class AnthropicProviderStreamingTests(TessCoreTestCase):
    def test_anthropic_streaming_eof_before_message_stop_rejects_partial_content(self):
        calls = {"count": 0}

        class TruncatedStreamingAnthropicHandler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *_args):
                return

            def do_POST(self):
                calls["count"] += 1
                length = int(self.headers.get("content-length") or 0)
                self.rfile.read(length)
                self.send_response(200)
                self.send_header("content-type", "text/event-stream")
                self.end_headers()
                for frame in (
                    'event: message_start\ndata: {"type":"message_start","message":{"model":"claude-sonnet-4-6","usage":{"input_tokens":5}}}\n\n',
                    'event: content_block_start\ndata: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}\n\n',
                    'event: content_block_delta\ndata: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"partial ans"}}\n\n',
                ):
                    self.wfile.write(frame.encode("utf-8"))
                    self.wfile.flush()

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), TruncatedStreamingAnthropicHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with self.assertRaises(core.ProviderIncompleteResponseError) as raised:
                run(
                    core.send_anthropic_messages_request(
                        {
                            "config": {
                                "provider": "anthropic",
                                "anthropicBaseUrl": f"http://127.0.0.1:{server.server_address[1]}/v1",
                                "apiKey": "test-key",
                                "customHeaders": {},
                            },
                            "request": {
                                "model": "claude-sonnet-4-6",
                                "system": "s",
                                "messages": [{"role": "user", "content": "Hello"}],
                                "tools": [],
                            },
                            "runtime": {"includePartialMessages": True},
                            "maxAttempts": 2,
                            "streamIdleTimeoutMs": 2000,
                        }
                    )
                )
            self.assertEqual(raised.exception.details["partialAssistantBytes"], 11)
            self.assertFalse(raised.exception.record["retryable"])
            self.assertEqual(calls["count"], 1)
        finally:
            server.shutdown()
            server.server_close()
