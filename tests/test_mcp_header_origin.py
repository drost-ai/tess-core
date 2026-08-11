from __future__ import annotations

import http.server
import os
import tempfile
import threading
import unittest
from pathlib import Path

import agent_core as core
from agent_core.authority.mcp_origin_policy import fetchMcpResourceWithBoundCredentials
from agent_core.runtime.secrets import authorizeRuntimeSecretWrite, storeRuntimeSecret


class _Server:
    def __init__(self, handler):
        requests: list[dict] = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                requests.append({"path": self.path, "authorization": self.headers.get("Authorization")})
                handler(self)

            def log_message(self, _format: str, *_args) -> None:
                return

        self.requests = requests
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.origin = f"http://127.0.0.1:{self.server.server_port}"

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


class McpHeaderOriginTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="mcp-origin-")
        self.home = Path(self.temp.name) / "home"
        self.previous_home = os.environ.get("AGENT_CORE_HOME")
        os.environ["AGENT_CORE_HOME"] = str(self.home)
        core.configure_runtime_home(str(self.home))

    async def asyncTearDown(self) -> None:
        core.reset_configured_runtime_home()
        if self.previous_home is None:
            os.environ.pop("AGENT_CORE_HOME", None)
        else:
            os.environ["AGENT_CORE_HOME"] = self.previous_home
        self.temp.cleanup()

    async def secret_header(self, value: str) -> dict:
        purpose = "mcp_header"
        key = "origin-test:authorization"
        return await storeRuntimeSecret(
            purpose=purpose,
            idempotencyKey=key,
            secret=value.encode(),
            authorization=authorizeRuntimeSecretWrite(purpose=purpose, idempotencyKey=key),
        )

    @staticmethod
    def config(origin: str, header: dict) -> dict:
        return {
            "name": "origin-bound",
            "transport": "http",
            "commandOrUrl": f"{origin}/mcp",
            "headers": {"Authorization": header},
        }

    async def test_credentials_reach_same_origin_resources_and_redirects_only(self) -> None:
        def handler(request):
            if request.path == "/start":
                request.send_response(302)
                request.send_header("Location", "/finish")
            else:
                request.send_response(200)
            request.end_headers()
            if request.path != "/start":
                request.wfile.write(b"ok")

        server = _Server(handler)
        try:
            header = await self.secret_header("Bearer same-origin-only")
            response = await fetchMcpResourceWithBoundCredentials(
                config=self.config(server.origin, header),
                uri=f"{server.origin}/start",
            )
            self.assertEqual(await response.text(), "ok")
            self.assertEqual([item["authorization"] for item in server.requests], [
                "Bearer same-origin-only",
                "Bearer same-origin-only",
            ])
        finally:
            server.close()

    async def test_model_selected_foreign_uri_is_rejected_before_network(self) -> None:
        configured = _Server(lambda request: (request.send_response(200), request.end_headers()))
        foreign = _Server(lambda request: (request.send_response(200), request.end_headers()))
        try:
            header = await self.secret_header("Bearer never-forward")
            with self.assertRaisesRegex(ValueError, "origin does not match"):
                await fetchMcpResourceWithBoundCredentials(
                    config=self.config(configured.origin, header),
                    uri=f"{foreign.origin}/steal",
                )
            self.assertEqual(configured.requests, [])
            self.assertEqual(foreign.requests, [])
        finally:
            configured.close()
            foreign.close()

    async def test_cross_origin_redirect_stops_before_target_request(self) -> None:
        foreign = _Server(lambda request: (request.send_response(200), request.end_headers()))

        def redirect(request):
            request.send_response(302)
            request.send_header("Location", f"{foreign.origin}/steal")
            request.end_headers()

        configured = _Server(redirect)
        try:
            header = await self.secret_header("Bearer redirect-secret")
            with self.assertRaisesRegex(ValueError, "redirect left"):
                await fetchMcpResourceWithBoundCredentials(
                    config=self.config(configured.origin, header),
                    uri=f"{configured.origin}/redirect",
                )
            self.assertEqual(configured.requests[0]["authorization"], "Bearer redirect-secret")
            self.assertEqual(foreign.requests, [])
        finally:
            configured.close()
            foreign.close()

    async def test_embedded_credentials_malformed_redirects_and_loops_fail_closed(self) -> None:
        def handler(request):
            request.send_response(302)
            if request.path != "/missing":
                request.send_header("Location", "/loop")
            request.end_headers()

        server = _Server(handler)
        try:
            header = await self.secret_header("Bearer bounded")
            config = self.config(server.origin, header)
            with self.assertRaisesRegex(ValueError, "Invalid MCP resource URL origin"):
                await fetchMcpResourceWithBoundCredentials(
                    config=config,
                    uri=server.origin.replace("http://", "http://user:pass@") + "/resource",
                )
            self.assertEqual(server.requests, [])
            with self.assertRaisesRegex(ValueError, "omitted Location"):
                await fetchMcpResourceWithBoundCredentials(config=config, uri=f"{server.origin}/missing")
            with self.assertRaisesRegex(ValueError, "redirect limit exceeded"):
                await fetchMcpResourceWithBoundCredentials(
                    config=config,
                    uri=f"{server.origin}/loop",
                    maxRedirects=1,
                )
        finally:
            server.close()


if __name__ == "__main__":
    unittest.main()
