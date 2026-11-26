"""A reasoning effort that a profile or a session names reaches the model.

An OpenAI-kind profile used to carry an effort only for models whose name the
runtime knew, and only to api.openai.com. A profile for another service
(Fireworks, Z.ai, DeepSeek, a local server) could name `reasoningEffort` and
have it dropped without a word.
"""
from __future__ import annotations

from tests.support.tess_core_case import *

import tess_core.runtime.host as host_module
from tess_core import ProviderRegistry, SessionOptions, start_runtime
from tess_core.config.providers import is_openai_compatible_service, normalize_effort_for_model, profile_declares_effort


def _profile(extra: str = "", *, model: str = "accounts/fireworks/models/kimi-k3p1-pro") -> str:
    return (
        "    kind: openai\n"
        "    apiMode: chat_completions\n"
        "    baseUrl: https://api.fireworks.ai/inference/v1/\n"
        f"    defaultModel: {model}\n"
        f"{extra}"
        "    auth:\n"
        "      kind: custom_bearer\n"
        "      envVar: SYNTHETIC_FIREWORKS\n"
    )


REGISTRY = (
    "schema_version: 1\n"
    "providerProfiles:\n"
    "  named:\n" + _profile("    reasoningEffort: max\n") +
    "  silent:\n" + _profile() +
    "  off:\n" + _profile("    reasoningEffort: high\n    reasoningEffortEnabled: false\n") +
    "  on:\n" + _profile("    reasoningEffortEnabled: true\n") +
    "  newer:\n"
    "    kind: openai\n"
    "    apiMode: responses\n"
    "    defaultModel: gpt-6.1-sol\n"
    "    reasoningEffort: max\n"
    "    auth:\n"
    "      kind: openai_api_key\n"
    "      envVar: SYNTHETIC_OPENAI\n"
)

FIREWORKS = {"kind": "openai", "apiMode": "chat_completions", "baseUrl": "https://api.fireworks.ai/inference/v1/"}


