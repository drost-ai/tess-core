from __future__ import annotations

from typing import ClassVar

from agent_core.providers.openai_turn_session import OpenAiProviderTurnSession
from tests.support.agent_core_case import AgentCoreTestCase, run


class Package43TurnSessionIntegrationTests(AgentCoreTestCase):
    def test_failed_generation_forces_stable_full_replay(self) -> None:
        async def exercise() -> tuple[dict, dict]:
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
                logical_turn_id="turn-package43",
                session_id="session-package43",
                provider="openai",
                profile="codex",
                model="gpt-5.6-sol",
                runtime={},
            )
            await turn.acquire_client(identity="codex", create=Client)
            first_input = [
                {"type": "message", "role": "user", "content": "work"}
            ]
            first = turn.prepare_responses_request(
                _params(first_input),
                connection_reused=False,
            )
            canonical = [
                {
                    "type": "message",
                    "role": "assistant",
                    "content": "first",
                }
            ]
            await turn.record_responses_success(
                first,
                output_items=canonical,
                canonical_output_items=canonical,
                response_id="response-first",
                turn_state=None,
            )
            second_input = [
                *first_input,
                *canonical,
                {
                    "type": "message",
                    "role": "user",
                    "content": "continue",
                },
            ]
            incremental = turn.prepare_responses_request(
                _params(second_input),
                connection_reused=True,
            )
            await turn.record_responses_failure(
                incremental,
                RuntimeError("stream failed"),
            )
            replay = turn.prepare_responses_request(
                _params(second_input),
                connection_reused=True,
            )
            await turn.close()
            return incremental.wire_params, replay.wire_params

        incremental, replay = run(exercise())
        self.assertEqual(incremental["previous_response_id"], "response-first")
        self.assertNotIn("previous_response_id", replay)
        self.assertEqual(replay["input"][-1]["content"], "continue")
        self.assertEqual(len(replay["input"]), 3)


def _params(input_items: list[dict]) -> dict:
    return {
        "model": "gpt-5.6-sol",
        "instructions": "stable",
        "input": input_items,
        "store": False,
        "stream": True,
    }
