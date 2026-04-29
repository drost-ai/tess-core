from __future__ import annotations

from tests.support.agent_core_case import *


class ProviderErrorTests(AgentCoreTestCase):
    def test_provider_request_watchdog_parity_surface(self):
        import agent_core.provider_watchdog as provider_watchdog

        watchdog = provider_watchdog.createProviderRequestWatchdog(
            {"initialTimeoutMs": 1, "buildTimeoutError": lambda: TimeoutError("timed out")}
        )
        with self.assertRaises(TimeoutError):
            run(watchdog.race(asyncio.sleep(0.05)))
        self.assertTrue(watchdog.didTimeout())
        self.assertTrue(watchdog.signal.aborted)
        self.assertIsInstance(watchdog.signal.reason, TimeoutError)
        watchdog.dispose()

        parent_reason = RuntimeError("cancelled by parent")
        parent = SimpleNamespace(aborted=True, reason=parent_reason)
        cancelled = provider_watchdog.createProviderRequestWatchdog(
            {"parentSignal": parent, "initialTimeoutMs": 1000, "buildTimeoutError": lambda: TimeoutError("unused")}
        )
        with self.assertRaisesRegex(RuntimeError, "cancelled by parent"):
            run(cancelled.race(asyncio.sleep(0)))
        self.assertTrue(cancelled.signal.aborted)

    def test_provider_requests_respect_abort_signal(self):
        signal = SimpleNamespace(aborted=True)
        calls = {"openai": 0, "anthropic": 0}
        openai_events = []

        def openai_factory(_options):
            calls["openai"] += 1

            async def create(_params):
                return AsyncChunks([])

            return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))

        with self.assertRaises(core.ProviderRequestError) as openai_ctx:
            run(
                testing.sendOpenAiChatCompletionsRequest(
                    {
                        "token": "test",
                        "request": {"model": "gpt-5.4", "system": "s", "messages": [{"role": "user", "content": "Hello"}], "tools": []},
                        "runtime": {"abortSignal": signal, "emitEvent": lambda event, data: openai_events.append((event, data))},
                        "maxAttempts": 2,
                        "clientFactory": openai_factory,
                    }
                )
            )
        self.assertEqual(openai_ctx.exception.record["category"], "cancelled")
        self.assertFalse(openai_ctx.exception.record["retryable"])
        self.assertEqual(calls["openai"], 0)
        self.assertFalse(any(event == "provider.request_started" for event, _ in openai_events))

        original_send = anthropic_module._send_anthropic_messages_sync
        anthropic_events = []

        def send_anthropic(*_args):
            calls["anthropic"] += 1
            return "{}", "application/json"

        anthropic_module._send_anthropic_messages_sync = send_anthropic
        try:
            with self.assertRaises(core.ProviderRequestError) as anthropic_ctx:
                run(
                    core.sendAnthropicMessagesRequest(
                        {
                            "config": {"provider": "anthropic", "apiKey": "test-key", "customHeaders": {}},
                            "request": {"model": "claude-sonnet-4-6", "system": "s", "messages": [{"role": "user", "content": "Hello"}], "tools": []},
                            "runtime": {"abortSignal": signal, "emitEvent": lambda event, data: anthropic_events.append((event, data))},
                            "maxAttempts": 2,
                        }
                    )
                )
            self.assertEqual(anthropic_ctx.exception.record["category"], "cancelled")
            self.assertFalse(anthropic_ctx.exception.record["retryable"])
            self.assertEqual(calls["anthropic"], 0)
            self.assertFalse(any(event == "provider.request_started" for event, _ in anthropic_events))
        finally:
            anthropic_module._send_anthropic_messages_sync = original_send

    def test_provider_requests_cancel_when_abort_signal_flips_during_activity(self):
        openai_signal = SimpleNamespace(aborted=False)
        calls = {"openai": 0, "anthropic": 0}

        async def create_slow(_params):
            calls["openai"] += 1
            openai_signal.aborted = True
            await asyncio.sleep(1)
            return AsyncChunks([])

        with self.assertRaises(core.ProviderRequestError) as openai_ctx:
            run(
                testing.sendOpenAiChatCompletionsRequest(
                    {
                        "token": "test",
                        "request": {"model": "gpt-5.4", "system": "s", "messages": [{"role": "user", "content": "Hello"}], "tools": []},
                        "runtime": {"abortSignal": openai_signal},
                        "maxAttempts": 2,
                        "requestStartTimeoutMs": 1000,
                        "clientFactory": lambda _options: SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create_slow))),
                    }
                )
            )
        self.assertEqual(openai_ctx.exception.record["category"], "cancelled")
        self.assertFalse(openai_ctx.exception.record["retryable"])
        self.assertEqual(calls["openai"], 1)

        anthropic_signal = SimpleNamespace(aborted=False)
        original_send = anthropic_module._send_anthropic_messages_sync

        def slow_send(*_args):
            calls["anthropic"] += 1
            anthropic_signal.aborted = True
            time.sleep(0.2)
            return "{}", "application/json"

        anthropic_module._send_anthropic_messages_sync = slow_send
        try:
            with self.assertRaises(core.ProviderRequestError) as anthropic_ctx:
                run(
                    core.sendAnthropicMessagesRequest(
                        {
                            "config": {"provider": "anthropic", "apiKey": "test-key", "customHeaders": {}},
                            "request": {"model": "claude-sonnet-4-6", "system": "s", "messages": [{"role": "user", "content": "Hello"}], "tools": []},
                            "runtime": {"abortSignal": anthropic_signal},
                            "maxAttempts": 2,
                            "jsonResponseTimeoutMs": 1000,
                        }
                    )
                )
            self.assertEqual(anthropic_ctx.exception.record["category"], "cancelled")
            self.assertFalse(anthropic_ctx.exception.record["retryable"])
            self.assertEqual(calls["anthropic"], 1)
        finally:
            anthropic_module._send_anthropic_messages_sync = original_send

    def test_provider_failure_classifier_matrix_matches_reference(self):
        base = {"provider": "openai", "providerLabel": "OpenAI", "phase": "request_start", "attempt": 1, "hadAssistantOutput": False, "hadToolUse": False}
        cases = [
            (401, "invalid api key", None, "auth", False),
            (403, "forbidden", None, "auth", False),
            (408, "timeout", None, "timeout", True),
            (429, "rate limit exceeded", None, "rate_limit", True),
            (429, "insufficient quota or billing", None, "quota", False),
            (400, "bad request", None, "client_request", False),
            (500, "server overloaded", None, "server", True),
        ]
        for status, body, error_type, category, retryable in cases:
            with self.subTest(status=status, category=category):
                failure = core.classifyProviderHttpFailure({**base, "status": status, "body": body, "errorType": error_type})
                self.assertEqual(failure.record["category"], category)
                self.assertEqual(failure.record["retryable"], retryable)
                self.assertEqual(failure.record["status"], status)
                self.assertEqual(failure.record["rawBody"], body)

        transport = core.normalizeUnknownProviderFailure({**base, "error": RuntimeError("socket hang up")})
        self.assertEqual(transport.record["category"], "transport")
        self.assertTrue(core.canRetryProviderFailure(transport, 2))
        partial = core.normalizeUnknownProviderFailure({**base, "error": RuntimeError("ECONNRESET"), "hadAssistantOutput": True})
        self.assertEqual(partial.record["category"], "transport")
        self.assertFalse(core.canRetryProviderFailure(partial, 2))
        protocol = core.normalizeUnknownProviderFailure({**base, "error": RuntimeError("Unexpected end of JSON input")})
        self.assertEqual(protocol.record["category"], "protocol")
        self.assertFalse(protocol.record["retryable"])
        auth = core.normalizeUnknownProviderFailure({**base, "error": RuntimeError("invalid API key")})
        self.assertEqual(auth.record["category"], "auth")

    def test_missing_provider_credentials_match_reference_diagnostics(self):
        request = {"model": "gpt-5.4", "system": "s", "messages": [{"role": "user", "content": "Hello"}], "tools": []}
        openai_config = {
            "provider": "openai",
            "providerProfileName": "codex",
            "providerProfile": {"name": "codex", "kind": "openai", "auth": {"envVar": "OPENAI_API_KEY"}},
            "openaiToken": None,
        }
        openai_response = agent_module._create_provider(openai_config, {})["send"](request)
        self.assertEqual(
            openai_response["content"][0]["text"],
            "No credential is configured for provider profile 'codex'. Set OPENAI_API_KEY, provide a Codex auth file, or run `tess auth login --openai` for official OpenAI access.",
        )
        self.assertEqual(openai_response["usage"], {"input_tokens": 0, "output_tokens": 0})
        self.assertEqual(openai_response["stopReason"], "end_turn")

        anthropic_config = {
            "provider": "anthropic",
            "providerProfileName": "anthropic-claude-code",
            "providerProfile": {"name": "anthropic-claude-code", "kind": "anthropic", "auth": {"envVar": "ANTHROPIC_AUTH_TOKEN"}},
            "apiKey": None,
            "authToken": None,
        }
        anthropic_response = agent_module._create_provider(anthropic_config, {})["send"]({**request, "model": "claude-sonnet-4-6"})
        self.assertEqual(
            anthropic_response["content"][0]["text"],
            "No credential is configured for provider profile 'anthropic-claude-code'. Set ANTHROPIC_AUTH_TOKEN or run `tess auth login` to enable model-backed responses.",
        )
        self.assertEqual(anthropic_response["usage"], {"input_tokens": 0, "output_tokens": 0})
        self.assertEqual(anthropic_response["stopReason"], "end_turn")
