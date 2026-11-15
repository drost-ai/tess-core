from __future__ import annotations

from tests.support.tess_core_case import *


class AnthropicProviderTransportTests(TessCoreTestCase):
    def test_anthropic_blocking_http_transport_abort_returns_before_socket_finishes(self):
        signal = SimpleNamespace(aborted=False)
        request_started = threading.Event()
        request_finished = threading.Event()

        class SlowAnthropicHandler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *_args):
                return

            def do_POST(self):
                request_started.set()
                signal.aborted = True
                time.sleep(0.4)
                body = json.dumps({"model": "claude-sonnet-4-6", "content": [{"type": "text", "text": "late"}]}).encode("utf-8")
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                with contextlib.suppress(BrokenPipeError, ConnectionResetError):
                    self.wfile.write(body)
                request_finished.set()

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), SlowAnthropicHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with self.assertRaises(core.ProviderRequestError) as ctx:
                run(
                    core.send_anthropic_messages_request(
                        {
                            "config": {"provider": "anthropic", "anthropicBaseUrl": f"http://127.0.0.1:{server.server_address[1]}/v1", "apiKey": "test-key", "customHeaders": {}},
                            "request": {"model": "claude-sonnet-4-6", "system": "s", "messages": [{"role": "user", "content": "Hello"}], "tools": []},
                            "runtime": {"abortSignal": signal},
                            "maxAttempts": 2,
                            "jsonResponseTimeoutMs": 1000,
                        }
                    )
                )
            self.assertTrue(request_started.is_set())
            self.assertFalse(request_finished.is_set())
            self.assertEqual(ctx.exception.record["category"], "cancelled")
            self.assertFalse(ctx.exception.record["retryable"])
            request_finished.wait(1)
        finally:
            server.shutdown()
            server.server_close()

    def test_anthropic_http_retry_and_non_retry_classification(self):
        original_delay = anthropic_module.request.anthropic_retry_delay_ms
        anthropic_module.request.anthropic_retry_delay_ms = lambda _attempt, *_a, **_kw: 0
        retry_calls = {"count": 0, "accept": None, "payload": None}

        class RetryHandler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *_args):
                return

            def do_POST(self):
                retry_calls["count"] += 1
                retry_calls["accept"] = self.headers.get("accept")
                length = int(self.headers.get("content-length") or 0)
                retry_calls["payload"] = json.loads(self.rfile.read(length).decode("utf-8"))
                if retry_calls["count"] == 1:
                    body = json.dumps({"error": {"type": "overloaded_error", "message": "temporary"}}).encode("utf-8")
                    self.send_response(500)
                    self.send_header("content-type", "application/json")
                    self.send_header("content-length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                body = json.dumps({"model": "claude-sonnet-4-6", "content": [{"type": "text", "text": "ok"}], "usage": {"input_tokens": 5, "output_tokens": 1}, "stop_reason": "end_turn"}).encode("utf-8")
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        retry_server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), RetryHandler)
        retry_thread = threading.Thread(target=retry_server.serve_forever, daemon=True)
        retry_thread.start()
        try:
            events = []
            request = {"model": "claude-sonnet-4-6", "system": "s", "messages": [{"role": "user", "content": "hello"}], "tools": []}
            config = {"provider": "anthropic", "anthropicBaseUrl": f"http://127.0.0.1:{retry_server.server_address[1]}/v1", "apiKey": "test-key", "customHeaders": {}}
            response = run(core.send_anthropic_messages_request({"config": config, "request": request, "runtime": {"emitEvent": lambda event, data: events.append((event, data))}, "maxAttempts": 2}))
            self.assertEqual(response["content"], [{"type": "text", "text": "ok"}])
            self.assertEqual(retry_calls["count"], 2)
            self.assertEqual(retry_calls["accept"], "application/json")
            self.assertNotIn("stream", retry_calls["payload"])
            self.assertTrue(any(event == "provider.retrying" and data["category"] == "server" for event, data in events))
            self.assertTrue(any(event == "provider.recovered" and data["attempts"] == 2 for event, data in events))
        finally:
            retry_server.shutdown()
            retry_server.server_close()

        auth_calls = {"count": 0}

        class AuthHandler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *_args):
                return

            def do_POST(self):
                auth_calls["count"] += 1
                body = json.dumps({"error": {"type": "authentication_error", "message": "bad key"}}).encode("utf-8")
                self.send_response(401)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        auth_server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), AuthHandler)
        auth_thread = threading.Thread(target=auth_server.serve_forever, daemon=True)
        auth_thread.start()
        try:
            request = {"model": "claude-sonnet-4-6", "system": "s", "messages": [{"role": "user", "content": "hello"}], "tools": []}
            config = {"provider": "anthropic", "anthropicBaseUrl": f"http://127.0.0.1:{auth_server.server_address[1]}/v1", "apiKey": "test-key", "customHeaders": {}}
            with self.assertRaises(core.ProviderRequestError) as auth_ctx:
                run(core.send_anthropic_messages_request({"config": config, "request": request, "runtime": {}, "maxAttempts": 2}))
            self.assertEqual(auth_ctx.exception.record["category"], "auth")
            self.assertEqual(auth_calls["count"], 1)
        finally:
            auth_server.shutdown()
            auth_server.server_close()
            anthropic_module.request.anthropic_retry_delay_ms = original_delay
