"""Gemini's implicit cache as the runtime sees it: what was cached, why not, and what it cost."""
from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta, timezone

from tests.support.tess_core_case import *

from tess_core import ProviderRegistry, SessionOptions, start_runtime
from tess_core.agent.providers import _create_provider
from tess_core.config._knobs import reset_configured_knobs
from tess_core.providers.connection import check_provider_connection
from tess_core.providers.google import build_interactions_create_params
from tess_core.providers.google.cache import diagnose_google_cache, google_cache_event, google_cache_snapshot, minimum_cacheable_tokens
from tess_core.providers.google.pricing import resolve_google_model_pricing
from tess_core.providers.usage_costs import estimate_usage_cost_usd, resolve_model_pricing
from tess_core.runtime import host as host_module
from tess_core.runtime.events.prompt_cache import prompt_cache_event_payload
from tess_core.shell_surface.status import build_provider_profiles_report, build_status_report
from tess_core_harness.fake_providers.google import FakeGoogleClient, googleInteractionEvents, googleInteractionsSseBody
from tess_core_harness.fake_providers.server import FakeProviderHttpServer

KEY_VARIABLE = "TESS_CORE_GOOGLE_CACHE_TEST_KEY"
TOOL = {"name": "get_weather", "description": "Weather.", "input_schema": {"type": "object", "properties": {"location": {"type": "string"}}}}
NOW = datetime(2026, 11, 19, 12, 0, tzinfo=timezone.utc)


def _params(messages, **overrides):
    request = {"model": "gemini-4-hydrogen", "system": "Be brief.", "tools": [TOOL], "messages": messages, "effort": "high", "maxOutputTokens": 4096}
    request.update(overrides)
    return build_interactions_create_params(request)


FIRST = [{"role": "user", "content": "hello"}]
SECOND = [*FIRST, {"role": "assistant", "content": [{"type": "text", "text": "Hi."}]}, {"role": "user", "content": "And again?"}]
HIT = {"input_tokens": 500, "cache_read_input_tokens": 6000}
MISS = {"input_tokens": 6500, "cache_read_input_tokens": 0}


def _previous(messages=FIRST, at=NOW - timedelta(seconds=20), **overrides):
    snapshot = {**google_cache_snapshot(_params(messages, **overrides)), "compactions": 0, "at": at.isoformat()}
    snapshot.pop("prefixHash")
    snapshot.pop("prefixStableHash")
    return snapshot


