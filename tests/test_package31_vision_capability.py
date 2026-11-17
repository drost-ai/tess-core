from __future__ import annotations

import unittest

import tess_core._facade as core
from tess_core.payloads.attachments import resolve_runtime_provider_capabilities


class Package31VisionCapabilityTests(unittest.TestCase):
    """A compatible profile may declare vision support the model name cannot imply.

    Anthropic-compatible endpoints serve non-Claude models, so the default
    capability probe -- which infers image support from the model name -- reports
    text-only for them. An explicit declaration on the profile has to win, or a
    vision-capable route silently loses image input at preflight.
    """

    def test_anthropic_compatible_profile_honors_declared_vision_capability(self):
        kimi = core.resolve_provider_profile(
            "kimi",
            {
                "kind": "anthropic",
                "baseUrl": "https://api.kimi.com/coding/",
                "defaultModel": "k3",
                "supportsVision": True,
                "capabilities": {"vision": True},
            },
        )
        self.assertTrue(kimi["supportsVision"])
        self.assertTrue(kimi["capabilities"]["imageInput"])
        self.assertTrue(
            resolve_runtime_provider_capabilities(
                {"providerProfile": kimi, "providerCapabilities": kimi["capabilities"]}
            )["imageInput"]
        )

    def test_model_name_inference_still_applies_without_a_declaration(self):
        # Guards the fallback the declaration overrides: an undeclared compatible
        # profile on a non-Claude model stays text-only.
        undeclared = core.resolve_provider_profile(
            "kimi-undeclared",
            {
                "kind": "anthropic",
                "baseUrl": "https://api.kimi.com/coding/",
                "defaultModel": "k3",
            },
        )
        self.assertFalse(undeclared["capabilities"]["imageInput"])

    def test_sending_and_attaching_images_follow_one_rule(self):
        # A request with images and an image attached to a tool result used to be
        # decided by different rules: a profile declaring only capabilities.imageInput
        # had its images attached and then answered with "This model does not support
        # image inputs", and Claude 5 profiles had tool images dropped.
        from tess_core.agent.shared import model_supports_image_inputs

        compatible = {"kind": "openai", "apiMode": "chat_completions", "baseUrl": "https://api.deepseek.com"}
        cases = [
            ("imageInput declared", {**compatible, "defaultModel": "deepseek-flash", "capabilities": {"imageInput": True}}, True),
            ("supportsVision declared", {**compatible, "defaultModel": "deepseek-flash", "supportsVision": True}, True),
            ("compatible model, undeclared", {**compatible, "defaultModel": "deepseek-flash"}, False),
            ("Claude 5, undeclared", {"kind": "anthropic", "defaultModel": "claude-opus-5-5"}, True),
            ("Claude, declared text-only", {"kind": "anthropic", "defaultModel": "claude-opus-5-5", "capabilities": {"imageInput": False}}, False),
            ("ChatGPT account, GPT-6", {"kind": "openai", "apiMode": "codex", "defaultModel": "gpt-6.1-sol"}, True),
            ("Kimi, undeclared", {"kind": "anthropic", "baseUrl": "https://api.kimi.com/coding/", "defaultModel": "k3"}, False),
        ]
        for name, record, expected in cases:
            with self.subTest(name):
                profile = core.resolve_provider_profile("p", record)
                config = {
                    "provider": record["kind"],
                    "providerProfile": profile,
                    "providerCapabilities": profile["capabilities"],
                    "model": record["defaultModel"],
                    "openaiApiMode": record.get("apiMode"),
                    "openaiBaseUrl": record.get("baseUrl"),
                }
                self.assertEqual(model_supports_image_inputs(config, record["defaultModel"]), expected)
                self.assertEqual(resolve_runtime_provider_capabilities(config)["imageInput"], expected)
                # A config that never went through profile loading reaches the same answer.
                bare = {**config, "providerProfile": record, "providerCapabilities": None}
                self.assertEqual(model_supports_image_inputs(bare, record["defaultModel"]), expected)
                self.assertEqual(resolve_runtime_provider_capabilities(bare)["imageInput"], expected)


if __name__ == "__main__":
    unittest.main()
