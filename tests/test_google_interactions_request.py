"""One provider call to the Gemini Interactions API, against scripted streams."""
from __future__ import annotations

import asyncio
import json
import unittest

from tess_core.config._knobs import configure_knobs, reset_configured_knobs
from tess_core.providers._core import AbortSignal
from tess_core.providers.error_evidence import ProviderWireError
from tess_core.providers.errors import ProviderIncompleteResponseError, ProviderRequestError
from tess_core.providers.google import convert_messages_to_steps, send_google_interactions_request
from tess_core.runtime.execution_budget import active_execution_budget
from tess_core.runtime.secrets import RuntimeSecretError
from tess_core_harness.fake_providers.google import (
    FakeGoogleClient,
    googleErrorBody,
    googleInteractionEvents,
    googleInteractionsSseBody,
)
from tess_core_harness.fake_providers.server import FakeProviderHttpServer

TOOLS = [{"name": "get_weather", "description": "Weather.", "input_schema": {"type": "object", "properties": {"location": {"type": "string"}}}}]
USAGE = {"total_tokens": 346, "total_input_tokens": 100, "total_cached_tokens": 60, "total_output_tokens": 90, "total_tool_use_tokens": 0, "total_thought_tokens": 245}


def _request(**overrides):
    request = {"model": "gemini-4-hydrogen", "system": "Be brief.", "messages": [{"role": "user", "content": "hello"}], "tools": TOOLS, "effort": "high", "maxOutputTokens": 4096}
    request.update(overrides)
    return request


def _send(client, *, events=None, runtime=None, request=None, **overrides):
    captured: list[tuple[str, dict]] = []
    run_runtime = {"emitEvent": lambda name, data: captured.append((name, data)), **(runtime or {})}
    args = {
        "config": {"provider": "google", "googleApiKey": "fake-google-key", "customHeaders": {}, "providerProfile": {"auth": {"kind": "google_api_key"}}},
        "request": request or _request(),
        "runtime": run_runtime,
        "clientFactory": client.factory,
        "maxAttempts": 1,
    }
    args.update(overrides)
    if events is not None:
        events.extend([])
    result = asyncio.run(send_google_interactions_request(args))
    if events is not None:
        events.extend(captured)
    return result