class GoogleCacheDiagnosisTests(unittest.TestCase):
    def _reason(self, previous, messages, usage, *, compactions=0, now=NOW, **overrides):
        current = {**google_cache_snapshot(_params(messages, **overrides), (previous or {}).get("inputCount")), "compactions": compactions}
        return diagnose_google_cache(previous, current, usage, model=overrides.get("model", "gemini-4-hydrogen"), now=now)

    def test_a_snapshot_holds_hashes_and_counts_and_no_content(self):
        params = _params(SECOND)
        snapshot = google_cache_snapshot(params, 1)
        self.assertEqual(set(snapshot), {"version", "modelHash", "systemHash", "toolsHash", "configHash", "inputCount", "inputHash", "stableHash", "prefixHash", "prefixStableHash"})
        self.assertEqual(snapshot["inputCount"], 3)
        self.assertNotIn("hello", json.dumps(snapshot))
        self.assertEqual(snapshot["prefixHash"], google_cache_snapshot(_params(FIRST))["inputHash"])
        self.assertIsNone(google_cache_snapshot(params)["prefixHash"])
        self.assertIsNone(google_cache_snapshot(params, 9)["prefixHash"])
        self.assertEqual(snapshot["configHash"], google_cache_snapshot(_params(SECOND, maxOutputTokens=99), 1)["configHash"])

    def test_the_reason_for_each_kind_of_call(self):
        previous = _previous()
        cases = [
            ("warmup_no_previous_state", dict(previous=None, messages=FIRST, usage=MISS)),
            ("warmup_no_previous_state", dict(previous={**previous, "version": 0}, messages=SECOND, usage=HIT)),
            ("cache_read_observed", dict(previous=previous, messages=SECOND, usage=HIT)),
            ("model_changed", dict(previous=previous, messages=SECOND, usage=MISS, model="gemini-4-neon")),
            ("system_changed", dict(previous=previous, messages=SECOND, usage=MISS, system="Be verbose.")),
            ("tools_changed", dict(previous=previous, messages=SECOND, usage=MISS, tools=[])),
            ("thinking_or_output_config_changed", dict(previous=previous, messages=SECOND, usage=MISS, effort="low")),
            ("message_prefix_changed", dict(previous=previous, messages=[{"role": "user", "content": "HELLO"}, *SECOND[1:]], usage=MISS)),
            ("compaction_or_relief_rewrite", dict(previous=previous, messages=[{"role": "user", "content": "A checkpoint."}, *SECOND[1:]], usage=MISS, compactions=1)),
            ("compaction_or_relief_rewrite", dict(previous=_previous(SECOND), messages=[{"role": "user", "content": "summary"}], usage=MISS)),
            ("below_cache_minimum", dict(previous=previous, messages=SECOND, usage={"input_tokens": 900, "cache_read_input_tokens": 0})),
            ("idle_gap_may_have_expired", dict(previous=_previous(at=NOW - timedelta(minutes=9)), messages=SECOND, usage=MISS)),
            ("unchanged_short_gap_likely_server_side", dict(previous=previous, messages=SECOND, usage=MISS)),
            ("no_cache_usage_reported", dict(previous=previous, messages=SECOND, usage={"input_tokens": 6500})),
            # Measured live: a full hit is the previous request less at most 2,050 re-read tokens.
            ("cache_read_observed", dict(previous={**previous, "inputTokens": 25333}, messages=SECOND, usage={"input_tokens": 2263, "cache_read_input_tokens": 25333})),
            ("cache_read_observed", dict(previous={**previous, "inputTokens": 8777}, messages=SECOND, usage={"input_tokens": 2358, "cache_read_input_tokens": 6728})),
            ("partial_cache_read", dict(previous={**previous, "inputTokens": 25333}, messages=SECOND, usage={"input_tokens": 24872, "cache_read_input_tokens": 3321})),
            ("below_cache_minimum", dict(previous={**previous, "inputTokens": 3320}, messages=SECOND, usage={"input_tokens": 4985, "cache_read_input_tokens": 0})),
        ]
        for expected, case in cases:
            with self.subTest(expected=expected):
                self.assertEqual(self._reason(case.pop("previous"), case.pop("messages"), case.pop("usage"), **case), expected)

    def test_context_for_one_turn_in_the_last_step_is_not_a_changed_prefix(self):
        # After a compaction the runtime puts its continuation context in the newest user
        # message. The next request carries that message without it: only the last step of
        # the earlier request differs, and Gemini still serves everything before it from cache.
        earlier = [*SECOND[:2], {"role": "user", "content": "[Dynamic continuation context] a checkpoint\n\nAnd again?"}]
        later = [*SECOND, {"role": "assistant", "content": [{"type": "text", "text": "Yes."}]}, {"role": "user", "content": "And once more?"}]
        self.assertEqual(self._reason(_previous(earlier), later, HIT), "cache_read_observed")
        self.assertEqual(self._reason(_previous(earlier), later, MISS), "unchanged_short_gap_likely_server_side")
        changed_earlier = [{"role": "user", "content": "HELLO"}, *later[1:]]
        self.assertEqual(self._reason(_previous(earlier), changed_earlier, MISS), "message_prefix_changed")
        # A request of one step has no stable part: a different step is a different prefix.
        self.assertEqual(self._reason(_previous(FIRST), [{"role": "user", "content": "something else entirely"}], MISS), "message_prefix_changed")

    def test_the_smallest_cacheable_prompt_depends_on_the_model(self):
        self.assertEqual(minimum_cacheable_tokens("gemini-4-neon"), 4096)
        self.assertEqual(minimum_cacheable_tokens("gemini-2.5-flash"), 2048)
        previous = _previous(model="gemini-2.5-flash")
        usage = {"input_tokens": 3000, "cache_read_input_tokens": 0}
        self.assertEqual(self._reason(previous, SECOND, usage, model="gemini-2.5-flash"), "unchanged_short_gap_likely_server_side")

    def test_every_reason_survives_the_event_schema(self):
        for reason in ("cache_read_observed", "below_cache_minimum", "idle_gap_may_have_expired", "partial_cache_read", "message_prefix_changed"):
            payload = prompt_cache_event_payload(google_cache_event("s1", HIT, reason))
            self.assertEqual((payload["phase"], payload["mode"], payload["ttl"], payload["placements"], payload["localReason"], payload["stateStatus"]), ("observed", "auto", "provider_default", ["automatic"], reason, "recorded"))
            self.assertEqual((payload["cacheReadInputTokens"], payload["cacheCreationInputTokens"]), (6000, 0))


