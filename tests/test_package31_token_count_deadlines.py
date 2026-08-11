from __future__ import annotations

import agent_core.context.token_accounting as token_accounting_module

from tests.support.agent_core_case import *


def _config(root: str, *, count_timeout_ms: int, total_timeout_ms: int, exact: bool):
    profile = {
        "kind": "anthropic",
        "name": "deadline-anthropic",
        "baseUrl": "https://count.invalid/v1",
        "tokenCountTimeoutMs": count_timeout_ms,
        "totalRequestTimeoutMs": total_timeout_ms,
        "exactTokenCountRequired": exact,
        "maxContextLength": 200_000,
        "maxOutputTokens": 4_096,
        "compactionThresholdTokens": 180_000,
        "compactionOutputTokens": 4_096,
        "requestOptions": {},
    }
    return {
        "cwd": root,
        "runtimeHome": root,
        "allowedRoots": [root],
        "agents": {},
        "provider": "anthropic",
        "providerKind": "anthropic",
        "providerProfileName": "deadline-anthropic",
        "providerProfile": profile,
        "providerProfiles": {"deadline-anthropic": profile},
        "apiKey": "test-key",
        "authToken": None,
        "anthropicBaseUrl": "https://count.invalid/v1",
        "customHeaders": {},
        "model": "deadline-model",
        "effort": "medium",
        "assistantName": "Test",
        "assistantDescription": "",
        "systemPrompt": "test",
        "tools": [],
        "allowedTools": [],
        "disallowedTools": [],
        "permissionMode": "bypassPermissions",
        "sessionPersistence": False,
    }


def _session(root: str):
    return core.create_empty_session(
        cwd=root,
        provider="anthropic",
        providerProfileName="deadline-anthropic",
        model="deadline-model",
        effort="medium",
        systemPrompt="test",
        permissionMode="bypassPermissions",
    )


def _request():
    return {
        "model": "deadline-model",
        "system": "test",
        "messages": [{"role": "user", "content": "measure this request"}],
        "tools": [],
    }


def _openai_config(root: str, *, count_timeout_ms: int, exact: bool, factory):
    profile = {
        "kind": "openai",
        "name": "deadline-openai",
        "apiMode": "responses",
        "tokenCountTimeoutMs": count_timeout_ms,
        "totalRequestTimeoutMs": 250,
        "exactTokenCountRequired": exact,
        "maxContextLength": 200_000,
        "maxOutputTokens": 4_096,
        "compactionThresholdTokens": 180_000,
        "compactionOutputTokens": 4_096,
        "requestOptions": {},
    }
    config = _config(root, count_timeout_ms=count_timeout_ms, total_timeout_ms=250, exact=exact)
    config.update(
        {
            "provider": "openai",
            "providerKind": "openai",
            "providerProfileName": "deadline-openai",
            "providerProfile": profile,
            "providerProfiles": {"deadline-openai": profile},
            "apiKey": None,
            "openaiToken": "test-openai-key",
            "openaiApiMode": "responses",
            "openaiBaseUrl": "https://count.invalid/v1",
            "openaiTokenCountClientFactory": factory,
        }
    )
    return config


