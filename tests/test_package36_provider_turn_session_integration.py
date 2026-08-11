from __future__ import annotations

import copy
import json
import tempfile
from types import SimpleNamespace

from tests import test_package36_provider_turn_session as p36
from tests.support.agent_core_case import (
    AgentCoreTestCase,
    AsyncChunks,
    bind_test_runtime,
    core,
    run,
    testing,
)

from agent_core.providers.openai_turn_session import OpenAiProviderTurnSession


class Package36ProviderTurnSessionIntegrationTests(AgentCoreTestCase):
    def test_three_round_turn_reuses_one_client_and_sends_strict_deltas(self):
        with tempfile.TemporaryDirectory(prefix="agent-core-p36-") as cwd:
            config = p36._config(cwd)
            # This test isolates provider-session transport reuse. Its deliberately
            # large prompt remains below the explicit proactive-compaction target
            # so Package 35's synthesis client is not part of the construction count.
            config["providerProfile"]["compactionThresholdTokens"] = 200_000
            config["providerProfile"]["compactionTargetTokens"] = 190_000
            config["providerProfile"]["contextReliefThresholdTokens"] = 200_000
            config["providerProfile"]["contextReliefTargetTokens"] = 190_000
            session = core.create_empty_session(
                cwd=cwd,
                provider="openai",
                providerProfileName="codex",
                model="gpt-5.4",
                effort="high",
                systemPrompt="test",
                permissionMode="bypassPermissions",
            )
            constructions = 0
            closes = 0
            wire: list[dict[str, object]] = []
            events: list[tuple[str, dict[str, object]]] = []
            outputs = [
                p36._tool_output("call-1"),
                p36._tool_output("call-2"),
                [
                    {
                        "type": "message",
                        "id": "message-final",
                        "role": "assistant",
                        "status": "completed",
                        "content": [{"type": "output_text", "text": "done"}],
                    }
                ],
            ]

            class Client:
                capabilities = {
                    "openaiResponsesIncremental": True,
                    "openaiResponsesWebSocket": True,
                    "openaiTurnState": True,
                }

                def __init__(self) -> None:
                    self.responses = self
                    self.chat = SimpleNamespace(completions=SimpleNamespace())

                async def create_websocket(
                    self, params: dict[str, object]
                ) -> AsyncChunks:
                    wire.append(copy.deepcopy(params))
                    index = len(wire) - 1
                    stream = AsyncChunks(
                        [
                            p36._response_event(
                                f"response-{index + 1}", outputs[index]
                            )
                        ]
                    )
                    stream.turn_state = "sticky-turn-state"
                    return stream

                async def close(self) -> None:
                    nonlocal closes
                    closes += 1

            def factory(_options: dict[str, object]) -> Client:
                nonlocal constructions
                constructions += 1
                return Client()

            async def echo_handler(
                payload: dict[str, object], _context: dict[str, object]
            ) -> dict[str, object]:
                return {"content": json.dumps(payload, sort_keys=True)}

            tool = core.ToolDefinition(
                name="Echo",
                description="Return the input.",
                inputSchema={
                    "type": "object",
                    "properties": {"value": {"type": "string"}},
                },
                handler=echo_handler,
            )
            result = run(
                core.runAgentPrompt(
                    session=session,
                    config=config,
                    runtime=bind_test_runtime(
                        session,
                        {
                            "emitEvent": lambda event, data: events.append(
                                (event, data)
                            ),
                            "tools": [tool],
                            "openaiClientFactory": factory,
                        },
                    ),
                    prompt="x" * 700_000,
                )
            )

            self.assertEqual(result["text"], "done")
            self.assertEqual(constructions, 1)
            self.assertEqual(closes, 1)
            self.assertEqual(len(wire), 3)
            self.assertNotIn("previous_response_id", wire[0])
            self.assertGreater(len(json.dumps(wire[0]["input"])), 700_000)
            self.assertEqual(wire[1]["previous_response_id"], "response-1")
            self.assertEqual(wire[2]["previous_response_id"], "response-2")
            self.assertEqual(
                [item["type"] for item in wire[1]["input"]],
                ["function_call_output"],
            )
            self.assertEqual(
                [item["type"] for item in wire[2]["input"]],
                ["function_call_output"],
            )
            projection_events = [
                data
                for event, data in events
                if event == "provider.request_started"
            ]
            self.assertEqual(
                [event["input_projection"] for event in projection_events],
                ["full", "incremental", "incremental"],
            )
            self.assertLess(
                projection_events[1]["transmitted_input_bytes"],
                projection_events[1]["logical_input_bytes"] // 10,
            )
            activity = [
                data for event, data in events if event == "provider.activity"
            ]
            self.assertTrue(activity)
            self.assertEqual(
                len({item["logical_turn_id"] for item in activity}), 1
            )
            by_call: dict[str, list[dict[str, object]]] = {}
            for item in activity:
                by_call.setdefault(str(item["logical_call_id"]), []).append(item)
            self.assertEqual(len(by_call), 3)
            for call_events in by_call.values():
                self.assertEqual(
                    len(
                        [
                            item
                            for item in call_events
                            if item["activity_kind"] == "request_started"
                        ]
                    ),
                    1,
                )
                self.assertEqual(
                    [
                        item["activity_kind"]
                        for item in call_events
                        if item["activity_kind"] == "terminal"
                    ],
                    ["terminal"],
                )
                self.assertEqual(call_events[-1]["activity_kind"], "terminal")
                self.assertEqual(
                    [item["monotonic_sequence"] for item in call_events],
                    list(range(1, len(call_events) + 1)),
                )
            self.assertNotIn("x" * 128, json.dumps(activity))
            persisted = json.dumps(session)
            self.assertNotIn("response-1", persisted)
            self.assertNotIn("sticky-turn-state", persisted)
            self.assertNotIn("encrypted-call-1", persisted)

    def test_websocket_rejection_falls_back_once_but_ambiguous_failure_does_not(self):
        async def exercise_safe() -> tuple[int, int, list[dict[str, object]]]:
            websocket_calls = 0
            sse_calls = 0
            captures: list[dict[str, object]] = []

            class Client:
                capabilities = {
                    "openaiResponsesIncremental": True,
                    "openaiResponsesWebSocket": True,
                }

                def __init__(self) -> None:
                    self.responses = self

                async def create_websocket(
                    self, _params: dict[str, object]
                ) -> AsyncChunks:
                    nonlocal websocket_calls
                    websocket_calls += 1
                    raise p36._WebSocketFailure("auth_401", fallback_safe=True)

                async def create(self, params: dict[str, object]) -> AsyncChunks:
                    nonlocal sse_calls
                    sse_calls += 1
                    captures.append(copy.deepcopy(params))
                    return AsyncChunks(
                        [
                            p36._response_event(
                                f"response-{sse_calls}",
                                [
                                    {
                                        "type": "message",
                                        "content": [
                                            {"type": "output_text", "text": "done"}
                                        ],
                                    }
                                ],
                            )
                        ]
                    )

            turn = OpenAiProviderTurnSession(
                config={
                    "providerProfile": {
                        "capabilities": {
                            "openaiResponsesIncremental": True,
                            "openaiResponsesWebSocket": True,
                        }
                    }
                },
                logical_turn_id="turn-fallback",
                session_id="session-fallback",
                provider="openai",
                profile="codex",
                model="gpt-5.4",
                runtime={},
            )

            def factory(_options):
                return Client()

            request = {
                "model": "gpt-5.4",
                "system": "system",
                "messages": [{"role": "user", "content": "start"}],
                "tools": [],
            }
            for _ in range(2):
                await testing.sendOpenAiResponsesRequest(
                    token="test",
                    mode="codex",
                    request=request,
                    runtime={},
                    maxAttempts=1,
                    clientFactory=factory,
                    turnSession=turn,
                )
            await turn.close()
            return websocket_calls, sse_calls, captures

        websocket_calls, sse_calls, captures = run(exercise_safe())
        self.assertEqual(websocket_calls, 1)
        self.assertEqual(sse_calls, 2)
        self.assertTrue(all(item["store"] is False for item in captures))

        async def exercise_ambiguous() -> int:
            sse_calls = 0

            class Client:
                capabilities = {
                    "openaiResponsesIncremental": True,
                    "openaiResponsesWebSocket": True,
                }

                def __init__(self) -> None:
                    self.responses = self

                async def create_websocket(
                    self, _params: dict[str, object]
                ) -> AsyncChunks:
                    raise p36._WebSocketFailure("receive", fallback_safe=False)

                async def create(self, _params: dict[str, object]) -> AsyncChunks:
                    nonlocal sse_calls
                    sse_calls += 1
                    return AsyncChunks([])

            turn = OpenAiProviderTurnSession(
                config={
                    "providerProfile": {
                        "capabilities": {
                            "openaiResponsesIncremental": True,
                            "openaiResponsesWebSocket": True,
                        }
                    }
                },
                logical_turn_id="turn-ambiguous",
                session_id="session-ambiguous",
                provider="openai",
                profile="codex",
                model="gpt-5.4",
                runtime={},
            )
            with self.assertRaises(core.ProviderRequestError):
                await testing.sendOpenAiResponsesRequest(
                    token="test",
                    mode="codex",
                    request={
                        "model": "gpt-5.4",
                        "system": "system",
                        "messages": [{"role": "user", "content": "start"}],
                        "tools": [],
                    },
                    runtime={},
                    maxAttempts=1,
                    clientFactory=lambda _options: Client(),
                    turnSession=turn,
                )
            await turn.close()
            return sse_calls

        self.assertEqual(run(exercise_ambiguous()), 0)

    def test_websocket_frame_allowlist_and_sse_sticky_header_fallback(self):
        async def exercise() -> tuple[dict[str, object], dict[str, object]]:
            websocket_calls = 0
            round_two_frame: dict[str, object] = {}
            fallback_params: dict[str, object] = {}

            class Client:
                capabilities = {
                    "openaiResponsesIncremental": True,
                    "openaiResponsesWebSocket": True,
                    "openaiTurnState": True,
                }

                def __init__(self) -> None:
                    self.responses = self

                async def create_websocket(
                    self, params: dict[str, object]
                ) -> AsyncChunks:
                    nonlocal websocket_calls, round_two_frame
                    websocket_calls += 1
                    if websocket_calls == 2:
                        round_two_frame = copy.deepcopy(params)
                        raise p36._WebSocketFailure("auth_401", fallback_safe=True)
                    event = p36._response_event(
                        "response-1",
                        [
                            {
                                "type": "message",
                                "content": [
                                    {"type": "output_text", "text": "first"}
                                ],
                            }
                        ],
                    )
                    event["turn_state"] = "sticky-from-event"
                    return AsyncChunks([event])

                async def create(self, params: dict[str, object]) -> AsyncChunks:
                    nonlocal fallback_params
                    fallback_params = copy.deepcopy(params)
                    return AsyncChunks(
                        [
                            p36._response_event(
                                "response-2",
                                [
                                    {
                                        "type": "message",
                                        "content": [
                                            {"type": "output_text", "text": "second"}
                                        ],
                                    }
                                ],
                            )
                        ]
                    )

            turn = OpenAiProviderTurnSession(
                config={
                    "providerProfile": {
                        "capabilities": {
                            "openaiResponsesIncremental": True,
                            "openaiResponsesWebSocket": True,
                            "openaiTurnState": True,
                        }
                    }
                },
                logical_turn_id="turn-wire-projection",
                session_id="session-wire-projection",
                provider="openai",
                profile="codex",
                model="gpt-5.4",
                runtime={},
            )
            request_options = {
                "extra_headers": {"x-user-header": "preserved-on-http"},
                "extra_query": {"debug": "one"},
                "timeout": 123,
                "metadata": {"hostile": True},
                "extra_body": {"sdk_only_extension": "retained-on-sse"},
                "service_tier": "default",
            }
            request = {
                "model": "gpt-5.4",
                "system": "system",
                "messages": [{"role": "user", "content": "start"}],
                "tools": [],
            }
            client = Client()

            def factory(_options):
                return client

            for _ in range(2):
                await testing.sendOpenAiResponsesRequest(
                    token="test",
                    mode="codex",
                    request=request,
                    requestOptions=request_options,
                    runtime={},
                    maxAttempts=1,
                    clientFactory=factory,
                    turnSession=turn,
                )
            await turn.close()
            return round_two_frame, fallback_params

        round_two_frame, fallback = run(exercise())
        legal = {
            "model",
            "instructions",
            "previous_response_id",
            "input",
            "tools",
            "tool_choice",
            "parallel_tool_calls",
            "reasoning",
            "store",
            "stream",
            "stream_options",
            "include",
            "service_tier",
            "prompt_cache_key",
            "text",
            "generate",
            "client_metadata",
        }
        self.assertTrue(set(round_two_frame).issubset(legal))
        self.assertNotIn("extra_headers", round_two_frame)
        self.assertNotIn("extra_query", round_two_frame)
        self.assertNotIn("timeout", round_two_frame)
        self.assertNotIn("metadata", round_two_frame)
        self.assertNotIn("extra_body", round_two_frame)
        self.assertEqual(fallback["extra_query"], {"debug": "one"})
        self.assertEqual(fallback["timeout"], 123)
        self.assertEqual(fallback["metadata"], {"hostile": True})
        self.assertEqual(
            fallback["extra_body"],
            {"sdk_only_extension": "retained-on-sse"},
        )
        self.assertEqual(
            fallback["extra_headers"],
            {
                "x-user-header": "preserved-on-http",
                "x-codex-turn-state": "sticky-from-event",
            },
        )
