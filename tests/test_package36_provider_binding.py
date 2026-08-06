from __future__ import annotations

from tests.support.agent_core_case import AgentCoreTestCase

from agent_core.agent_loop.turn_session import (
    resolve_effective_provider_turn_binding,
)
from agent_core.providers.turn_session import createProviderTurnSession
from agent_core.providers.openai_turn_session import OpenAiProviderTurnSession


class Package36ProviderBindingTests(AgentCoreTestCase):
    def test_model_selected_provider_is_bound_before_turn_session_creation(self):
        session = {
            "id": "session-provider-selection",
            "provider": "anthropic",
            "providerProfileName": "anthropic-console",
            "model": "claude-sonnet-4-6",
        }
        config = {
            "provider": "anthropic",
            "providerProfileName": "anthropic-console",
            "providerProfile": {
                "kind": "anthropic",
                "name": "anthropic-console",
            },
            "providerProfiles": {
                "anthropic-console": {
                    "kind": "anthropic",
                    "name": "anthropic-console",
                },
                "openai": {
                    "kind": "openai",
                    "name": "openai",
                    "apiMode": "responses",
                    "baseUrl": "https://api.openai.com/v1",
                    "capabilities": {},
                },
            },
        }
        bound = resolve_effective_provider_turn_binding(
            {
                "session": session,
                "config": config,
                "modelOverride": "gpt-5.6-sol",
            }
        )
        turn_session = createProviderTurnSession(
            logical_turn_id="turn-provider-selection",
            session=session,
            config=bound["config"],
            runtime={},
            model="gpt-5.6-sol",
        )
        self.assertIsInstance(turn_session, OpenAiProviderTurnSession)
        self.assertEqual(session["provider"], "openai")
        self.assertEqual(bound["config"]["provider"], "openai")
        self.assertEqual(bound["config"]["providerProfileName"], "openai")
        self.assertEqual(
            bound["config"]["openaiBaseUrl"],
            "https://api.openai.com/v1",
        )
