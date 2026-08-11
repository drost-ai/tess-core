from __future__ import annotations

from tests.support.agent_core_case import *

from tests.test_providers_anthropic_contract import _config, _request, _server


class EffortModelSupportTests(unittest.TestCase):
    def test_five_family_models_keep_their_effort(self):
        from agent_core.config.providers import normalize_effort_for_model

        for model in ("claude-fable-5", "claude-mythos-5", "claude-opus-5", "claude-sonnet-5"):
            self.assertEqual(normalize_effort_for_model("anthropic", model, "xhigh"), "xhigh", model)
            self.assertEqual(normalize_effort_for_model("anthropic", model, "max"), "max", model)
            self.assertEqual(normalize_effort_for_model("anthropic", model, "high"), "high", model)

    def test_pre_xhigh_models_downgrade_xhigh_only(self):
        from agent_core.config.providers import normalize_effort_for_model

        self.assertEqual(normalize_effort_for_model("anthropic", "claude-sonnet-4-6", "xhigh"), "high")
        self.assertEqual(normalize_effort_for_model("anthropic", "claude-sonnet-4-6", "max"), "max")
        self.assertEqual(normalize_effort_for_model("anthropic", "claude-opus-4-8", "xhigh"), "xhigh")
        self.assertEqual(normalize_effort_for_model("anthropic", "claude-opus-4-6", "xhigh"), "high")
        self.assertIsNone(normalize_effort_for_model("anthropic", "claude-haiku-4-5", "high"))


class ThinkingDisplayTests(unittest.TestCase):
    def test_profile_thinking_display_reaches_the_wire(self):
        server, requests = _server()
        try:
            run(
                core.send_anthropic_messages_request(
                    config=_config(
                        server,
                        providerProfile={"anthropicThinkingDisplay": "summarized"},
                    ),
                    request=_request(effort="high"),
                    runtime={},
                    maxAttempts=1,
                )
            )
        finally:
            server.close()
        body = requests[0]["body"]
        self.assertEqual(
            body["thinking"], {"type": "adaptive", "display": "summarized"}
        )
        self.assertEqual(body["output_config"], {"effort": "high"})

    def test_no_display_key_without_profile_option(self):
        server, requests = _server()
        try:
            run(
                core.send_anthropic_messages_request(
                    config=_config(server),
                    request=_request(effort="high"),
                    runtime={},
                    maxAttempts=1,
                )
            )
        finally:
            server.close()
        self.assertEqual(requests[0]["body"]["thinking"], {"type": "adaptive"})



if __name__ == "__main__":
    unittest.main()

class ReasoningEffortFallbackTests(unittest.TestCase):
    def test_reasoning_effort_feeds_profile_effort_level(self):
        from agent_core.config.providers import resolve_provider_profile

        profile = resolve_provider_profile(
            "p", {"kind": "anthropic", "reasoningEffort": "xhigh"}
        )
        self.assertEqual(profile["effortLevel"], "xhigh")

    def test_explicit_effort_level_wins_over_reasoning_effort(self):
        from agent_core.config.providers import resolve_provider_profile

        profile = resolve_provider_profile(
            "p",
            {"kind": "anthropic", "effortLevel": "medium", "reasoningEffort": "xhigh"},
        )
        self.assertEqual(profile["effortLevel"], "medium")

    def test_openai_xhigh_covers_gpt_5_4_and_later_minors(self):
        from agent_core.config.providers import normalize_effort_for_model

        self.assertEqual(normalize_effort_for_model("openai", "gpt-5.6-sol", "xhigh"), "xhigh")
        self.assertEqual(normalize_effort_for_model("openai", "gpt-5.4", "xhigh"), "xhigh")
        self.assertEqual(normalize_effort_for_model("openai", "gpt-5.6-sol", "max"), "xhigh")
        self.assertEqual(normalize_effort_for_model("openai", "gpt-5", "xhigh"), "high")