class GoogleCacheObservationTests(TessCoreTestCase):
    def test_each_call_reports_what_the_cache_did_and_leaves_a_snapshot_for_the_next(self):
        client = FakeGoogleClient([
            googleInteractionEvents([{"type": "text", "text": "Hi."}], usage={"total_input_tokens": 6000, "total_cached_tokens": 0, "total_output_tokens": 5}),
            googleInteractionEvents([{"type": "text", "text": "Again."}], usage={"total_input_tokens": 6100, "total_cached_tokens": 5900, "total_output_tokens": 5}),
            googleInteractionEvents([{"type": "text", "text": "Fresh."}], usage={"total_input_tokens": 6200, "total_cached_tokens": 0, "total_output_tokens": 5}),
        ])
        events: list[tuple[str, dict]] = []
        session = {"id": "s1", "metadata": {"compactions": 0}}
        config = {"provider": "google", "googleApiKey": "k", "providerProfile": {"auth": {"kind": "google_api_key"}}}
        runtime = {"emitEvent": lambda name, data: events.append((name, data)), "googleClientFactory": client.factory}
        send = _create_provider(config, runtime, session)["send"]
        request = lambda messages, **extra: {"model": "gemini-4-hydrogen", "system": "Be brief.", "tools": [TOOL], "messages": messages, **extra}

        async def scenario():
            first = await send(request(FIRST))
            second = await send(request(SECOND))
            third = await send(request(SECOND, system="Be verbose."))
            return first, second, third

        first, second, third = asyncio.run(scenario())
        for response in (first, second, third):
            self.assertNotIn("googleCache", response)
        cache_events = [data for name, data in events if name == "provider.prompt_cache"]
        self.assertEqual([event["local_reason"] for event in cache_events], ["warmup_no_previous_state", "cache_read_observed", "system_changed"])
        self.assertEqual([event["cache_read_input_tokens"] for event in cache_events], [0, 5900, 0])
        self.assertEqual({event["session_id"] for event in cache_events}, {"s1"})
        kept = session["metadata"]["googleCache"]
        self.assertEqual((kept["inputCount"], kept["compactions"], kept["inputTokens"]), (3, 0, 6200))
        self.assertNotIn("prefixHash", kept)
        self.assertNotIn("prefixStableHash", kept)
        self.assertNotIn("hello", json.dumps(kept))
        self.assertEqual(second["usage"], {"input_tokens": 200, "output_tokens": 5, "cache_read_input_tokens": 5900, "cache_hit_input_tokens": 5900})

    def test_a_session_keeps_its_snapshot_across_turns_and_on_disk(self):
        host_module._ACTIVE = None
        root, home, workspace, previous = self.temp_context()
        os.environ[KEY_VARIABLE] = "cache-google-key"
        requests: list[dict] = []
        body = lambda text, cached: {"contentType": "text/event-stream", "rawBody": googleInteractionsSseBody(googleInteractionEvents([{"type": "text", "text": text}], usage={"total_input_tokens": 7000, "total_cached_tokens": cached, "total_output_tokens": 4}))}
        server = FakeProviderHttpServer(provider="google", script={"interactions": [body("one", 0), body("two", 6500)]}, request_recorder=requests).start()
        try:
            registry = ProviderRegistry.from_document({"schema_version": 1, "providerProfiles": {"gemini": {"kind": "google", "baseUrl": server.base_url, "defaultModel": "gemini-4-hydrogen", "auth": {"kind": "google_api_key", "envVar": KEY_VARIABLE}}}})
            seen: list[tuple[str, dict]] = []

            class Listener:
                def on_event(self, session_id, envelope):
                    if envelope["event"] == "provider.prompt_cache":
                        seen.append((session_id, envelope))
                def on_session_opened(self, *_): pass
                def on_session_closed(self, *_): pass
                def on_runtime_event(self, *_): pass

            async def scenario():
                runtime = await start_runtime(str(home), cwd=str(workspace), daemon_enabled=False, startup_timeout_seconds=20, provider_registry=registry, listener=Listener(),
                                              session_options=lambda *_: SessionOptions(provider="gemini", permission_mode="bypassPermissions", extra={"settingSources": ""}))
                try:
                    handle = await runtime.sessions.open()
                    await handle.run_turn("first")
                    await handle.run_turn("second")
                    return await core.load_session(handle.id)
                finally:
                    await runtime.stop()

            stored = run(scenario())
        finally:
            server.close()
            os.environ.pop(KEY_VARIABLE, None)
            reset_configured_knobs()
            self.restore_home(root, previous)
        self.assertEqual(stored["metadata"]["googleCache"]["inputCount"], 3)
        self.assertEqual(len(seen), 2)
        payloads = [json.dumps(envelope) for _, envelope in seen]
        self.assertIn("warmup_no_previous_state", payloads[0])
        self.assertIn("cache_read_observed", payloads[1])
        usage = stored["metadata"]["usage"]
        self.assertEqual((usage["inputTokens"], usage["cacheReadInputTokens"]), (7500, 6500))


