from __future__ import annotations

from tests.support.agent_core_case import *

import gzip
import socket

import agent_core.tools.web as web_module
from agent_core.payloads.bounded_io import AcquisitionLimitError, read_response_text_bounded_sync
from agent_core.acquisition.bounded_web_fetch import WebFetchPolicyError, bounded_web_fetch


@contextlib.contextmanager
def _serve(callback):
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            try:
                callback(self)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def log_message(self, _format, *_args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


class Package31WebAcquisitionTests(AgentCoreTestCase):
    def setUp(self):
        super().setUp()
        # These fixtures serve from loopback; admitting that requires the
        # explicit private-host opt-in knob.
        env = mock.patch.dict(os.environ, {"AGENT_CORE_WEB_FETCH_PRIVATE_HOST_ALLOWLIST": "127.0.0.1,localhost"})
        env.start()
        self.addCleanup(env.stop)

    def test_connect_deadline_is_classified_independently(self):
        observed = {}

        def timed_out_dial(hostname, port, timeout):
            observed.update(host=hostname, port=port, timeout=timeout)
            raise socket.timeout("controlled connect deadline")

        with mock.patch(
            "agent_core.acquisition.bounded_web_fetch._connect_policy_checked",
            side_effect=timed_out_dial,
        ):
            result = run(
                bounded_web_fetch(
                    url="https://connect-timeout.invalid",
                    connectTimeoutMs=30,
                    headersTimeoutMs=500,
                    totalTimeoutMs=1_000,
                )
            )

        self.assertEqual(result["outcome"], "timeout")
        self.assertEqual(result["reason"], "connect_timeout")
        self.assertEqual(result["timeoutPhase"], "connect")
        self.assertEqual(observed["host"], "connect-timeout.invalid")
        self.assertEqual(observed["timeout"], 0.03)

    def test_private_and_metadata_addresses_are_refused_by_default(self):
        with mock.patch.dict(os.environ):
            os.environ.pop("AGENT_CORE_WEB_FETCH_PRIVATE_HOST_ALLOWLIST", None)
            for url, reason in (
                ("http://169.254.169.254/latest/meta-data/", "metadata_address"),
                ("http://metadata.google.internal/computeMetadata/", "metadata_address"),
                ("http://169.254.7.7/", "link_local_address"),
                ("http://127.0.0.1:9/", "private_address"),
                ("http://[::1]:9/", "private_address"),
                ("http://[::ffff:127.0.0.1]:9/", "private_address"),
                ("http://10.1.2.3/", "private_address"),
                ("http://192.168.0.10/", "private_address"),
            ):
                with self.subTest(url=url):
                    result = run(bounded_web_fetch(url=url, totalTimeoutMs=2_000))
                    self.assertEqual(result["outcome"], "refused")
                    self.assertEqual(result["reason"], reason)
                    self.assertEqual(result["body"], "")

    def test_private_allowlist_admits_loopback_but_never_metadata(self):
        def handler(request):
            request.send_response(200)
            request.send_header("content-type", "text/plain")
            request.end_headers()
            request.wfile.write(b"local ok")

        with _serve(handler) as base:
            admitted = run(bounded_web_fetch(url=base))
            self.assertEqual(admitted["outcome"], "ok")
            self.assertEqual(admitted["body"], "local ok")

        with mock.patch.dict(
            os.environ,
            {"AGENT_CORE_WEB_FETCH_PRIVATE_HOST_ALLOWLIST": "169.254.169.254,169.254.0.0/16,metadata.google.internal"},
        ):
            metadata = run(bounded_web_fetch(url="http://169.254.169.254/"))
            self.assertEqual(metadata["outcome"], "refused")
            self.assertEqual(metadata["reason"], "metadata_address")
            hostname = run(bounded_web_fetch(url="http://metadata.google.internal/"))
            self.assertEqual(hostname["outcome"], "refused")
            self.assertEqual(hostname["reason"], "metadata_address")
            link_local = run(bounded_web_fetch(url="http://169.254.7.7/"))
            self.assertEqual(link_local["outcome"], "refused")
            self.assertEqual(link_local["reason"], "link_local_address")

    def test_redirects_to_blocked_addresses_are_refused_per_hop(self):
        def handler(request):
            request.send_response(302)
            request.send_header("location", "http://169.254.169.254/latest/meta-data/")
            request.end_headers()

        with _serve(handler) as base:
            result = run(bounded_web_fetch(url=f"{base}/start"))
            self.assertEqual(result["outcome"], "refused")
            self.assertEqual(result["reason"], "metadata_address")
            self.assertEqual(len(result["redirects"]), 1)

    def test_resolved_addresses_are_vetted_and_pinned(self):
        def handler(request):
            request.send_response(200)
            request.send_header("content-type", "text/plain")
            request.end_headers()
            request.wfile.write(b"pinned")

        with _serve(handler) as base:
            port = int(base.rsplit(":", 1)[1])
            resolutions = []

            def fake_getaddrinfo(host, requested_port, *args, **kwargs):
                resolutions.append(host)
                return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", requested_port))]

            with mock.patch(
                "agent_core.acquisition.bounded_web_fetch.socket.getaddrinfo",
                side_effect=fake_getaddrinfo,
            ):
                with mock.patch.dict(os.environ):
                    os.environ.pop("AGENT_CORE_WEB_FETCH_PRIVATE_HOST_ALLOWLIST", None)
                    hidden = run(bounded_web_fetch(url=f"http://rebind.test:{port}/"))
                self.assertEqual(hidden["outcome"], "refused")
                self.assertEqual(hidden["reason"], "private_address")

                with mock.patch.dict(
                    os.environ,
                    {"AGENT_CORE_WEB_FETCH_PRIVATE_HOST_ALLOWLIST": "rebind.test"},
                ):
                    resolutions.clear()
                    admitted = run(bounded_web_fetch(url=f"http://rebind.test:{port}/"))
                self.assertEqual(admitted["outcome"], "ok")
                self.assertEqual(admitted["body"], "pinned")
                # One resolution serves both the policy check and the dial;
                # a second lookup would reopen the rebinding window.
                self.assertEqual(resolutions, ["rebind.test"])

    def test_one_shot_api_response_refuses_declared_and_streamed_overflow(self):
        class Response:
            def __init__(self, chunks, content_length=None):
                self.chunks = list(chunks)
                self.headers = (
                    {"content-length": str(content_length)}
                    if content_length is not None
                    else {}
                )
                self.read_sizes = []

            def read(self, size):
                self.read_sizes.append(size)
                if not self.chunks:
                    return b""
                chunk = self.chunks.pop(0)
                return chunk[:size]

        declared = Response([], content_length=2_000)
        with self.assertRaises(AcquisitionLimitError) as declared_error:
            read_response_text_bounded_sync(declared, maxBytes=1_000, label="fixture")
        self.assertEqual(declared_error.exception.measuredBytes, 2_000)
        self.assertEqual(declared.read_sizes, [])

        streamed = Response([b"a" * 600, b"b" * 401])
        with self.assertRaises(AcquisitionLimitError) as streamed_error:
            read_response_text_bounded_sync(streamed, maxBytes=1_000, label="fixture")
        self.assertEqual(streamed_error.exception.measuredBytes, 1_001)
        self.assertTrue(streamed.read_sizes)
        self.assertLessEqual(max(streamed.read_sizes), 1_001)

    def test_decoded_and_compressed_limits_are_independent(self):
        compressed = gzip.compress(b"b" * (256 * 1024))

        def handler(request):
            if request.path == "/declared":
                request.send_response(200)
                request.send_header("content-type", "text/plain")
                request.send_header("content-length", str(1024 * 1024))
                request.end_headers()
                return
            if request.path == "/gzip":
                request.send_response(200)
                request.send_header("content-type", "text/plain")
                request.send_header("content-encoding", "gzip")
                request.send_header("content-length", str(len(compressed)))
                request.end_headers()
                request.wfile.write(compressed)
                return
            request.send_response(200)
            request.send_header("content-type", "text/plain")
            request.end_headers()
            for _ in range(32):
                request.wfile.write(b"a" * 8_192)

        with _serve(handler) as base:
            decoded = run(
                bounded_web_fetch(
                    url=base,
                    compressedMaxBytes=512 * 1024,
                    decodedMaxBytes=32 * 1024,
                )
            )
            self.assertEqual(decoded["outcome"], "partial")
            self.assertEqual(decoded["reason"], "decoded_limit")
            self.assertLessEqual(len(decoded["body"].encode()), 32 * 1024)
            self.assertGreater(decoded["decodedBytes"], 32 * 1024)

            declared = run(
                bounded_web_fetch(
                    url=f"{base}/declared",
                    compressedMaxBytes=64 * 1024,
                    decodedMaxBytes=512 * 1024,
                )
            )
            self.assertEqual(declared["outcome"], "refused")
            self.assertEqual(declared["reason"], "declared_compressed_limit")
            self.assertEqual(declared["compressedBytes"], 0)

            expanded = run(
                bounded_web_fetch(
                    url=f"{base}/gzip",
                    compressedMaxBytes=64 * 1024,
                    decodedMaxBytes=32 * 1024,
                )
            )
            self.assertEqual(expanded["outcome"], "partial")
            self.assertEqual(expanded["reason"], "decoded_limit")
            self.assertLessEqual(expanded["compressedBytes"], 64 * 1024)
            self.assertGreater(expanded["decodedBytes"], 32 * 1024)
            self.assertLessEqual(len(expanded["body"].encode()), 32 * 1024)

    def test_redirects_reauthorize_each_hop_and_stop_at_ceiling(self):
        def handler(request):
            if request.path.startswith("/loop/"):
                ordinal = int(request.path.rsplit("/", 1)[1])
                request.send_response(302)
                request.send_header("location", f"/loop/{ordinal + 1}")
                request.end_headers()
                return
            if request.path == "/start":
                request.send_response(302)
                request.send_header("location", "/target")
                request.end_headers()
                return
            request.send_response(200)
            request.send_header("content-type", "text/plain")
            request.end_headers()
            request.wfile.write(b"target")

        with _serve(handler) as base:
            hops = []

            async def authorize(url):
                hops.append(url)
                if url.endswith("/target"):
                    raise WebFetchPolicyError("target denied")

            with self.assertRaisesRegex(WebFetchPolicyError, "target denied"):
                run(bounded_web_fetch(url=f"{base}/start", authorizeHop=authorize))
            self.assertEqual(len(hops), 2)
            loop = run(bounded_web_fetch(url=f"{base}/loop/0", maxRedirects=2))
            self.assertEqual(loop["outcome"], "refused")
            self.assertEqual(loop["reason"], "redirect_limit")
            self.assertEqual(len(loop["redirects"]), 2)

    def test_header_body_idle_total_deadlines_and_header_ceiling(self):
        def handler(request):
            if request.path == "/slow-headers":
                time.sleep(0.15)
                request.send_response(200)
                request.end_headers()
                request.wfile.write(b"late")
                return
            if request.path == "/idle":
                request.send_response(200)
                request.send_header("content-type", "text/plain")
                request.end_headers()
                request.wfile.write(b"partial")
                request.wfile.flush()
                time.sleep(0.15)
                request.wfile.write(b"late")
                return
            if request.path == "/stream":
                request.send_response(200)
                request.send_header("content-type", "text/plain")
                request.end_headers()
                for _ in range(100):
                    request.wfile.write(b"still-streaming\n")
                    request.wfile.flush()
                    time.sleep(0.01)
                return
            request.send_response(200)
            request.send_header("content-type", "text/plain")
            request.send_header("x-oversized", "h" * 4_096)
            request.end_headers()
            request.wfile.write(b"ignored")

        with _serve(handler) as base:
            headers = run(
                bounded_web_fetch(
                    url=f"{base}/slow-headers", headersTimeoutMs=30, totalTimeoutMs=1_000
                )
            )
            self.assertEqual(headers["outcome"], "timeout")
            self.assertEqual(headers["reason"], "headers_timeout")
            self.assertEqual(headers["timeoutPhase"], "headers")

            idle = run(
                bounded_web_fetch(
                    url=f"{base}/idle", bodyIdleTimeoutMs=30, totalTimeoutMs=1_000
                )
            )
            self.assertEqual(idle["outcome"], "partial")
            self.assertEqual(idle["reason"], "body_idle_timeout")
            self.assertEqual(idle["timeoutPhase"], "body_idle")
            self.assertEqual(idle["body"], "partial")

            total = run(
                bounded_web_fetch(
                    url=f"{base}/stream", bodyIdleTimeoutMs=500, totalTimeoutMs=50
                )
            )
            self.assertEqual(total["outcome"], "partial")
            self.assertEqual(total["reason"], "total_timeout")
            self.assertEqual(total["timeoutPhase"], "total")
            self.assertIn("still-streaming", total["body"])

            oversized = run(bounded_web_fetch(url=f"{base}/headers", maxHeaderBytes=512))
            self.assertEqual(oversized["outcome"], "refused")
            self.assertEqual(oversized["reason"], "header_limit")

    def test_fetch_forwards_hop_authorizer_to_bounded_acquisition(self):
        captured = {}

        async def fake_bounded(**kwargs):
            captured.update(kwargs)
            return {"statusCode": 200, "body": "ok"}

        async def authorize(_url):
            return None

        with mock.patch.object(web_module, "bounded_web_fetch", fake_bounded):
            run(web_module.fetch("https://example.test/", authorizeHop=authorize))
        self.assertIs(captured["authorizeHop"], authorize)

    def test_websearch_scraper_hops_are_gated_by_policy(self):
        def make_context(network: bool, rules: list | None = None) -> dict:
            return {
                "cwd": os.getcwd(),
                "config": {
                    "providerKind": "openai", "model": "tiny",
                    "sandbox": {"network": True}, "settingsSources": [],
                },
                "session": {
                    "id": "search-policy-session", "cwd": os.getcwd(),
                    "metadata": {
                        "effectiveAgentAuthority": {
                            "sandbox": {"network": network},
                            "permissionRules": rules or [],
                            "permissionRuleProjectRoot": os.getcwd(),
                        }
                    },
                },
                "interactive": False, "askQuestion": lambda *_: "",
                "outputJson": lambda *_: None, "abortSignal": None,
            }

        fetched = []

        async def scraper_fetch(url, *, headers=None, signal=None, authorizeHop=None):
            fetched.append(url)
            if authorizeHop is not None:
                await authorizeHop(url)
            return web_module.FetchResponse(200, "<html></html>")

        tool = next(tool for tool in core.create_builtin_tools() if tool.name == "WebSearch")
        with mock.patch.dict(os.environ):
            os.environ.pop("EXA_API_KEY", None)
            os.environ.pop("TESS_WEB_SEARCH_PROVIDER", None)
            with mock.patch.object(web_module, "fetch", scraper_fetch):
                offline = run(tool.handler({"query": "blocked"}, make_context(network=False)))
                self.assertTrue(offline["isError"])
                self.assertRegex(offline["content"], r"network policy is disabled")
                self.assertEqual(fetched, [])

                deny_rule = {
                    "effect": "deny", "tool": "WebFetch",
                    "pattern": "domain:html.duckduckgo.com", "scope": "inline",
                    "raw": "WebFetch(domain:html.duckduckgo.com)", "approved": True,
                }
                denied = run(tool.handler({"query": "blocked"}, make_context(network=True, rules=[deny_rule])))
                self.assertTrue(denied["isError"])
                self.assertRegex(denied["content"], r"blocked by permission rule")
                self.assertEqual(len(fetched), 1)
                self.assertIn("html.duckduckgo.com", fetched[0])

    def test_webfetch_tool_enforces_network_and_redirect_domain_policy(self):
        target_requests = {"count": 0}

        def handler(request):
            if request.path == "/start":
                request.send_response(302)
                request.send_header(
                    "location", f"http://localhost:{request.server.server_address[1]}/target"
                )
                request.end_headers()
                return
            target_requests["count"] += 1
            request.send_response(200)
            request.send_header("content-type", "text/plain")
            request.end_headers()
            request.wfile.write(b"must not be reached")

        with _serve(handler) as base:
            tool = next(tool for tool in core.create_builtin_tools() if tool.name == "WebFetch")
            context = {
                "cwd": os.getcwd(),
                "config": {
                    "providerKind": "openai", "model": "tiny",
                    "sandbox": {"network": True}, "settingsSources": [],
                },
                "session": {
                    "id": "web-policy-session", "cwd": os.getcwd(),
                    "metadata": {
                        "effectiveAgentAuthority": {
                            "sandbox": {"network": True},
                            "permissionRules": [
                                {
                                    "effect": "deny", "tool": "WebFetch",
                                    "pattern": "domain:localhost", "scope": "inline",
                                    "raw": "WebFetch(domain:localhost)", "approved": True,
                                }
                            ],
                            "permissionRuleProjectRoot": os.getcwd(),
                        }
                    },
                },
            }
            result = run(
                tool.handler({"url": f"{base}/start", "prompt": "read"}, context)
            )
            self.assertTrue(result["isError"])
            self.assertRegex(result["content"], r"Redirect hop blocked")
            self.assertEqual(target_requests["count"], 0)

            context["session"]["metadata"]["effectiveAgentAuthority"]["sandbox"]["network"] = False
            blocked = run(
                tool.handler({"url": f"{base}/target", "prompt": "read"}, context)
            )
            self.assertTrue(blocked["isError"])
            self.assertRegex(blocked["content"], r"network policy is disabled")