class Package31TokenCountDeadlineTests(AgentCoreTestCase):
    def setUp(self):
        self.original_count = token_accounting_module._measure_anthropic_count_tokens

    def tearDown(self):
        token_accounting_module._measure_anthropic_count_tokens = self.original_count

    def test_never_settling_exact_count_falls_back_observably(self):
        signals = []

        async def never_count(_config, _request, _timeout_seconds, signal):
            signals.append(signal)
            await asyncio.Future()

        token_accounting_module._measure_anthropic_count_tokens = never_count
        with tempfile.TemporaryDirectory(prefix="agent-core-token-deadline-") as root:
            config = _config(root, count_timeout_ms=25, total_timeout_ms=250, exact=False)
            events = []
            started = time.monotonic()
            measurement = run(
                testing.measure_request_input_tokens(
                    {
                        "config": config,
                        "request": _request(),
                        "session": {"id": "session-1"},
                        "emitEvent": lambda event, data: events.append((event, data)),
                    }
                )
            )
        self.assertLess(time.monotonic() - started, 0.5)
        self.assertEqual(measurement["source"], "legacy_estimate")
        self.assertGreater(measurement["baseInputTokens"], 0)
        self.assertEqual(measurement["safetyMarginTokens"], 2_000)
        self.assertEqual(
            measurement["inputTokens"],
            measurement["baseInputTokens"] + measurement["safetyMarginTokens"],
        )
        self.assertTrue(signals[0].aborted)
        fallback = next(data for event, data in events if event == "provider.token_count_fallback")
        self.assertEqual(fallback["phase"], "token_count")
        self.assertEqual(fallback["reason"], "timeout")
        self.assertEqual(fallback["token_count_timeout_ms"], 25)
        self.assertEqual(fallback["fallback_source"], "legacy_estimate")
        self.assertEqual(fallback["estimated_input_tokens"], measurement["inputTokens"])

    def test_mandatory_exact_count_fails_before_provider_dispatch(self):
        signals = []

        async def never_count(_config, _request, _timeout_seconds, signal):
            signals.append(signal)
            await asyncio.Future()

        token_accounting_module._measure_anthropic_count_tokens = never_count
        with tempfile.TemporaryDirectory(prefix="agent-core-token-deadline-") as root:
            config = _config(root, count_timeout_ms=20, total_timeout_ms=250, exact=True)
            session = _session(root)
            events = []
            provider_calls = 0

            async def send(_request):
                nonlocal provider_calls
                provider_calls += 1
                raise AssertionError("provider must not be called")

            with self.assertRaises(core.ProviderRequestError) as raised:
                run(
                    core.runAgentPrompt(
                        session=session,
                        config=config,
                        prompt="hello",
                        runtime=bind_test_runtime(
                            session,
                            {
                                "interactive": False,
                                "askQuestion": lambda *_: "",
                                "emitEvent": lambda event, data: events.append((event, data)),
                                "tools": [],
                            },
                        ),
                        providerOverride={"send": send},
                    )
                )
        self.assertEqual(raised.exception.record["phase"], "token_count")
        self.assertEqual(raised.exception.record["category"], "timeout")
        self.assertFalse(raised.exception.record["retryable"])
        self.assertEqual(provider_calls, 0)
        self.assertTrue(signals[0].aborted)
        self.assertTrue(any(event == "provider.failed" for event, _data in events))
        self.assertFalse(any(event == "provider.token_count_fallback" for event, _data in events))

    def test_successful_exact_count_disposes_deadline_without_late_abort(self):
        signals = []

        async def successful_count(_config, _request, _timeout_seconds, signal):
            signals.append(signal)
            return 37

        token_accounting_module._measure_anthropic_count_tokens = successful_count
        with tempfile.TemporaryDirectory(prefix="agent-core-token-deadline-") as root:
            measurement = run(
                testing.measure_request_input_tokens(
                    {
                        "config": _config(
                            root,
                            count_timeout_ms=20,
                            total_timeout_ms=250,
                            exact=False,
                        ),
                        "request": _request(),
                        "session": {"id": "session-1"},
                    }
                )
            )
        self.assertEqual(measurement["source"], "anthropic_count")
        self.assertEqual(measurement["inputTokens"], 37)
        time.sleep(0.05)
        self.assertFalse(signals[0].aborted)

    def test_openai_responses_exact_count_uses_sanitized_request(self):
        calls = []
        factory_args = []

        async def count(params):
            calls.append(params)
            return {"input_tokens": 41}

        def factory(args):
            factory_args.append(args)
            return {"responses": {"input_tokens": {"count": count}}}

        with tempfile.TemporaryDirectory(prefix="agent-core-token-deadline-") as root:
            measurement = run(
                testing.measure_request_input_tokens(
                    {
                        "config": _openai_config(
                            root,
                            count_timeout_ms=20,
                            exact=False,
                            factory=factory,
                        ),
                        "request": _request(),
                        "session": {"id": "session-openai"},
                    }
                )
            )
        self.assertEqual(measurement["source"], "openai_exact")
        self.assertEqual(measurement["inputTokens"], 41)
        self.assertEqual(len(calls), 1)
        for excluded in (
            "stream",
            "store",
            "max_output_tokens",
            "prompt_cache_key",
            "prompt_cache_retention",
        ):
            self.assertNotIn(excluded, calls[0])
        time.sleep(0.05)
        self.assertFalse(factory_args[0]["abortSignal"].aborted)

    def test_openai_responses_count_timeout_falls_back_and_aborts(self):
        signals = []

        async def count(_params):
            await asyncio.Future()

        def factory(args):
            signals.append(args["abortSignal"])
            return {"responses": {"input_tokens": {"count": count}}}

        with tempfile.TemporaryDirectory(prefix="agent-core-token-deadline-") as root:
            events = []
            measurement = run(
                testing.measure_request_input_tokens(
                    {
                        "config": _openai_config(
                            root,
                            count_timeout_ms=20,
                            exact=False,
                            factory=factory,
                        ),
                        "request": _request(),
                        "session": {"id": "session-openai"},
                        "emitEvent": lambda event, data: events.append((event, data)),
                    }
                )
            )
        self.assertEqual(measurement["source"], "openai_responses_estimate")
        self.assertEqual(measurement["safetyMarginTokens"], 2_000)
        self.assertTrue(signals[0].aborted)
        fallback = next(data for event, data in events if event == "provider.token_count_fallback")
        self.assertEqual(fallback["reason"], "timeout")
        self.assertEqual(fallback["fallback_source"], "openai_responses_estimate")

    def test_total_request_deadline_bounds_never_settling_provider_override(self):
        async def successful_count(_config, _request, _timeout_seconds, _signal):
            return 12

        token_accounting_module._measure_anthropic_count_tokens = successful_count
        with tempfile.TemporaryDirectory(prefix="agent-core-token-deadline-") as root:
            config = _config(root, count_timeout_ms=100, total_timeout_ms=200, exact=False)
            session = _session(root)
            provider_calls = 0

            async def send(_request):
                nonlocal provider_calls
                provider_calls += 1
                await asyncio.Future()

            started = time.monotonic()
            with self.assertRaises(core.ProviderRequestError) as raised:
                run(
                    core.runAgentPrompt(
                        session=session,
                        config=config,
                        prompt="hello",
                        runtime=bind_test_runtime(session, {"tools": []}),
                        providerOverride={"send": send},
                    )
                )
            elapsed = time.monotonic() - started
        self.assertEqual(provider_calls, 1)
        self.assertLess(elapsed, 1.0)
        self.assertEqual(raised.exception.record["category"], "timeout")
        self.assertIn("total deadline", raised.exception.record["rawMessage"])

    def test_provider_result_after_total_deadline_is_ignored(self):
        async def successful_count(_config, _request, _timeout_seconds, _signal):
            return 12

        token_accounting_module._measure_anthropic_count_tokens = successful_count
        with tempfile.TemporaryDirectory(prefix="agent-core-token-deadline-") as root:
            config = _config(root, count_timeout_ms=100, total_timeout_ms=200, exact=False)
            session = _session(root)
            late_results = []
            events = []

            async def send(_request):
                try:
                    await asyncio.Future()
                except asyncio.CancelledError:
                    late_results.append("returned-after-cancel")
                    return {
                        "content": [{"type": "text", "text": "late assistant output"}],
                        "model": "deadline-model",
                        "stopReason": "end_turn",
                        "usage": {
                            "input_tokens": 1,
                            "output_tokens": 1,
                            "cache_creation_input_tokens": 0,
                            "cache_read_input_tokens": 0,
                        },
                    }

            with self.assertRaises(core.ProviderRequestError) as raised:
                run(
                    core.runAgentPrompt(
                        session=session,
                        config=config,
                        prompt="hello",
                        runtime=bind_test_runtime(
                            session,
                            {
                                "tools": [],
                                "emitEvent": lambda event, data: events.append((event, data)),
                            },
                        ),
                        providerOverride={"send": send},
                    )
                )
        self.assertEqual(late_results, ["returned-after-cancel"])
        self.assertEqual(raised.exception.record["category"], "timeout")
        self.assertFalse(any(message.get("role") == "assistant" for message in session["messages"]))
        self.assertFalse(any(event == "message.assistant" for event, _data in events))
        failures = [data for event, data in events if event == "provider.failed"]
        self.assertEqual(len(failures), 1)
        self.assertEqual(session["metadata"]["lastProviderFailure"], raised.exception.record)

    def test_provider_profile_round_trips_deadline_settings(self):
        profile = core.resolve_provider_profile(
            "deadline-anthropic",
            {
                "kind": "anthropic",
                "defaultModel": "deadline-model",
                "tokenCountTimeoutMs": 15_000,
                "totalRequestTimeoutMs": 600_000,
                "exactTokenCountRequired": True,
            },
        )
        self.assertEqual(profile["tokenCountTimeoutMs"], 15_000)
        self.assertEqual(profile["totalRequestTimeoutMs"], 600_000)
        self.assertTrue(profile["exactTokenCountRequired"])


if __name__ == "__main__":
    unittest.main()
