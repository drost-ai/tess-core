from __future__ import annotations

import hashlib
import copy
import stat
from urllib.parse import parse_qs, urlsplit

from tests.support.tess_core_case import *


ACCESS_ONE = "access-secret-one"
ACCESS_TWO = "access-secret-two"
REFRESH_ONE = "refresh-secret-one"
REFRESH_TWO = "refresh-secret-two"
CLIENT_SECRET = "client-secret-sentinel"
AUTH_CODE = "authorization-code-sentinel"


class MemoryTokenStore:
    def __init__(self):
        self.records = {}
        self.get_count = 0
        self.set_count = 0
        self.delete_count = 0

    async def get(self, key):
        self.get_count += 1
        value = self.records.get(key)
        return copy.deepcopy(value) if value is not None else None

    async def set(self, key, value):
        self.set_count += 1
        self.records[key] = copy.deepcopy(value)

    async def delete(self, key):
        self.delete_count += 1
        self.records.pop(key, None)


class OAuthFixtureServer:
    def __init__(self):
        self.exchange_count = 0
        self.refresh_count = 0
        self.registration_count = 0
        self.initialize_count = 0
        self.tool_count = 0
        self.expected_challenge = None
        self.accepted_tokens = set()
        self.protocol_headers = []
        owner = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                path = urlsplit(self.path).path
                if path == "/.well-known/oauth-protected-resource/mcp":
                    return self._json({"resource": f"{owner.origin}/mcp", "authorization_servers": [owner.origin]})
                if path in ("/.well-known/oauth-authorization-server", "/.well-known/openid-configuration"):
                    return self._json(
                        {
                            "issuer": owner.origin,
                            "authorization_endpoint": f"{owner.origin}/authorize",
                            "token_endpoint": f"{owner.origin}/token",
                            "registration_endpoint": f"{owner.origin}/register",
                            "code_challenge_methods_supported": ["S256"],
                        }
                    )
                self.send_response(404)
                self.end_headers()

            def do_POST(self):
                path = urlsplit(self.path).path
                length = int(self.headers.get("content-length") or 0)
                raw = self.rfile.read(length)
                if path == "/register":
                    owner.registration_count += 1
                    registration = json.loads(raw.decode("utf-8"))
                    if registration.get("token_endpoint_auth_method") != "none":
                        return self._json({"error": "bad registration"}, status=400)
                    return self._json({"client_id": "dynamic-client", "client_secret": "dynamic-secret"})
                if path == "/token":
                    fields = parse_qs(raw.decode("utf-8"))
                    grant = fields.get("grant_type", [""])[0]
                    if grant == "authorization_code":
                        owner.exchange_count += 1
                        verifier = fields.get("code_verifier", [""])[0]
                        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).decode("ascii").rstrip("=")
                        if fields.get("code", [""])[0] != AUTH_CODE or challenge != owner.expected_challenge:
                            return self._json({"error": "invalid_grant"}, status=400)
                        owner.accepted_tokens.add(ACCESS_ONE)
                        return self._json(
                            {
                                "access_token": ACCESS_ONE,
                                "refresh_token": REFRESH_ONE,
                                "token_type": "Bearer",
                                "expires_in": 3600,
                                "scope": "tools",
                            }
                        )
                    if grant == "refresh_token":
                        owner.refresh_count += 1
                        if fields.get("refresh_token", [""])[0] != REFRESH_ONE:
                            return self._json({"error": "invalid_refresh"}, status=400)
                        owner.accepted_tokens.add(ACCESS_TWO)
                        return self._json(
                            {
                                "access_token": ACCESS_TWO,
                                "refresh_token": REFRESH_TWO,
                                "token_type": "Bearer",
                                "expires_in": 3600,
                            }
                        )
                    return self._json({"error": "unsupported_grant"}, status=400)
                if path == "/bare":
                    self.send_response(401)
                    self.end_headers()
                    return
                if path != "/mcp":
                    self.send_response(404)
                    self.end_headers()
                    return
                authorization = self.headers.get("authorization") or ""
                token = authorization.removeprefix("Bearer ")
                if token not in owner.accepted_tokens:
                    self.send_response(401)
                    self.send_header(
                        "WWW-Authenticate",
                        f'Bearer resource_metadata="{owner.origin}/.well-known/oauth-protected-resource/mcp"',
                    )
                    self.end_headers()
                    return
                request = json.loads(raw.decode("utf-8"))
                method = request.get("method")
                request_id = request.get("id")
                if method != "initialize":
                    owner.protocol_headers.append(self.headers.get("MCP-Protocol-Version"))
                if request_id is None:
                    self.send_response(202)
                    self.end_headers()
                    return
                if method == "initialize":
                    owner.initialize_count += 1
                    result = {
                        "protocolVersion": "2025-06-18",
                        "serverInfo": {"name": "oauth-fixture"},
                        "capabilities": {"tools": {"listChanged": True}},
                    }
                elif method == "tools/list":
                    result = {
                        "tools": [
                            {
                                "name": "read",
                                "inputSchema": {"type": "object"},
                                "annotations": {"readOnlyHint": True, "destructiveHint": False},
                            }
                        ]
                    }
                elif method == "resources/list":
                    result = {"resources": []}
                elif method == "tools/call":
                    owner.tool_count += 1
                    result = {
                        "content": [{"type": "text", "text": "safe result"}],
                        "structuredContent": {"ok": True, "call": owner.tool_count},
                    }
                else:
                    result = {}
                return self._json({"jsonrpc": "2.0", "id": request_id, "result": result})

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


