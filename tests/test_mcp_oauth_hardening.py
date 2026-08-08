from __future__ import annotations

from tests.support.agent_core_case import *

from urllib.parse import parse_qs, urlsplit

from agent_core.mcp.auth import (
    McpOAuthRuntime,
    _assert_oauth_url,
    _canonical_resource_uri,
)
from agent_core.mcp.token_store import FileMcpTokenStore, mcpTokenStoreKey


class _AuthServer:
    """Discovery + token endpoints with recording, configurable for abuse."""

    def __init__(self, issuer_override: str | None = None, redirect_metadata: bool = False, authorization_servers: list[str] | None = None):
        owner = self
        self.token_requests: list[dict] = []
        self.issuer_override = issuer_override
        self.redirect_metadata = redirect_metadata
        self.authorization_servers = authorization_servers

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                path = urlsplit(self.path).path
                if path == "/.well-known/oauth-protected-resource/mcp":
                    return self._json(
                        {
                            "resource": f"{owner.origin}/mcp",
                            "authorization_servers": owner.authorization_servers or [owner.origin],
                        }
                    )
                if path == "/.well-known/oauth-authorization-server":
                    if owner.redirect_metadata:
                        self.send_response(302)
                        self.send_header("Location", f"{owner.origin}/elsewhere")
                        self.end_headers()
                        return
                    return self._json(
                        {
                            "issuer": owner.issuer_override or owner.origin,
                            "authorization_endpoint": f"{owner.origin}/authorize",
                            "token_endpoint": f"{owner.origin}/token",
                            "registration_endpoint": f"{owner.origin}/register",
                        }
                    )
                self.send_response(404)
                self.end_headers()

            def do_POST(self):
                path = urlsplit(self.path).path
                raw = self.rfile.read(int(self.headers.get("content-length") or 0))
                if path == "/register":
                    return self._json({"client_id": "dynamic-client"})
                if path == "/token":
                    fields = {key: value[0] for key, value in parse_qs(raw.decode("utf-8")).items()}
                    owner.token_requests.append(fields)
                    return self._json(
                        {"access_token": "token-1", "token_type": "Bearer", "expires_in": 3600}
                    )
                self.send_response(404)
                self.end_headers()

            def _json(self, value, status=200):
                payload = json.dumps(value).encode("utf-8")
                self.send_response(status)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, _format, *_args):
                return

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.origin = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


def _runtime(server: _AuthServer, store: FileMcpTokenStore, callback) -> McpOAuthRuntime:
    config = {
        "name": "hardened",
        "transport": "http",
        "commandOrUrl": f"{server.origin}/mcp",
        "oauth": {},
    }
    return McpOAuthRuntime(
        config=config,
        store=store,
        on_runtime_event=None,
        authorization_callback=callback,
    )