class NamedReasoningEffortTests(TessCoreTestCase):
    def setUp(self):
        host_module._ACTIVE = None

    def test_which_profiles_are_another_companys_service(self):
        self.assertTrue(is_openai_compatible_service(FIREWORKS))
        self.assertTrue(is_openai_compatible_service({"kind": "openai", "apiMode": "responses", "baseUrl": "https://api.x.ai/v1/"}))
        self.assertTrue(is_openai_compatible_service({"kind": "openai", "apiMode": "chat_completions", "baseUrl": "http://127.0.0.1:8080/v1"}))
        # OpenAI's own API, with or without its address written out, and the Codex backend.
        self.assertFalse(is_openai_compatible_service({"kind": "openai", "apiMode": "responses"}))
        self.assertFalse(is_openai_compatible_service({"kind": "openai", "apiMode": "chat_completions", "baseUrl": "https://api.openai.com/v1"}))
        self.assertFalse(is_openai_compatible_service({"kind": "openai", "apiMode": "codex", "baseUrl": "https://chatgpt.com/backend-api/codex"}))
        self.assertFalse(is_openai_compatible_service({"kind": "anthropic", "baseUrl": "https://api.kimi.com/coding/v1"}))
        self.assertFalse(is_openai_compatible_service(None))

    def test_a_profile_declares_effort_by_naming_one_or_by_its_switch(self):
        self.assertIs(profile_declares_effort({"reasoningEffort": "max"}), True)
        self.assertIs(profile_declares_effort({"effortLevel": "low"}), True)
        self.assertIs(profile_declares_effort({"reasoningEffortEnabled": True}), True)
        self.assertIs(profile_declares_effort({}, named=True), True)  # the session named one
        # The switch turns effort off whatever else is said.
        self.assertIs(profile_declares_effort({"reasoningEffort": "max", "reasoningEffortEnabled": False}), False)
        self.assertIs(profile_declares_effort({"reasoningEffortEnabled": False}, named=True), False)
        # Nothing said: the model's name decides, as before.
        self.assertIsNone(profile_declares_effort({}))
        self.assertIsNone(profile_declares_effort(None))

    def test_a_declared_effort_survives_a_model_the_runtime_does_not_know(self):
        kimi = "accounts/fireworks/models/kimi-k3p1-pro"
        # Another company's service takes its own values: what was named is what is sent.
        for level in ("low", "medium", "high", "xhigh", "max"):
            self.assertEqual(normalize_effort_for_model("openai", kimi, level, declared=True, compatible=True), level)
        # OpenAI's own API has no "max": a model the runtime does not know is asked for its deepest.
        self.assertEqual(normalize_effort_for_model("openai", "gpt-6.1-sol", "max", declared=True), "xhigh")
        self.assertEqual(normalize_effort_for_model("openai", "gpt-6.1-sol", "xhigh", declared=True), "xhigh")
        self.assertEqual(normalize_effort_for_model("openai", "gpt-6.1-sol", "low", declared=True), "low")
        # A model the runtime does know keeps its own ladder.
        self.assertEqual(normalize_effort_for_model("openai", "gpt-5.3-codex-spark", "xhigh", declared=True), "high")

    def test_the_switch_turns_effort_off_for_every_model(self):
        for model, compatible in (("gpt-5.4", False), ("gpt-6.1-sol", False), ("accounts/fireworks/models/kimi-k3p1-pro", True)):
            self.assertIsNone(normalize_effort_for_model("openai", model, "high", declared=False, compatible=compatible))

    def test_nothing_declared_leaves_the_old_behaviour(self):
        self.assertEqual(normalize_effort_for_model("openai", "gpt-5.4", "max"), "xhigh")
        self.assertEqual(normalize_effort_for_model("openai", "gpt-5.3-codex-spark", "xhigh"), "high")
        self.assertIsNone(normalize_effort_for_model("openai", "gpt-6.1-sol", "xhigh"))
        self.assertIsNone(normalize_effort_for_model("openai", "qwen3-coder", "high"))
        # A default effort is never sent to another company's service unasked, whatever its model is called.
        self.assertIsNone(normalize_effort_for_model("openai", "gpt-5.4", "xhigh", compatible=True))
        # Other kinds are untouched by a declaration.
        self.assertEqual(normalize_effort_for_model("anthropic", "claude-fable-5-2", "max", declared=True), "max")
        self.assertIsNone(normalize_effort_for_model("anthropic", "k3.1-pro", "max", declared=True))

    def test_the_registry_reports_the_efforts_of_a_profile_that_declares_them(self):
        root, home, workspace, previous = self.temp_context()
        try:
            path = Path(root.name) / "providers.yaml"
            path.write_text(REGISTRY)
            registry = ProviderRegistry(path)
            self.assertEqual(registry.effort_levels("named"), {"levels": ["low", "medium", "high", "xhigh", "max"], "default": "max"})
            self.assertEqual(registry.effort_levels("on"), {"levels": ["low", "medium", "high", "xhigh", "max"], "default": "xhigh"})
            self.assertEqual(registry.effort_levels("silent"), {"levels": [], "default": None})
            self.assertEqual(registry.effort_levels("off"), {"levels": [], "default": None})
            self.assertEqual(registry.effort_levels("newer"), {"levels": ["low", "medium", "high", "xhigh"], "default": "xhigh"})
        finally:
            self.restore_home(root, previous)

    def _session_turn(self, options: SessionOptions) -> dict:
        root, home, workspace, previous = self.temp_context()

        async def scenario():
            path = Path(root.name) / "providers.yaml"
            path.write_text(REGISTRY)
            runtime = await start_runtime(
                str(home), cwd=str(workspace), provider_registry=str(path), daemon_enabled=False, startup_timeout_seconds=20,
                session_options=lambda session_id, reason: options,
            )
            try:
                session = await runtime.sessions.open()
                return await session.run_turn("Hello", tools=[])
            finally:
                await runtime.stop()

        with mock.patch.dict(os.environ, {"SYNTHETIC_FIREWORKS": "synthetic-fireworks", "SYNTHETIC_OPENAI": "synthetic-openai"}):
            try:
                return run(scenario())
            finally:
                self.restore_home(root, previous)

    def _turn(self, provider: str, **options) -> dict:
        """Run one turn on a profile and return the Chat Completions parameters that went
        to the client: everything but the network runs for real."""
        captured: dict = {}

        async def create(params):
            captured["params"] = params
            return AsyncChunks([{"choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 3, "completion_tokens": 1}}])

        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        with mock.patch.object(openai_module.transport, "_create_openai_client", lambda args, base_url: client):
            result = self._session_turn(SessionOptions(provider=provider, **options))
        self.assertEqual(result["text"], "ok")
        return captured["params"]

    def _request(self, provider: str, **options) -> dict:
        """Run one turn on a profile and return the request the runtime built for the provider."""
        seen: list[dict] = []

        async def send(request):
            seen.append(request)
            return {"model": request["model"], "stopReason": "end_turn", "usage": {"input_tokens": 1, "output_tokens": 1}, "content": [{"type": "text", "text": "ok"}]}

        self._session_turn(SessionOptions(provider=provider, provider_override={"send": send}, **options))
        return seen[0]

    def test_a_profile_that_names_an_effort_sends_it_to_a_compatible_service(self):
        params = self._turn("named")
        self.assertEqual(params["model"], "accounts/fireworks/models/kimi-k3p1-pro")
        self.assertEqual(params["reasoning_effort"], "max")
        self.assertNotIn("store", params)  # still OpenAI's own parameter

    def test_a_profile_that_names_no_effort_sends_none(self):
        self.assertNotIn("reasoning_effort", self._turn("silent"))

    def test_a_session_that_names_an_effort_sends_it(self):
        self.assertEqual(self._turn("silent", effort="low")["reasoning_effort"], "low")
        self.assertEqual(self._turn("named", effort="medium")["reasoning_effort"], "medium")

    def test_the_switch_off_sends_none_even_when_an_effort_is_named(self):
        self.assertNotIn("reasoning_effort", self._turn("off"))
        self.assertNotIn("reasoning_effort", self._turn("off", effort="high"))

    def test_the_switch_on_sends_the_default_effort(self):
        self.assertEqual(self._turn("on")["reasoning_effort"], "xhigh")

    def test_a_named_effort_reaches_an_openai_model_newer_than_the_runtime_knows(self):
        request = self._request("newer")
        self.assertEqual(request["model"], "gpt-6.1-sol")
        self.assertEqual(request["effort"], "xhigh")  # OpenAI has no "max"; the Responses API sends this as reasoning.effort
        self.assertEqual(self._request("newer", effort="low")["effort"], "low")

    def test_the_chat_sender_takes_the_declaration_over_the_address(self):
        captured: dict = {}

        async def create(params):
            captured["params"] = params
            return AsyncChunks([{"choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 3, "completion_tokens": 1}}])

        def factory(_options):
            return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))

        def send(**extra):
            request = {"model": "some-model", "effort": "high", "system": "s", "messages": [{"role": "user", "content": "Hello"}], "tools": []}
            run(testing.send_openai_chat_completions_request({"token": "t", "request": request, "runtime": {}, "maxAttempts": 1, "clientFactory": factory, **extra}))
            return captured["params"]

        compatible = "https://api.deepinfra.com/v1/openai"
        self.assertNotIn("reasoning_effort", send(baseUrl=compatible))  # nothing declared: the address decides
        self.assertEqual(send(baseUrl=compatible, reasoningEffortEnabled=True)["reasoning_effort"], "high")
        self.assertEqual(send()["reasoning_effort"], "high")  # OpenAI's own API
        self.assertNotIn("reasoning_effort", send(reasoningEffortEnabled=False))


if __name__ == "__main__":
    unittest.main()