class Package28McpOAuthTests(TessCoreTestCase):
    def test_oauth_pkce_exchange_forced_401_refresh_single_flight_and_secret_redaction(self):
        fixture = OAuthFixtureServer()
        store = MemoryTokenStore()
        events = []
        runtime = None
        try:
            config = {
                "name": "oauth",
                "transport": "http",
                "commandOrUrl": f"{fixture.origin}/mcp",
                "oauth": {
                    "clientId": "configured-client",
                    "clientSecret": CLIENT_SECRET,
                    "scopes": ["tools"],
                },
            }

            async def authorize(payload):
                values = parse_qs(urlsplit(payload["authorizationUrl"]).query)
                self.assertEqual(values["code_challenge_method"], ["S256"])
                self.assertEqual(values["client_id"], ["configured-client"])
                self.assertTrue(values.get("state", [""])[0])
                fixture.expected_challenge = values["code_challenge"][0]
                return {"code": AUTH_CODE}

            runtime = run(
                core.create_mcp_runtime(
                    cwd="/tmp",
                    servers=[config],
                    mcpTokenStore=store,
                    mcpAuthorizationCallback=authorize,
                    onRuntimeEvent=lambda event, data: events.append((event, data)),
                )
            )
            self.assertEqual(runtime["listServerStatus"]()[0]["state"], "connected")
            self.assertEqual(runtime["listServerStatus"]()[0]["protocolVersion"], "2025-06-18")
            self.assertEqual(fixture.exchange_count, 1)
            self.assertEqual(fixture.registration_count, 0)
            self.assertTrue(all(header == "2025-06-18" for header in fixture.protocol_headers))
            key = core.mcp_token_store_key(config)
            self.assertEqual(store.records[key]["refreshToken"], REFRESH_ONE)

            # Force two concurrent 401s. Both calls share one refresh and see
            # the rotated token only after it has been persisted.
            fixture.accepted_tokens.discard(ACCESS_ONE)
            async def concurrent_calls():
                return await asyncio.gather(
                    runtime["callTool"]("oauth", "read", {"index": 1}),
                    runtime["callTool"]("oauth", "read", {"index": 2}),
                )

            results = run(concurrent_calls())
            self.assertEqual(fixture.refresh_count, 1)
            self.assertEqual(store.records[key]["accessToken"], ACCESS_TWO)
            self.assertEqual(store.records[key]["refreshToken"], REFRESH_TWO)
            self.assertTrue(all(result["structuredContent"]["ok"] for result in results))

            observable = json.dumps(
                {
                    "events": events,
                    "status": runtime["listServerStatus"](),
                    "diagnostics": runtime["getDiagnostics"](),
                    "results": results,
                },
                sort_keys=True,
            )
            for sentinel in (ACCESS_ONE, ACCESS_TWO, REFRESH_ONE, REFRESH_TWO, CLIENT_SECRET, AUTH_CODE):
                self.assertNotIn(sentinel, observable)
            authorization_event = next(data for event, data in events if event == "mcp.authorization_required")
            self.assertIn("continuation_id", authorization_event)
            authorization_query = parse_qs(urlsplit(authorization_event["authorization_url"]).query)
            self.assertEqual(authorization_query["code_challenge_method"], ["S256"])
        finally:
            if runtime:
                run(runtime["close"]())
            fixture.close()

    def test_headless_authorization_fails_fast_and_recovers_after_token_injection(self):
        fixture = OAuthFixtureServer()
        store = MemoryTokenStore()
        events = []
        runtime = None
        try:
            config = {
                "name": "headless",
                "transport": "http",
                "commandOrUrl": f"{fixture.origin}/mcp",
                "oauth": {"clientId": "headless-client"},
            }
            runtime = run(
                core.create_mcp_runtime(
                    cwd="/tmp",
                    servers=[config],
                    mcpTokenStore=store,
                    onRuntimeEvent=lambda event, data: events.append((event, data)),
                )
            )
            status = runtime["listServerStatus"]()[0]
            self.assertEqual(status["state"], "authorization_required")
            self.assertFalse(status["connected"])
            self.assertNotIn("?", status["authorizationUrl"])
            with self.assertRaisesRegex(RuntimeError, "requires authorization"):
                run(runtime["callTool"]("headless", "read", {}))

            key = core.mcp_token_store_key(config)
            fixture.accepted_tokens.add("headless-injected-token")
            run(
                store.set(
                    key,
                    {
                        "accessToken": "headless-injected-token",
                        "tokenType": "Bearer",
                        "resource": fixture.origin,
                    },
                )
            )
            recovered = run(runtime["callTool"]("headless", "read", {}))
            self.assertIn("safe result", recovered["content"])
            status = runtime["listServerStatus"]()[0]
            self.assertEqual(status["state"], "connected")
            self.assertIsNone(status["authorizationUrl"])
            self.assertTrue(any(event == "mcp.authorization_required" for event, _data in events))
        finally:
            if runtime:
                run(runtime["close"]())
            fixture.close()

    def test_dynamic_registration_and_canonical_origin_token_binding(self):
        fixture = OAuthFixtureServer()
        store = MemoryTokenStore()
        runtime = None
        try:
            config = {
                "name": "dynamic",
                "transport": "http",
                "commandOrUrl": f"{fixture.origin}/mcp",
                "oauth": {"scopes": ["tools"]},
            }

            async def authorize(payload):
                values = parse_qs(urlsplit(payload["authorizationUrl"]).query)
                fixture.expected_challenge = values["code_challenge"][0]
                self.assertEqual(values["client_id"], ["dynamic-client"])
                return AUTH_CODE

            runtime = run(
                core.create_mcp_runtime(
                    cwd="/tmp",
                    servers=[config],
                    mcpTokenStore=store,
                    mcpAuthorizationCallback=authorize,
                )
            )
            self.assertEqual(fixture.registration_count, 1)
            key = core.mcp_token_store_key(config)
            self.assertEqual(store.records[key]["clientId"], "dynamic-client")
            auth = core.create_mcp_oauth_runtime(config=config, store=store)
            self.assertIn("Authorization", run(auth.authorize_headers(f"{fixture.origin}/mcp")))
            self.assertEqual(run(auth.authorize_headers("https://attacker.example/mcp")), {})
            run(auth.close())
        finally:
            if runtime:
                run(runtime["close"]())
            fixture.close()

    def test_bare_401_without_oauth_metadata_does_not_enter_authorization_state(self):
        fixture = OAuthFixtureServer()
        runtime = None
        try:
            runtime = run(
                core.create_mcp_runtime(
                    cwd="/tmp",
                    servers=[
                        {
                            "name": "static",
                            "transport": "http",
                            "commandOrUrl": f"{fixture.origin}/bare",
                            "headers": {"Authorization": "Static configured-value"},
                        }
                    ],
                    mcpTokenStore=MemoryTokenStore(),
                )
            )
            status = runtime["listServerStatus"]()[0]
            self.assertNotEqual(status["state"], "authorization_required")
            self.assertIn("HTTP 401", status["error"])
        finally:
            if runtime:
                run(runtime["close"]())
            fixture.close()

    def test_file_token_store_permissions_atomicity_and_injected_store_exclusivity(self):
        with tempfile.TemporaryDirectory(prefix="tess-core-p28-token-store-") as root:
            store_dir = Path(root) / "tokens"
            store = core.create_file_mcp_token_store(store_dir)
            key = "server\0https://example.test/mcp"
            record = {"accessToken": ACCESS_ONE, "refreshToken": REFRESH_ONE}
            run(store.set(key, record))
            self.assertEqual(run(store.get(key)), record)
            self.assertEqual(stat.S_IMODE(store_dir.stat().st_mode), 0o700)
            files = list(store_dir.iterdir())
            self.assertEqual(len(files), 1)
            self.assertEqual(stat.S_IMODE(files[0].stat().st_mode), 0o600)
            self.assertFalse(any(path.suffix == ".tmp" for path in files))
            run(store.delete(key))
            self.assertIsNone(run(store.get(key)))

        fixture = OAuthFixtureServer()
        runtime = None
        with tempfile.TemporaryDirectory(prefix="tess-core-p28-injected-") as runtime_home:
            core.configure_runtime_home(runtime_home)
            try:
                injected = MemoryTokenStore()
                runtime = run(
                    core.create_mcp_runtime(
                        cwd="/tmp",
                        servers=[
                            {
                                "name": "injected",
                                "transport": "http",
                                "commandOrUrl": f"{fixture.origin}/mcp",
                                "oauth": {"clientId": "headless"},
                            }
                        ],
                        mcpTokenStore=injected,
                    )
                )
                self.assertFalse((Path(runtime_home) / "mcp-tokens").exists())
            finally:
                if runtime:
                    run(runtime["close"]())
                core.reset_configured_runtime_home()
                fixture.close()

    def test_oauth_close_settles_in_flight_refresh_waiters(self):
        async def exercise():
            config = {
                "name": "closing",
                "transport": "http",
                "commandOrUrl": "https://example.test/mcp",
                "oauth": {"clientId": "client"},
            }
            store = MemoryTokenStore()
            await store.set(
                core.mcp_token_store_key(config),
                {
                    "accessToken": ACCESS_ONE,
                    "refreshToken": REFRESH_ONE,
                    "expiresAt": 0,
                    "resource": "https://example.test",
                },
            )
            auth = core.create_mcp_oauth_runtime(config=config, store=store)
            started = asyncio.Event()
            cleaned = asyncio.Event()

            async def blocked_discovery(_challenge, _resource):
                started.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    cleaned.set()

            auth._discover_authorization_server = blocked_discovery
            waiter = asyncio.create_task(auth.authorize_headers("https://example.test/mcp"))
            await started.wait()
            await auth.close()
            with self.assertRaises(asyncio.CancelledError):
                await waiter
            await asyncio.wait_for(cleaned.wait(), 1)

        run(exercise())
