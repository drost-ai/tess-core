from __future__ import annotations

import unittest

import agent_core._facade as core
from agent_core.payloads.attachments import resolve_runtime_provider_capabilities


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


if __name__ == "__main__":
    unittest.main()
