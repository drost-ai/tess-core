from __future__ import annotations

import base64
import json
import os
import tempfile
import unittest

from tests.support.agent_core_case import bind_test_runtime, run

import agent_core as core
from agent_core import testing
from agent_core.payloads.image_budget import ImageBudgetError


def _block(data: bytes) -> dict:
    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": "image/png",
            "data": base64.b64encode(data).decode("ascii"),
        },
    }


def _config(cwd: str) -> dict:
    capabilities = {
        "imageInput": True,
        "imageMimeTypes": ["image/png"],
        "maxImageCount": 8,
        "maxImageBytes": 5 * 1024 * 1024,
        "maxAggregateImageBytes": 20 * 1024 * 1024,
        "maxEncodedImageRequestBytes": 24 * 1024 * 1024,
    }
    return {
        "cwd": cwd,
        "runtimeHome": cwd,
        "allowedRoots": [cwd],
        "agents": {},
        "provider": "openai",
        "providerKind": "openai",
        "providerProfileName": "wire-image",
        "providerProfile": {
            "kind": "openai",
            "name": "wire-image",
            "apiMode": "responses",
            "maxContextLength": 1_000_000,
            "maxOutputTokens": 128_000,
            "capabilities": capabilities,
        },
        "providerCapabilities": capabilities,
        "providerProfiles": {"wire-image": {"kind": "openai", "name": "wire-image"}},
        "openaiApiMode": "responses",
        "model": "gpt-5.4",
        "effort": "medium",
        "systemPrompt": "test",
        "tools": [],
        "allowedTools": [],
        "disallowedTools": [],
        "permissionMode": "bypassPermissions",
        "sessionPersistence": False,
    }


def _session(config: dict) -> dict:
    return core.createEmptySession(
        cwd=config["cwd"],
        provider="openai",
        providerProfileName="wire-image",
        model="gpt-5.4",
        effort="medium",
        systemPrompt="test",
        permissionMode="bypassPermissions",
    )