class OAuthDiscoveryHardeningTests(AgentCoreTestCase):
    def test_endpoint_urls_must_be_https_or_loopback(self):
        _assert_oauth_url("https://auth.example/authorize")
        _assert_oauth_url("http://127.0.0.1:8000/authorize")
        _assert_oauth_url("http://localhost/token")
        for url in ("http://auth.example/token", "ftp://auth.example/x", "not-a-url"):
            with self.subTest(url=url):
                with self.assertRaisesRegex(RuntimeError, "HTTPS"):
                    _assert_oauth_url(url)

    def test_canonical_resource_uri(self):
        self.assertEqual(
            _canonical_resource_uri("HTTPS://Mcp.Example.COM:443/mcp/"),
            "https://mcp.example.com/mcp",
        )
        self.assertEqual(
            _canonical_resource_uri("http://127.0.0.1:8123/mcp"),
            "http://127.0.0.1:8123/mcp",
        )

    def test_full_flow_sends_resource_and_state_and_validates_them(self):
        root, home, workspace, previous = self.temp_context()
        server = _AuthServer()
        try:
            store = FileMcpTokenStore(home / "mcp-tokens")
            seen: dict = {}

            def callback(payload):
                seen.update(payload)
                return {"code": "auth-code", "state": payload["state"]}

            runtime = _runtime(server, store, callback)
            run(runtime._authorize(None, f"{server.origin}/mcp"))

            auth_query = parse_qs(urlsplit(seen["authorizationUrl"]).query)
            self.assertEqual(auth_query["resource"], [f"{server.origin}/mcp"])
            self.assertTrue(seen["state"])
            exchange = server.token_requests[-1]
            self.assertEqual(exchange["grant_type"], "authorization_code")
            self.assertEqual(exchange["resource"], f"{server.origin}/mcp")
            record = run(store.get(mcpTokenStoreKey(runtime.config)))
            self.assertEqual(record["accessToken"], "token-1")
        finally:
            server.close()
            self.restore_home(root, previous)

    def test_mismatched_callback_state_is_rejected(self):
        root, home, workspace, previous = self.temp_context()
        server = _AuthServer()
        try:
            store = FileMcpTokenStore(home / "mcp-tokens")
            runtime = _runtime(server, store, lambda payload: {"code": "auth-code", "state": "forged"})
            with self.assertRaisesRegex(RuntimeError, "mismatched state"):
                run(runtime._authorize(None, f"{server.origin}/mcp"))
            self.assertEqual(server.token_requests, [])
        finally:
            server.close()
            self.restore_home(root, previous)

    def test_issuer_mismatch_fails_discovery(self):
        root, home, workspace, previous = self.temp_context()
        server = _AuthServer(issuer_override="https://evil.example")
        try:
            store = FileMcpTokenStore(home / "mcp-tokens")
            runtime = _runtime(server, store, lambda payload: {"code": "x"})
            with self.assertRaisesRegex(RuntimeError, "issuer mismatch"):
                run(runtime._authorize(None, f"{server.origin}/mcp"))
        finally:
            server.close()
            self.restore_home(root, previous)

    def test_redirecting_metadata_endpoint_is_refused(self):
        root, home, workspace, previous = self.temp_context()
        server = _AuthServer(redirect_metadata=True)
        try:
            store = FileMcpTokenStore(home / "mcp-tokens")
            runtime = _runtime(server, store, lambda payload: {"code": "x"})
            with self.assertRaisesRegex(RuntimeError, "redirect"):
                run(runtime._authorize(None, f"{server.origin}/mcp"))
        finally:
            server.close()
            self.restore_home(root, previous)

    def test_non_loopback_http_authorization_server_is_refused(self):
        root, home, workspace, previous = self.temp_context()
        server = _AuthServer(authorization_servers=["http://attacker.example"])
        try:
            store = FileMcpTokenStore(home / "mcp-tokens")
            runtime = _runtime(server, store, lambda payload: {"code": "x"})
            with self.assertRaisesRegex(RuntimeError, "HTTPS"):
                run(runtime._authorize(None, f"{server.origin}/mcp"))
        finally:
            server.close()
            self.restore_home(root, previous)


class TokenStoreReadHardeningTests(AgentCoreTestCase):
    def test_read_rejects_widened_symlinked_and_oversized_records(self):
        root, home, workspace, previous = self.temp_context()
        try:
            store = FileMcpTokenStore(home / "mcp-tokens")
            key = "server\0https://example.test/mcp"
            run(store.set(key, {"accessToken": "secret"}))
            self.assertEqual(run(store.get(key))["accessToken"], "secret")

            record_path = store._path(key)
            record_path.chmod(0o644)
            with self.assertRaisesRegex(RuntimeError, "owner-only"):
                run(store.get(key))
            record_path.chmod(0o600)

            moved = record_path.with_suffix(".real")
            moved.write_bytes(record_path.read_bytes())
            moved.chmod(0o600)
            record_path.unlink()
            record_path.symlink_to(moved)
            with self.assertRaisesRegex(RuntimeError, "not a regular file"):
                run(store.get(key))
            record_path.unlink()

            self.assertIsNone(run(store.get(key)))
        finally:
            self.restore_home(root, previous)