class GooglePricingTests(unittest.TestCase):
    def test_prices_follow_the_date_they_take_effect(self):
        before, after = date(2026, 11, 30), date(2026, 12, 1)
        self.assertEqual(resolve_google_model_pricing("gemini-4-carbon", before)["inputUsdPerMtok"], 2.0)
        self.assertEqual(resolve_google_model_pricing("gemini-4-carbon", after), {"inputUsdPerMtok": 4.0, "outputUsdPerMtok": 20.0, "cacheWrite5mUsdPerMtok": 0, "cacheWrite1hUsdPerMtok": 0, "cacheReadUsdPerMtok": 0.20})
        self.assertEqual(resolve_google_model_pricing("gemini-3.8-flash", date(2026, 12, 31))["outputUsdPerMtok"], 3.75)
        self.assertEqual(resolve_google_model_pricing("gemini-3.8-flash", date(2027, 1, 1))["outputUsdPerMtok"], 7.5)
        self.assertEqual(resolve_google_model_pricing("models/gemini-4-hydrogen", after)["cacheReadUsdPerMtok"], 0.075)
        self.assertEqual(resolve_google_model_pricing("gemini-4-neon-001", date(2025, 1, 1))["inputUsdPerMtok"], 3.0)
        self.assertIsNone(resolve_google_model_pricing("gemini-9-unknown"))
        self.assertIsNone(resolve_google_model_pricing("gemini-embedding-2"))

    def test_cached_input_costs_a_twentieth_and_thinking_is_output(self):
        pricing = resolve_model_pricing("gemini-4-hydrogen")
        self.assertEqual((pricing["inputUsdPerMtok"], pricing["outputUsdPerMtok"], pricing["cacheReadUsdPerMtok"]), (1.5, 7.5, 0.075))
        cold = estimate_usage_cost_usd({"input_tokens": 1_000_000, "output_tokens": 100_000, "cache_read_input_tokens": 0}, "gemini-4-hydrogen")
        warm = estimate_usage_cost_usd({"input_tokens": 100_000, "output_tokens": 100_000, "cache_read_input_tokens": 900_000}, "gemini-4-hydrogen")
        self.assertAlmostEqual(cold, 1.5 + 0.75)
        self.assertAlmostEqual(warm, 0.15 + 0.75 + 0.0675)
        self.assertEqual(estimate_usage_cost_usd({"input_tokens": 10, "output_tokens": 10}, "gemini-9-unknown"), 0.0)
        self.assertIsNotNone(resolve_model_pricing("claude-sonnet-4-6"))
        self.assertIsNotNone(resolve_model_pricing("gpt-5.4"))


class Response:
    def __init__(self, status, body):
        self.status, self._body = status, body

    async def read_limited(self, limit):
        if len(self._body) > limit:
            raise ValueError("too large")
        return self._body


