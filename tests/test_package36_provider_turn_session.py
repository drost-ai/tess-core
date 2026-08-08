from __future__ import annotations

import copy
from types import SimpleNamespace

from tests.support.agent_core_case import *

from agent_core.providers.openai_turn_session import (
    OpenAiProviderTurnSession,
    PreparedOpenAiRequest,
)
from agent_core.providers.turn_session import ProviderTurnSession


class _WebSocketFailure(RuntimeError):
    def __init__(self, stage: str, *, fallback_safe: bool) -> None:
        super().__init__(stage)
        self.stage = stage
        self.fallback_safe = fallback_safe


def _response_event(
    response_id: str,
    output: list[dict[str, object]],
    *,
    input_tokens: int = 10,
) -> dict[str, object]:
    return {
        "type": "response.completed",
        "response": {
            "id": response_id,
            "status": "completed",
            "output": output,
            "usage": {"input_tokens": input_tokens, "output_tokens": 2},
        },
    }


def _tool_output(call_id: str, name: str = "Echo") -> list[dict[str, object]]:
    return [
        {
            "type": "reasoning",
            "id": f"reasoning-{call_id}",
            "encrypted_content": f"encrypted-{call_id}",
        },
        {
            "type": "function_call",
            "id": f"item-{call_id}",
            "call_id": call_id,
            "name": name,
            "arguments": '{"value":"ok"}',
            "status": "completed",
        },
    ]


def _config(cwd: str, *, incremental: bool = True) -> dict[str, object]:
    capabilities = {
        "openaiResponsesIncremental": incremental,
        "openaiResponsesWebSocket": incremental,
        "openaiTurnState": incremental,
    }
    return {
        "cwd": cwd,
        "runtimeHome": cwd,
        "allowedRoots": [cwd],
        "agents": {},
        "provider": "openai",
        "providerKind": "openai",
        "providerProfileName": "codex",
        "providerProfile": {
            "kind": "openai",
            "name": "codex",
            "capabilities": capabilities,
            "requestOptions": {},
        },
        "providerProfiles": {"codex": {"kind": "openai", "name": "codex"}},
        "customHeaders": {},
        "openaiApiMode": "codex",
        "openaiBaseUrl": testing.DEFAULT_CODEX_BASE_URL,
        "openaiToken": "test-token",
        "model": "gpt-5.4",
        "effort": "high",
        "assistantName": "Test",
        "assistantDescription": "",
        "systemPrompt": "test",
        "tools": ["Echo"],
        "allowedTools": ["Echo"],
        "disallowedTools": [],
        "permissionMode": "bypassPermissions",
        "sessionPersistence": False,
    }