class GoogleInteractionsResponseTests(unittest.TestCase):
    def setUp(self):
        configure_knobs({"provider": {"google_retry_delays_ms": [1]}})
        self.addCleanup(reset_configured_knobs)

    def test_a_text_turn(self):
        client = FakeGoogleClient([googleInteractionEvents([{"type": "text", "chunks": ["Hel", "lo."]}], usage=USAGE)])
        events: list = []
        response = _send(client, events=events, runtime={"includePartialMessages": True})
        self.assertEqual(response, {
            "content": [{"type": "text", "text": "Hello."}],
            "usage": {"input_tokens": 100, "output_tokens": 335, "cache_read_input_tokens": 60, "cache_hit_input_tokens": 60},
            "model": "gemini-4-hydrogen",
            "stopReason": "end_turn",
        })
        partials = [data for name, data in events if name == "message.assistant.partial"]
        self.assertEqual([(item["text_delta"], item["text"]) for item in partials], [("Hel", "Hel"), ("lo.", "Hello.")])
        started = [data for name, data in events if name == "provider.request_started"]
        self.assertEqual((started[0]["provider"], started[0]["transport"], started[0]["chain_reset_reason"]), ("google", "sse", "stateless_interactions"))
        self.assertEqual(client.options[0], {"apiKey": "fake-google-key", "baseURL": "https://generativelanguage.googleapis.com/v1beta", "defaultHeaders": {}})
        self.assertIs(client.requests[0]["store"], False)
        self.assertEqual(client.closed_streams, 1)

    def test_thinking_is_reported_and_kept_on_the_block_it_preceded(self):
        steps = [
            {"type": "thought", "signature": "sig-1", "summary": "Working it out."},
            {"type": "text", "text": "Checking."},
            {"type": "thought", "signature": "sig-2"},
            {"type": "function_call", "id": "call_a", "name": "get_weather", "argumentChunks": ['{"loc', 'ation": "Par', 'is"}']},
            {"type": "thought", "signature": "sig-3"},
        ]
        client = FakeGoogleClient([googleInteractionEvents(steps)])
        events: list = []
        response = _send(client, events=events, runtime={"includePartialMessages": True})
        self.assertEqual(response["stopReason"], "tool_use")
        self.assertEqual(response["content"], [
            {"type": "text", "text": "Checking.", "provider_metadata": {"google_thoughts": [{"type": "thought", "signature": "sig-1", "summary": [{"type": "text", "text": "Working it out."}]}]}},
            {"type": "tool_use", "id": "call_a", "name": "get_weather", "input": {"location": "Paris"}, "provider_metadata": {"google_thoughts": [{"type": "thought", "signature": "sig-2"}], "google_trailing_thoughts": [{"type": "thought", "signature": "sig-3"}]}},
        ])
        thinking = [data for name, data in events if name == "message.assistant.thinking.partial"]
        self.assertEqual((thinking[0]["thinking_delta"], thinking[0]["reasoning_kind"], thinking[0]["user_visible"]), ("Working it out.", "summary", False))

    def test_the_next_request_returns_every_thought_step_as_it_was_received(self):
        steps = [
            {"type": "thought", "signature": "sig-1", "summary": "First."},
            {"type": "function_call", "id": "call_a", "name": "get_weather", "arguments": {"location": "Paris"}},
            {"type": "function_call", "id": "call_b", "name": "get_weather", "arguments": {"location": "Rome"}},
        ]
        client = FakeGoogleClient([googleInteractionEvents(steps), googleInteractionEvents([{"type": "text", "text": "Both sunny."}])])
        first = _send(client)
        history = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": first["content"]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "call_a", "content": "sunny"}, {"type": "tool_result", "tool_use_id": "call_b", "content": "sunny"}]},
        ]
        second = _send(client, request=_request(messages=history))
        self.assertEqual(second["content"], [{"type": "text", "text": "Both sunny."}])
        sent = client.requests[1]["input"]
        self.assertEqual(sent[: len(client.requests[0]["input"])], client.requests[0]["input"])
        self.assertEqual(sent[1], {"type": "thought", "signature": "sig-1", "summary": [{"type": "text", "text": "First."}]})
        self.assertEqual([step["type"] for step in sent], ["user_input", "thought", "function_call", "function_call", "function_result", "function_result"])
        self.assertEqual(sent, convert_messages_to_steps(history))

    def test_a_function_without_arguments_and_one_whose_arguments_arrive_whole(self):
        events = googleInteractionEvents([{"type": "function_call", "id": "c1", "name": "now", "argumentChunks": []}])
        events.insert(4, {"event_type": "step.start", "index": 1, "step": {"type": "function_call", "id": "c2", "name": "get_weather", "arguments": {"location": "Oslo"}}})
        events.insert(5, {"event_type": "step.stop", "index": 1})
        response = _send(FakeGoogleClient([events]))
        self.assertEqual([(block["id"], block["input"]) for block in response["content"]], [("c1", {}), ("c2", {"location": "Oslo"})])

    def test_text_carried_by_step_start_and_event_types_this_build_does_not_know(self):
        events = [
            {"event_type": "interaction.created", "interaction": {"id": "v1_x", "status": "in_progress"}},
            {"event_type": "interaction.future_event", "payload": {"anything": True}},
            {"event_type": "step.start", "index": 0, "step": {"type": "google_search_call", "id": "s1", "signature": ""}},
            {"event_type": "step.delta", "index": 0, "delta": {"type": "google_search_call", "arguments": {"queries": ["x"]}}},
            {"event_type": "step.stop", "index": 0},
            {"event_type": "step.start", "index": 1, "step": {"type": "model_output", "content": [{"type": "text", "text": "Based on"}]}},
            {"event_type": "step.delta", "index": 1, "delta": {"type": "audio", "data": "AAAA"}},
            {"event_type": "step.delta", "index": 1, "delta": {"type": "text", "text": " the clues."}},
            {"event_type": "step.delta", "index": 9, "delta": {"type": "text", "text": "orphan"}},
            {"event_type": "step.stop", "index": 1},
            "not an event",
            {"event_type": "interaction.completed", "interaction": {"id": "v1_x", "status": "completed", "usage": {"total_input_tokens": 7, "total_output_tokens": 3}}},
        ]
        response = _send(FakeGoogleClient([events]))
        self.assertEqual(response["content"], [{"type": "text", "text": "Based on the clues."}])
        self.assertEqual(response["usage"], {"input_tokens": 7, "output_tokens": 3})

    def test_an_incomplete_interaction_is_a_truncated_response(self):
        response = _send(FakeGoogleClient([googleInteractionEvents([{"type": "text", "text": "Half an ans"}], status="incomplete")]))
        self.assertEqual(response["stopReason"], "max_tokens")
        self.assertEqual(response["content"], [{"type": "text", "text": "Half an ans"}])

    def test_the_execution_budget_notice_joins_the_system_instruction(self):
        class Budget:
            calls = 0

            def consume_provider_call(self):
                Budget.calls += 1

            def context(self):
                return "BUDGET NOTICE"

        client = FakeGoogleClient([googleInteractionEvents([{"type": "text", "text": "ok"}])])
        token = active_execution_budget.set(Budget())
        try:
            _send(client)
        finally:
            active_execution_budget.reset(token)
        self.assertEqual(Budget.calls, 1)
        self.assertEqual(client.requests[0]["system_instruction"], "Be brief.\n\nBUDGET NOTICE")
        self.assertEqual(list(client.requests[0])[:2], ["model", "system_instruction"])
        self.assertNotIn("system", client.requests[0])

    def test_a_credential_that_was_never_resolved_does_not_reach_the_transport(self):
        client = FakeGoogleClient([googleInteractionEvents([{"type": "text", "text": "ok"}])])
        for config in (
            {"provider": "google", "googleApiKey": {"kind": "runtime_secret"}, "providerProfile": {"auth": {"kind": "google_api_key"}}},
            {"provider": "google", "googleApiKey": None, "providerProfile": {"auth": {"kind": "google_api_key"}}},
            {"provider": "google", "googleApiKey": "k", "customHeaders": {"x-extra": {"kind": "runtime_secret"}}, "providerProfile": {"auth": {"kind": "google_api_key"}}},
        ):
            with self.subTest(config=config), self.assertRaises(RuntimeSecretError):
                _send(client, config=config)
        self.assertEqual(client.requests, [])
        _send(client, config={"provider": "google", "googleApiKey": None, "providerProfile": {"auth": {"kind": "none"}}})
        self.assertIsNone(client.options[0]["apiKey"])


