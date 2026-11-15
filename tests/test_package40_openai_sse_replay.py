from __future__ import annotations

from typing import ClassVar

from tess_core.providers.openai_turn_session import (
    OpenAiProviderTurnSession,
    PreparedOpenAiRequest,
    _complete_replay_output_items,
)
from tests.support.tess_core_case import TessCoreTestCase, run


class Package40OpenAiSseReplayTests(TessCoreTestCase):
    def test_full_replay_fills_tool_call_omitted_by_terminal_output(self) -> None:
        async def exercise() -> PreparedOpenAiRequest:
            class Client:
                capabilities: ClassVar[dict[str, bool]] = {
                    "openaiResponsesIncremental": True,
                    "openaiResponsesWebSocket": True,
                }

            turn = OpenAiProviderTurnSession(
                config={
                    "providerProfile": {
                        "capabilities": {
                            "openaiResponsesIncremental": True,
                            "openaiResponsesWebSocket": True,
                        }
                    }
                },
                logical_turn_id="turn-sse-empty-terminal",
                session_id="session-sse-empty-terminal",
                provider="openai",
                profile="codex",
                model="gpt-5.4",
                runtime={},
            )
            await turn.acquire_client(identity="codex", create=Client)
            first_input = [
                {"type": "message", "role": "user", "content": "start"}
            ]
            first = turn.prepare_responses_request(
                {
                    "model": "gpt-5.4",
                    "instructions": "system",
                    "input": first_input,
                    "store": False,
                    "stream": True,
                },
                connection_reused=False,
            )
            first = turn.fallback_to_sse(first, "auth_401")
            canonical_call = {
                "type": "function_call",
                "call_id": "call-1",
                "name": "Echo",
                "arguments": '{"value":"ok"}',
            }
            await turn.record_responses_success(
                first,
                output_items=[],
                canonical_output_items=[canonical_call],
                response_id="response-1",
                turn_state=None,
            )
            second = turn.prepare_responses_request(
                {
                    "model": "gpt-5.4",
                    "instructions": "system",
                    "input": [
                        *first_input,
                        canonical_call,
                        {
                            "type": "function_call_output",
                            "call_id": "call-1",
                            "output": "ok",
                        },
                    ],
                    "store": False,
                    "stream": True,
                },
                connection_reused=True,
            )
            await turn.close()
            return second

        prepared = run(exercise())
        self.assertEqual(prepared.projection, "full")
        self.assertNotIn("previous_response_id", prepared.wire_params)
        self.assertEqual(
            [item["type"] for item in prepared.wire_params["input"]],
            ["message", "function_call", "function_call_output"],
        )

    def test_replay_preserves_reasoning_and_deduplicates_calls(self) -> None:
        reasoning = {
            "type": "reasoning",
            "id": "reasoning-1",
            "encrypted_content": "encrypted-1",
        }
        function_call = {
            "type": "function_call",
            "call_id": "call-1",
            "name": "Echo",
            "arguments": "{}",
        }
        repaired = _complete_replay_output_items([reasoning], [function_call])
        complete = _complete_replay_output_items(
            [reasoning, function_call],
            [function_call],
        )

        self.assertEqual(repaired, [reasoning, function_call])
        self.assertEqual(complete, [reasoning, function_call])
