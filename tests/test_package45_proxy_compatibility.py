from __future__ import annotations

import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from tess_core_harness.fake_providers.server import FakeProviderHttpServer
from tests.support.tess_core_case import *
from tests.test_package45_transport import _config, _send, _turn


class Package45ProxyCompatibilityTests(unittest.TestCase):
    def test_capability_off_uses_fresh_connection_per_request(self):
        async def exercise():
            requests = []
            server = FakeProviderHttpServer(
                provider="anthropic",
                script={},
                request_recorder=requests,
                record_all_headers=True,
            ).start()
            runtime = {"emitEvent": lambda *_: None}
            config = _config(server, keep_alive=False)
            try:
                turn = _turn(config, runtime, "fresh-wire")
                await _send(config, runtime, turn)
                await _send(config, runtime, turn)
                await turn.close()
            finally:
                server.close()
            return requests

        first, second = run(exercise())
        self.assertEqual(first["path"], second["path"])
        self.assertEqual(first["body"], second["body"])
        self.assertEqual(first["headers"], second["headers"])
        self.assertEqual(first["headers"]["connection"], "close")
        self.assertNotEqual(first["connectionId"], second["connectionId"])

    def test_https_proxy_uses_connect_and_no_proxy_bypasses(self):
        connect_targets = []

        class ConnectProxyHandler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                return

            def do_CONNECT(self):
                connect_targets.append(self.path)
                self.send_error(502, "injected CONNECT stop")

        proxy = ThreadingHTTPServer(("127.0.0.1", 0), ConnectProxyHandler)
        proxy.daemon_threads = True
        thread = threading.Thread(target=proxy.serve_forever, daemon=True)
        thread.start()
        proxy_url = f"http://127.0.0.1:{proxy.server_address[1]}"

        async def exercise_connect():
            config = {
                "provider": "anthropic",
                "anthropicBaseUrl": "https://package45-connect.invalid/v1",
                "apiKey": "package45-key",
                "customHeaders": {},
                "providerCapabilities": {"anthropicHttpKeepAlive": False},
            }
            runtime = {"emitEvent": lambda *_: None}
            turn = _turn(config, runtime, "connect-proxy")
            try:
                with self.assertRaises(core.ProviderRequestError):
                    await _send(config, runtime, turn)
            finally:
                await turn.close()

        try:
            with mock.patch.dict(
                os.environ,
                {
                    "https_proxy": proxy_url,
                    "HTTPS_PROXY": proxy_url,
                    "no_proxy": "",
                    "NO_PROXY": "",
                },
                clear=False,
            ):
                run(exercise_connect())
        finally:
            proxy.shutdown()
            proxy.server_close()
            thread.join(timeout=1)

        self.assertEqual(connect_targets, ["package45-connect.invalid:443"])

        async def exercise_bypass():
            server = FakeProviderHttpServer(
                provider="anthropic", script={}, request_recorder=[]
            ).start()
            runtime = {"emitEvent": lambda *_: None}
            config = _config(server)
            turn = _turn(config, runtime, "no-proxy")
            try:
                response = await _send(config, runtime, turn)
            finally:
                await turn.close()
                server.close()
            return response

        with mock.patch.dict(
            os.environ,
            {
                "http_proxy": "http://127.0.0.1:1",
                "HTTP_PROXY": "http://127.0.0.1:1",
                "no_proxy": "127.0.0.1",
                "NO_PROXY": "127.0.0.1",
            },
            clear=False,
        ):
            bypassed = run(exercise_bypass())
        self.assertEqual(bypassed["stopReason"], "end_turn")