class GoogleInteractionsFailureTests(unittest.TestCase):
    def setUp(self):
        configure_knobs({"provider": {"google_retry_delays_ms": [1]}})
        self.addCleanup(reset_configured_knobs)

    def _fail(self, scripts, **overrides):
        client = FakeGoogleClient(scripts)
        with self.assertRaises(ProviderRequestError) as caught:
            _send(client, **overrides)
        return caught.exception.record, client

    def test_http_failures_are_classified_and_only_the_transient_ones_are_retried(self):
        def wire(status, code, retry_after=None):
            return ProviderWireError(f"{code} happened", status=status, body=json.dumps(googleErrorBody(code)), code=code, retryAfterMs=retry_after)

        ok = googleInteractionEvents([{"type": "text", "text": "ok"}])
        for status, code, category, retried in (
            (429, "rate_limit_exceeded", "rate_limit", True), (429, "too_many_requests", "rate_limit", True),
            (429, "quota_exceeded", "quota", False), (500, "api_error", "server", True),
            (503, "service_unavailable", "server", True), (504, "deadline_exceeded", "server", True),
            (401, "authentication", "auth", False), (403, "permission_denied", "auth", False),
            (402, "payment_required", "client_request", False), (400, "invalid_request", "client_request", False),
            (404, "model_not_found", "client_request", False), (409, "aborted", "client_request", False),
        ):
            with self.subTest(code=code):
                client = FakeGoogleClient([wire(status, code), ok])
                events: list = []
                if retried:
                    response = _send(client, events=events, maxAttempts=3)
                    self.assertEqual(response["content"], [{"type": "text", "text": "ok"}])
                    self.assertEqual(len(client.requests), 2)
                    retrying = [data for name, data in events if name == "provider.retrying"]
                    self.assertEqual((retrying[0]["category"], retrying[0]["status"], retrying[0]["code"], retrying[0]["next_attempt"]), (category, status, code, 2))
                    self.assertEqual([data["attempts"] for name, data in events if name == "provider.recovered"], [2])
                else:
                    with self.assertRaises(ProviderRequestError) as caught:
                        _send(client, maxAttempts=3)
                    self.assertEqual((caught.exception.record["category"], caught.exception.record["retryable"]), (category, False))
                    self.assertEqual(len(client.requests), 1)

    def test_attempts_are_bounded_and_the_key_is_never_in_the_failure(self):
        record, client = self._fail([ProviderWireError("down", status=503, body=json.dumps(googleErrorBody("service_unavailable")), code="service_unavailable")], maxAttempts=3)
        self.assertEqual((len(client.requests), record["attempt"], record["category"]), (3, 3, "server"))
        self.assertNotIn("fake-google-key", json.dumps(record))

    def test_an_error_event_before_any_output_is_retried_and_after_output_is_not(self):
        error = {"event_type": "error", "error": {"code": "gateway_timeout", "message": "Deadline expired before operation could complete."}}
        ok = googleInteractionEvents([{"type": "text", "text": "ok"}])
        early = googleInteractionEvents([])[:2] + [error]
        client = FakeGoogleClient([early, ok])
        self.assertEqual(_send(client, maxAttempts=2)["content"], [{"type": "text", "text": "ok"}])
        late = googleInteractionEvents([{"type": "text", "text": "partial"}])[:4] + [error]
        record, client = self._fail([late, ok], maxAttempts=3)
        self.assertEqual((record["category"], record["status"], record["hadAssistantOutput"], record["streamEventType"]), ("server", 504, True, "error"))
        self.assertEqual(len(client.requests), 1)
        self.assertEqual(client.closed_streams, 1)

    def test_a_withheld_response_is_reported_and_not_asked_for_again(self):
        blocked = googleInteractionEvents([])[:2] + [{"event_type": "error", "error": {"code": "safety", "message": "Blocked."}}]
        record, client = self._fail([blocked], maxAttempts=3)
        self.assertEqual((record["category"], record["retryable"], record["code"]), ("client_request", False, "safety"))
        self.assertIn("withheld", record["rawMessage"])
        self.assertEqual(len(client.requests), 1)

    def test_a_malformed_function_call_gets_a_fresh_attempt(self):
        malformed = googleInteractionEvents([])[:2] + [{"event_type": "error", "error": {"code": "malformed_function_call", "message": "Could not parse."}}]
        client = FakeGoogleClient([malformed, googleInteractionEvents([{"type": "text", "text": "ok"}])])
        self.assertEqual(_send(client, maxAttempts=2)["stopReason"], "end_turn")
        record, _ = self._fail([malformed], maxAttempts=1)
        self.assertEqual((record["category"], record["code"]), ("protocol", "malformed_function_call"))

    def test_a_failed_interaction_is_a_failure(self):
        record, _ = self._fail([googleInteractionEvents([], status="failed")])
        self.assertEqual((record["category"], record["code"]), ("unknown", "failed"))

    def test_a_stream_that_ends_early_is_incomplete(self):
        complete = googleInteractionEvents([{"type": "function_call", "id": "c1", "name": "get_weather", "arguments": {"location": "Paris"}}])
        cases = {
            "before interaction.completed": complete[:-1],
            "steps were still open": complete[:4] + complete[-1:],
            "complete JSON": googleInteractionEvents([{"type": "function_call", "id": "c1", "name": "get_weather", "argumentChunks": ['{"location": "Par']}]),
            "not a JSON object": googleInteractionEvents([{"type": "function_call", "id": "c1", "name": "get_weather", "argumentChunks": ["[1, 2]"]}]),
            "id and name": googleInteractionEvents([{"type": "function_call", "id": "", "name": "get_weather", "arguments": {}}]),
            "same id": googleInteractionEvents([{"type": "function_call", "id": "c1", "name": "a", "arguments": {}}, {"type": "function_call", "id": "c1", "name": "b", "arguments": {}}]),
            "carried no function call": googleInteractionEvents([{"type": "text", "text": "x"}], status="requires_action"),
        }
        for reason, events in cases.items():
            with self.subTest(reason=reason):
                client = FakeGoogleClient([events])
                with self.assertRaises(ProviderIncompleteResponseError) as caught:
                    _send(client)
                self.assertEqual(caught.exception.record["category"], "protocol")
                self.assertIn(reason, caught.exception.details["reason"])
                self.assertEqual(caught.exception.details["mode"], "interactions")
                self.assertEqual(client.closed_streams, 1)

    def test_a_dropped_connection_is_retried_before_output_only(self):
        ok = googleInteractionEvents([{"type": "text", "text": "ok"}])
        client = FakeGoogleClient([ConnectionResetError("reset"), ok])
        self.assertEqual(_send(client, maxAttempts=2)["content"], [{"type": "text", "text": "ok"}])
        mid = googleInteractionEvents([{"type": "text", "text": "partial"}])[:4] + [ConnectionResetError("reset mid-stream")]
        record, client = self._fail([mid, ok], maxAttempts=3)
        self.assertEqual((record["category"], record["hadAssistantOutput"], record["phase"]), ("transport", True, "stream_read"))
        self.assertEqual(len(client.requests), 1)

    def test_a_silent_stream_times_out_and_a_cancelled_turn_stops_it(self):
        async def hang():
            await asyncio.sleep(30)

        head = googleInteractionEvents([{"type": "text", "text": "x"}])[:2]
        record, client = self._fail([head + [hang]], streamIdleTimeoutMs=40)
        self.assertEqual((record["category"], record["code"]), ("timeout", "provider_stream_idle_timeout"))
        self.assertEqual(client.closed_streams, 1)

        signal = AbortSignal()
        client = FakeGoogleClient([head + [hang]])

        async def scenario():
            task = asyncio.ensure_future(send_google_interactions_request({
                "config": {"provider": "google", "googleApiKey": "k", "providerProfile": {"auth": {"kind": "google_api_key"}}},
                "request": _request(), "runtime": {"abortSignal": signal}, "clientFactory": client.factory, "maxAttempts": 3,
            }))
            await asyncio.sleep(0.05)
            signal.abort()
            return await task

        with self.assertRaises(ProviderRequestError) as caught:
            asyncio.run(scenario())
        self.assertEqual(caught.exception.record["category"], "cancelled")
        self.assertEqual((len(client.requests), client.closed_streams), (1, 1))