def _transport(status=200, payload=None, seen=None):
    names = ["models/gemini-4-hydrogen", "models/gemini-4-neon", "models/gemini-3.8-flash", "models/gemini-3.8-flash-tts", "models/gemini-embedding-2", "models/gemini-3.1-flash-image", "models/gemini-nano-banana-2.1", "models/gemini-3.8-live", "models/gemini-3.5-transcribe", "models/veo-3.1-generate-preview", "models/gemma-4-31b-it", "models/gemini-robotics-er-2-preview", "models/gemini-2.5-computer-use-preview-10-2025"]
    answer = payload if payload is not None else json.dumps({"models": [{"name": name, "inputTokenLimit": 1048576} for name in names] + [{"nope": 1}]}).encode()

    class Owner:
        async def start(self, *, url, body, headers, timeout_seconds, method):
            if seen is not None:
                seen.update(url=url, headers=headers, method=method)
            return Response(status, answer)

        def close(self):
            pass

    return Owner


class GoogleConnectionAndStatusTests(unittest.TestCase):
    def _registry(self, **profile):
        record = {"kind": "google", "defaultModel": "gemini-4-hydrogen", "auth": {"kind": "google_api_key", "envVar": KEY_VARIABLE}}
        record.update(profile)
        return ProviderRegistry.from_document({"providerProfiles": {"gemini": record}})

    def test_the_connection_check_lists_the_models_a_session_can_use(self):
        seen: dict = {}
        with mock.patch.dict(os.environ, {KEY_VARIABLE: "check-google-key"}):
            result = asyncio.run(check_provider_connection(self._registry(), "gemini", transport=_transport(seen=seen)))
        self.assertEqual(result, {"state": "available", "code": "models_listed", "models": ["gemini-4-hydrogen", "gemini-4-neon", "gemini-3.8-flash"]})
        self.assertEqual((seen["url"], seen["method"]), ("https://generativelanguage.googleapis.com/v1beta/models?pageSize=1000", "GET"))
        self.assertEqual(seen["headers"]["x-goog-api-key"], "check-google-key")
        self.assertNotIn("Authorization", seen["headers"])
        self.assertNotIn("check-google-key", seen["url"])

    def test_the_connection_check_reports_a_refused_key_and_a_strange_answer(self):
        with mock.patch.dict(os.environ, {KEY_VARIABLE: "check-google-key"}):
            for status, payload, expected in (
                (401, b"{}", ("failed", "authentication_denied")), (403, b"{}", ("failed", "authentication_denied")),
                (429, b"{}", ("failed", "rate_limited")), (200, b'{"data": []}', ("failed", "invalid_provider_response")),
                (400, b'[{"error": {"code": 400, "status": "INVALID_ARGUMENT", "details": [{"reason": "API_KEY_INVALID"}]}}]', ("failed", "authentication_denied")),
                (400, b'{"error": {"code": "invalid_request"}}', ("failed", "invalid_provider_response")),
                (200, b"not json", ("failed", "invalid_provider_response")), (200, b'{"models": []}', ("available", "models_listed")),
            ):
                with self.subTest(status=status, payload=payload):
                    result = asyncio.run(check_provider_connection(self._registry(baseUrl="https://proxy.invalid/v1beta/"), "gemini", transport=_transport(status, payload)))
                    self.assertEqual((result["state"], result["code"]), expected)
                    self.assertNotIn("check-google-key", json.dumps(result))
        self.assertEqual(asyncio.run(check_provider_connection(self._registry(), "gemini", transport=_transport()))["code"], "invalid_profile")

    def test_status_names_the_api_and_endpoint_of_a_google_session(self):
        config = {"provider": "google", "providerProfileName": "gemini", "model": "gemini-4-hydrogen", "googleBaseUrl": "https://proxy.invalid/v1beta", "openaiApiMode": "responses", "openaiBaseUrl": "https://wrong.invalid",
                  "providerProfiles": {"gemini": {"kind": "google", "defaultModel": "gemini-4-hydrogen", "auth": {"kind": "google_api_key", "envVar": KEY_VARIABLE}}}}
        report = build_status_report({"id": "s1", "provider": "google", "metadata": {}}, config)
        self.assertIn("mode         interactions", report)
        self.assertIn("base url     https://proxy.invalid/v1beta", report)
        profiles = build_provider_profiles_report({"providerProfileName": "gemini"}, config)
        self.assertIn("mode       interactions", profiles)
        self.assertIn(f"google_api_key via {KEY_VARIABLE}", profiles)
        self.assertIn("mode         messages", build_status_report({"id": "s2", "provider": "anthropic", "metadata": {}}, {"provider": "anthropic"}))
        self.assertIn("base url     default", build_status_report({"id": "s3", "provider": "google", "metadata": {}}, {"provider": "google"}))
