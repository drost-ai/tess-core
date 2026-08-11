from __future__ import annotations

import base64
import unittest

from tests.support.agent_core_case import run

import agent_core._facade as core
from agent_core.context.compaction.utils import estimate_message_tokens


MIB = 1024 * 1024


def image_block(data: bytes) -> dict:
    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": "image/png",
            "data": base64.b64encode(data).decode("ascii"),
        },
    }


def image_config() -> dict:
    capabilities = {
        "imageInput": True,
        "imageMimeTypes": ["image/png"],
        "maxImageCount": 8,
        "maxImageBytes": 5 * MIB,
        "maxAggregateImageBytes": 20 * MIB,
        "maxEncodedImageRequestBytes": 24 * MIB,
    }
    return {
        "provider": "openai",
        "providerKind": "openai",
        "providerProfileName": "image-test",
        "providerProfile": {
            "kind": "openai",
            "name": "image-test",
            "apiMode": "responses",
            "capabilities": capabilities,
        },
        "providerCapabilities": capabilities,
        "model": "gpt-5.4",
        "openaiApiMode": "responses",
    }


class ImageTokenEstimationTests(unittest.TestCase):
    def test_nested_image_base64_is_opaque_but_vision_cost_remains(self):
        def message(data: bytes) -> dict:
            return {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Build the site from this logo."},
                    image_block(data),
                ],
            }

        small_message = message(b"x")
        large_message = message(b"x" * (2 * MIB))
        small_pressure = core.estimate_raw_context_pressure(
            {"messages": [small_message]}
        )
        large_pressure = core.estimate_raw_context_pressure(
            {"messages": [large_message]}
        )

        self.assertEqual(
            large_pressure["rawTokenEstimate"],
            small_pressure["rawTokenEstimate"],
        )
        self.assertLess(large_pressure["rawTokenEstimate"], 100)
        self.assertFalse(
            core.decide_durable_checkpoint(
                {"messages": [large_message]},
                tokenLimit=1_000,
            )["shouldCheckpoint"]
        )
        self.assertEqual(
            estimate_message_tokens([large_message]),
            estimate_message_tokens([small_message]),
        )

        measurement = run(
            core.measure_request_input_tokens(
                config=image_config(),
                request={
                    "model": "gpt-5.4",
                    "system": "test",
                    "tools": [],
                    "messages": [large_message],
                },
                session={},
            )
        )
        self.assertEqual(measurement["imageCount"], 1)
        self.assertEqual(measurement["imageDecodedBytes"], 2 * MIB)
        self.assertGreater(measurement["imageInputTokens"], 0)
        self.assertLess(measurement["inputTokens"], 10_000)


if __name__ == "__main__":
    unittest.main()
