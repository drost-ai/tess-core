from __future__ import annotations

from tests.support.agent_core_case import *

import gzip
import socket

from agent_core.payloads.bounded_io import AcquisitionLimitError, readResponseTextBoundedSync
from agent_core.acquisition.bounded_web_fetch import WebFetchPolicyError, boundedWebFetch


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
    def test_connect_deadline_is_classified_independently(self):
        observed = {}

        class TimedOutConnection:
            sock = None

            def __init__(self, host, port, timeout):
                observed.update(host=host, port=port, timeout=timeout)

            def connect(self):
                raise socket.timeout("controlled connect deadline")

            def close(self):
                return None

        with mock.patch(
            "agent_core.acquisition.bounded_web_fetch.http.client.HTTPSConnection",
            TimedOutConnection,
        ):
            result = run(
                boundedWebFetch(
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
            readResponseTextBoundedSync(declared, maxBytes=1_000, label="fixture")
        self.assertEqual(declared_error.exception.measuredBytes, 2_000)
        self.assertEqual(declared.read_sizes, [])

        streamed = Response([b"a" * 600, b"b" * 401])
        with self.assertRaises(AcquisitionLimitError) as streamed_error:
            readResponseTextBoundedSync(streamed, maxBytes=1_000, label="fixture")
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
                boundedWebFetch(
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
                boundedWebFetch(
                    url=f"{base}/declared",
                    compressedMaxBytes=64 * 1024,
                    decodedMaxBytes=512 * 1024,
                )
            )
            self.assertEqual(declared["outcome"], "refused")
            self.assertEqual(declared["reason"], "declared_compressed_limit")
            self.assertEqual(declared["compressedBytes"], 0)

            expanded = run(
                boundedWebFetch(
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
                run(boundedWebFetch(url=f"{base}/start", authorizeHop=authorize))
            self.assertEqual(len(hops), 2)
            loop = run(boundedWebFetch(url=f"{base}/loop/0", maxRedirects=2))
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
                boundedWebFetch(
                    url=f"{base}/slow-headers", headersTimeoutMs=30, totalTimeoutMs=1_000
                )
            )
            self.assertEqual(headers["outcome"], "timeout")
            self.assertEqual(headers["reason"], "headers_timeout")
            self.assertEqual(headers["timeoutPhase"], "headers")

            idle = run(
                boundedWebFetch(
                    url=f"{base}/idle", bodyIdleTimeoutMs=30, totalTimeoutMs=1_000
                )
            )
            self.assertEqual(idle["outcome"], "partial")
            self.assertEqual(idle["reason"], "body_idle_timeout")
            self.assertEqual(idle["timeoutPhase"], "body_idle")
            self.assertEqual(idle["body"], "partial")

            total = run(
                boundedWebFetch(
                    url=f"{base}/stream", bodyIdleTimeoutMs=500, totalTimeoutMs=50
                )
            )
            self.assertEqual(total["outcome"], "partial")
            self.assertEqual(total["reason"], "total_timeout")
            self.assertEqual(total["timeoutPhase"], "total")
            self.assertIn("still-streaming", total["body"])

            oversized = run(boundedWebFetch(url=f"{base}/headers", maxHeaderBytes=512))
            self.assertEqual(oversized["outcome"], "refused")
            self.assertEqual(oversized["reason"], "header_limit")

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
            tool = next(tool for tool in core.createBuiltinTools() if tool.name == "WebFetch")
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