class GoogleInteractionsWireTests(unittest.TestCase):
    def _server(self, script=None):
        requests: list[dict] = []
        server = FakeProviderHttpServer(provider="google", script=script, request_recorder=requests, record_all_headers=True).start()
        self.addCleanup(server.close)
        return server, requests

    def _args(self, server, **overrides):
        args = {
            "config": {"provider": "google", "googleApiKey": "wire-google-key", "googleBaseUrl": server.base_url, "customHeaders": {"x-team": "blue", "x-goog-api-key": "not-this-one", "Authorization": "Bearer nor-this"}, "providerProfile": {"auth": {"kind": "google_api_key"}}},
            "request": _request(), "runtime": {}, "maxAttempts": 1,
        }
        args.update(overrides)
        return args

    def test_a_request_over_a_real_socket_carries_the_key_in_its_header_only(self):
        server, requests = self._server()
        response = asyncio.run(send_google_interactions_request(self._args(server)))
        self.assertEqual(response["content"], [{"type": "text", "text": "ok"}])
        self.assertEqual(response["stopReason"], "end_turn")
        record = requests[0]
        self.assertEqual(record["path"], "/v1/interactions")
        self.assertNotIn("key", record["path"])
        self.assertEqual(record["headers"]["x-goog-api-key"], "wire-google-key")
        self.assertNotIn("authorization", record["headers"])
        self.assertEqual((record["headers"]["accept"], record["headers"]["content-type"], record["headers"]["x-team"]), ("text/event-stream", "application/json", "blue"))
        self.assertIs(record["body"]["store"], False)
        self.assertEqual(record["body"]["input"], [{"type": "user_input", "content": [{"type": "text", "text": "hello"}]}])

    def test_an_error_body_and_retry_after_reach_the_failure_record(self):
        configure_knobs({"provider": {"google_retry_delays_ms": [1]}})
        self.addCleanup(reset_configured_knobs)
        server, requests = self._server({"interactions": [
            {"status": 429, "body": googleErrorBody("rate_limit_exceeded", "Slow down."), "headers": {"retry-after": "0"}},
            {"contentType": "text/event-stream", "rawBody": googleInteractionsSseBody(googleInteractionEvents([{"type": "text", "text": "after retry"}]))},
        ]})
        response = asyncio.run(send_google_interactions_request(self._args(server, maxAttempts=2)))
        self.assertEqual(response["content"], [{"type": "text", "text": "after retry"}])
        self.assertEqual(len(requests), 2)
        server, _ = self._server({"interactions": [{"status": 401, "body": googleErrorBody("authentication", "API key not valid.")}]})
        with self.assertRaises(ProviderRequestError) as caught:
            asyncio.run(send_google_interactions_request(self._args(server)))
        record = caught.exception.record
        self.assertEqual((record["category"], record["status"], record["code"]), ("auth", 401, "authentication"))
        self.assertNotIn("wire-google-key", json.dumps(record))

    def test_a_json_error_with_a_success_status_is_still_an_error(self):
        server, _ = self._server({"interactions": [{"status": 200, "body": googleErrorBody("invalid_request", "The value is not supported.")}]})
        with self.assertRaises(ProviderRequestError) as caught:
            asyncio.run(send_google_interactions_request(self._args(server)))
        self.assertEqual(caught.exception.record["category"], "client_request")