class Package31MultimodalWireTests(unittest.TestCase):
    def test_canonical_image_shape_serializes_for_all_provider_vocabularies(self):
        block = _block(b"img")
        request = {
            "model": "gpt-5.4",
            "system": "system",
            "messages": [{"role": "user", "content": [{"type": "text", "text": "inspect"}, block]}],
            "tools": [],
        }
        responses = testing.buildResponsesCreateParams({"request": request})
        response_image = responses["input"][0]["content"][1]
        self.assertEqual(response_image["type"], "input_image")
        self.assertEqual(response_image["image_url"], "data:image/png;base64,aW1n")
        chat = testing.buildChatCompletionsCreateParams(request)
        self.assertEqual(
            chat["messages"][1]["content"][1]["image_url"]["url"],
            "data:image/png;base64,aW1n",
        )
        anthropic = testing.buildAnthropicMessagesPayload({**request, "model": "claude-sonnet-4-6"})
        self.assertEqual(anthropic["messages"][0]["content"][1], block)

    def test_token_preflight_and_serialization_use_the_same_four_image_set(self):
        with tempfile.TemporaryDirectory(prefix="agent-core-p31-wire-") as cwd:
            config = _config(cwd)
            session = _session(config)
            blocks = [_block(bytes([value]) * (value * 7)) for value in range(1, 5)]
            events = []
            captured = {}

            async def send(request):
                captured["request"] = request
                return {
                    "model": "gpt-5.4",
                    "stopReason": "end_turn",
                    "usage": {"input_tokens": 1, "output_tokens": 1},
                    "content": [{"type": "text", "text": "done"}],
                }

            run(core.runAgentPrompt(
                session=session,
                config=config,
                runtime=bind_test_runtime(session, {
                    "interactive": False,
                    "askQuestion": lambda *_: "",
                    "tools": [],
                    "emitEvent": lambda event, data: events.append((event, data)),
                }),
                prompt=[{"type": "text", "text": "compare all four"}, *blocks],
                providerOverride={"send": send},
            ))
            serialized = json.dumps(testing.buildResponsesCreateParams({"request": captured["request"]}))
            for block in blocks:
                self.assertEqual(serialized.count(block["source"]["data"]), 1)
            measurement = next(data for event, data in events if event == "provider.input_tokens")
            self.assertEqual(measurement["image_count"], 4)
            self.assertEqual(measurement["image_decoded_bytes"], 70)
            self.assertGreaterEqual(measurement["image_input_tokens"], 4 * 255)
            self.assertRegex(measurement["image_set_fingerprint"], r"^[a-f0-9]{64}$")
            self.assertNotIn(blocks[0]["source"]["data"], json.dumps(session))

    def test_context_capacity_rejects_images_before_provider_dispatch(self):
        with tempfile.TemporaryDirectory(prefix="agent-core-p31-context-") as cwd:
            config = _config(cwd)
            config["providerProfile"]["maxContextLength"] = 5_000
            config["providerProfile"]["maxOutputTokens"] = 16_384
            session = _session(config)
            png = bytearray(24)
            png[:8] = b"\x89PNG\r\n\x1a\n"
            png[16:20] = (5_000).to_bytes(4, "big")
            png[20:24] = (5_000).to_bytes(4, "big")
            events = []

            async def send(_request):
                raise AssertionError("provider must not run")

            with self.assertRaises(ImageBudgetError) as caught:
                run(core.runAgentPrompt(
                    session=session,
                    config=config,
                    runtime=bind_test_runtime(session, {
                        "interactive": False,
                        "askQuestion": lambda *_: "",
                        "tools": [],
                        "emitEvent": lambda event, data: events.append((event, data)),
                    }),
                    prompt=[{"type": "text", "text": "inspect"}, _block(bytes(png))],
                    providerOverride={"send": send},
                ))
            self.assertEqual(caught.exception.code, "image_context_limit")
            self.assertEqual(session["messages"], [])
            rejection = next(data for event, data in events if event == "provider.image_budget_rejected")
            self.assertEqual(rejection["imageCount"], 1)
            self.assertGreaterEqual(rejection["images"][0]["estimatedInputTokens"], 765)

    def test_parallel_read_reservation_rejects_overflow_before_second_file_acquisition(self):
        with tempfile.TemporaryDirectory(prefix="agent-core-p31-order-") as cwd:
            config = _config(cwd)
            config["tools"] = config["allowedTools"] = ["Read"]
            config["providerCapabilities"] = {**config["providerCapabilities"], "maxImageCount": 1}
            config["providerProfile"]["capabilities"] = config["providerCapabilities"]
            session = _session(config)
            first = os.path.join(cwd, "first.png")
            second = os.path.join(cwd, "second.png")
            with open(first, "wb") as handle:
                handle.write(b"\x89PNG\r\n\x1a\n" + b"a" * 128)
            with open(second, "wb") as handle:
                handle.write(b"\x89PNG\r\n\x1a\n" + b"b" * 128)
            os.chmod(second, 0)
            events = []
            captured = {}
            calls = 0

            async def send(request):
                nonlocal calls
                calls += 1
                if calls == 1:
                    return {
                        "model": "gpt-5.4",
                        "stopReason": "tool_use",
                        "usage": {"input_tokens": 1, "output_tokens": 1},
                        "content": [
                            {"type": "tool_use", "id": "read-1", "name": "Read", "input": {"file_path": first}},
                            {"type": "tool_use", "id": "read-2", "name": "Read", "input": {"file_path": second}},
                        ],
                    }
                captured["request"] = request
                return {
                    "model": "gpt-5.4", "stopReason": "end_turn",
                    "usage": {"input_tokens": 1, "output_tokens": 1},
                    "content": [{"type": "text", "text": "done"}],
                }

            try:
                run(core.runAgentPrompt(
                    session=session,
                    config=config,
                    runtime=bind_test_runtime(session, {
                        "interactive": False,
                        "askQuestion": lambda *_: "",
                        "tools": core.createBuiltinTools(),
                        "emitEvent": lambda event, data: events.append((event, data)),
                    }),
                    prompt="read both",
                    providerOverride={"send": send},
                ))
            finally:
                os.chmod(second, 0o600)
            image_blocks = [
                block
                for message in captured["request"]["messages"]
                for block in message.get("content", [])
                if isinstance(block, dict) and block.get("type") == "image"
            ]
            self.assertEqual(len(image_blocks), 1)
            self.assertTrue(any(
                event == "tool.attachment_dropped" and "image count limit exceeded" in str(data.get("reason"))
                for event, data in events
            ))


if __name__ == "__main__":
    unittest.main()