class Package36ProviderTurnSessionTests(AgentCoreTestCase):
    def test_cancel_closes_the_exact_stream_and_activity_task(self):
        async def exercise() -> tuple[bool, int]:
            closed = 0

            class Stream:
                async def aclose(self):
                    nonlocal closed
                    closed += 1

            async def pending():
                await asyncio.Event().wait()

            session = ProviderTurnSession(
                logical_turn_id="turn-cancel",
                session_id="session-cancel",
                provider="custom",
                profile="custom",
                model="model",
                runtime={},
            )
            task = asyncio.create_task(pending())
            session.bind_activity_task(task)
            session.bind_stream(Stream())
            await session.cancel("test_cancel")
            await session.close()
            return task.cancelled(), closed

        task_cancelled, closed = run(exercise())
        self.assertTrue(task_cancelled)
        self.assertEqual(closed, 1)

    def test_sse_store_false_replays_every_raw_response_item(self):
        captures: list[dict[str, object]] = []
        constructions = 0

        class Client:
            capabilities: dict[str, object] = {}

            def __init__(self) -> None:
                self.responses = self

            async def create(self, params: dict[str, object]) -> AsyncChunks:
                captures.append(copy.deepcopy(params))
                if len(captures) == 1:
                    return AsyncChunks(
                        [_response_event("response-1", _tool_output("call-1"))]
                    )
                return AsyncChunks(
                    [
                        _response_event(
                            "response-2",
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

        def factory(_options: dict[str, object]) -> Client:
            nonlocal constructions
            constructions += 1
            return Client()

        turn = OpenAiProviderTurnSession(
            config={"providerProfile": {"capabilities": {}}},
            logical_turn_id="turn-sse",
            session_id="session-sse",
            provider="openai",
            profile="openai",
            model="gpt-5.4",
            runtime={},
        )
        first_request = {
            "model": "gpt-5.4",
            "system": "system",
            "messages": [{"role": "user", "content": "start"}],
            "tools": [],
        }
        run(
            testing.sendOpenAiResponsesRequest(
                token="test",
                mode="responses",
                request=first_request,
                runtime={},
                maxAttempts=1,
                clientFactory=factory,
                turnSession=turn,
            )
        )
        second_request = {
            **first_request,
            "messages": [
                *first_request["messages"],
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "call-1",
                            "name": "Echo",
                            "input": {"value": "ok"},
                        }
                    ],
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "call-1",
                            "content": "ok",
                        }
                    ],
                },
            ],
        }
        run(
            testing.sendOpenAiResponsesRequest(
                token="test",
                mode="responses",
                request=second_request,
                runtime={},
                maxAttempts=1,
                clientFactory=factory,
                turnSession=turn,
            )
        )
        run(turn.close())

        self.assertEqual(constructions, 1)
        self.assertFalse(captures[0]["store"])
        self.assertFalse(captures[1]["store"])
        self.assertNotIn("previous_response_id", captures[1])
        second_types = [item["type"] for item in captures[1]["input"]]
        self.assertEqual(
            second_types,
            ["message", "reasoning", "function_call", "function_call_output"],
        )
        self.assertEqual(
            captures[1]["input"][1]["encrypted_content"], "encrypted-call-1"
        )

    def test_request_compatibility_changes_force_canonical_full_request(self):
        base = {
            "model": "gpt-5.4",
            "instructions": "system",
            "input": [{"type": "message", "role": "user", "content": "start"}],
            "tools": [{"type": "function", "name": "Echo"}],
            "tool_choice": "auto",
            "parallel_tool_calls": True,
            "reasoning": {"effort": "high"},
            "store": False,
            "stream": True,
            "include": ["reasoning.encrypted_content"],
            "service_tier": "default",
            "prompt_cache_key": "stable",
            "text": {"format": {"type": "text"}},
            "max_output_tokens": 100,
            "temperature": 0.5,
            "top_p": 0.9,
            "truncation": "disabled",
            "metadata": {"route": "one"},
            "user": "user-one",
            "background": False,
            "extra_body": {"vendor": {"mode": "one"}},
        }
        changes = {
            "model": "gpt-5.4-mini",
            "instructions": "changed",
            "tools": [],
            "tool_choice": "required",
            "parallel_tool_calls": False,
            "reasoning": {"effort": "low"},
            "store": True,
            "stream": False,
            "include": [],
            "service_tier": "priority",
            "prompt_cache_key": "changed",
            "text": {"format": {"type": "json_schema"}},
            "max_output_tokens": 101,
            "temperature": 0.6,
            "top_p": 0.8,
            "truncation": "auto",
            "metadata": {"route": "two"},
            "user": "user-two",
            "background": True,
            "extra_body": {"vendor": {"mode": "two"}},
        }

        async def exercise() -> None:
            turn = OpenAiProviderTurnSession(
                config={
                    "providerProfile": {
                        "capabilities": {
                            "openaiResponsesIncremental": True,
                            "openaiResponsesWebSocket": True,
                        }
                    }
                },
                logical_turn_id="turn-properties",
                session_id="session-properties",
                provider="openai",
                profile="codex",
                model="gpt-5.4",
                runtime={},
            )
            client = SimpleNamespace(
                capabilities={
                    "openaiResponsesIncremental": True,
                    "openaiResponsesWebSocket": True,
                }
            )
            await turn.acquire_client(identity="client", create=lambda: client)
            first = turn.prepare_responses_request(base, connection_reused=False)
            canonical = [{"type": "function_call", "call_id": "call-1"}]
            await turn.record_responses_success(
                first,
                output_items=canonical,
                canonical_output_items=canonical,
                response_id="response-1",
                turn_state=None,
            )
            extension = [
                *base["input"],
                *canonical,
                {
                    "type": "function_call_output",
                    "call_id": "call-1",
                    "output": "ok",
                },
            ]
            compatible = turn.prepare_responses_request(
                {**base, "input": extension}, connection_reused=True
            )
            self.assertEqual(compatible.projection, "incremental")
            for key, changed in changes.items():
                prepared = turn.prepare_responses_request(
                    {**base, key: changed, "input": extension},
                    connection_reused=True,
                )
                self.assertEqual(prepared.projection, "full", key)
                self.assertNotIn("previous_response_id", prepared.wire_params, key)
                self.assertEqual(
                    prepared.chain_reset_reason, "request_properties_changed", key
                )
            mismatch = copy.deepcopy(extension)
            mismatch[0] = {
                "type": "message",
                "role": "user",
                "content": "rewritten",
            }
            prepared = turn.prepare_responses_request(
                {**base, "input": mismatch}, connection_reused=True
            )
            self.assertEqual(prepared.projection, "full")
            self.assertEqual(prepared.chain_reset_reason, "input_prefix_mismatch")
            await turn.close()

        run(exercise())

    def test_completed_then_malformed_stream_does_not_advance_response_chain(self):
        async def exercise() -> PreparedOpenAiRequest:
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
                    event = _response_event(
                        "must-not-advance",
                        [
                            {
                                "type": "message",
                                "content": [
                                    {"type": "output_text", "text": "partial"}
                                ],
                            }
                        ],
                    )
                    # A duplicate terminal is stream corruption; the committed
                    # chain state must not advance from it.
                    return AsyncChunks([event, event])

            turn = OpenAiProviderTurnSession(
                config={
                    "providerProfile": {
                        "capabilities": {
                            "openaiResponsesIncremental": True,
                            "openaiResponsesWebSocket": True,
                        }
                    }
                },
                logical_turn_id="turn-incomplete",
                session_id="session-incomplete",
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
            prepared = turn.prepare_responses_request(
                {
                    "model": "gpt-5.4",
                    "instructions": "system",
                    "input": [{"type": "message", "role": "user", "content": "start"}],
                    "store": False,
                    "stream": True,
                },
                connection_reused=True,
            )
            await turn.close()
            return prepared

        prepared = run(exercise())
        self.assertEqual(prepared.projection, "full")
        self.assertNotIn("previous_response_id", prepared.wire_params)
