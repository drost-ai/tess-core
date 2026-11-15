from __future__ import annotations

import asyncio
import base64
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.support.tess_core_case import TessCoreTestCase, bind_test_runtime, run

import tess_core._facade as core
from tess_core.payloads.agent_results import finalize_tool_dispatch_result
from tess_core.payloads.image_budget import (
    ImageBudgetError,
    admit_image_messages,
    create_image_budget_reservation,
    estimate_encoded_image_request_bytes,
    measure_admitted_image_set,
    measure_canonical_base64,
    assert_image_context_capacity,
)
from tess_core.payloads.attachments import (
    filter_images_for_provider_capabilities,
    resolve_runtime_provider_capabilities,
    strip_in_memory_image_data,
)


MIB = 1024 * 1024
DEFAULT_CAPABILITIES = {
    "imageInput": True,
    "imageMimeTypes": ["image/png", "image/jpeg", "image/webp", "image/gif"],
    "maxImageCount": 8,
    "maxImageBytes": 5 * MIB,
    "maxAggregateImageBytes": 20 * MIB,
    "maxEncodedImageRequestBytes": 24 * MIB,
}


def image_block(data: bytes, mime_type: str = "image/png") -> dict:
    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": mime_type,
            "data": base64.b64encode(data).decode("ascii"),
        },
    }


def image_config(cwd: str, *, enabled: bool = True, persistence: bool = False) -> dict:
    capabilities = {**DEFAULT_CAPABILITIES, "imageInput": enabled}
    return {
        "cwd": cwd,
        "runtimeHome": cwd,
        "allowedRoots": [cwd],
        "agents": {},
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
        "providerProfiles": {"image-test": {"kind": "openai", "name": "image-test"}},
        "customHeaders": {},
        "openaiApiMode": "responses",
        "model": "gpt-5.4",
        "effort": "medium",
        "assistantName": "Test",
        "assistantDescription": "",
        "systemPrompt": "test",
        "tools": ["Read"],
        "allowedTools": ["Read"],
        "disallowedTools": [],
        "permissionMode": "bypassPermissions",
        "sessionPersistence": persistence,
    }


def create_session(config: dict) -> dict:
    return core.create_empty_session(
        cwd=config["cwd"],
        provider="openai",
        providerProfileName=config["providerProfileName"],
        model=config["model"],
        effort="medium",
        systemPrompt="test",
        permissionMode="bypassPermissions",
    )


class Package31ImageBudgetUnitTests(unittest.TestCase):
    def test_encoded_request_prediction_and_canonical_base64_are_allocation_free_contracts(self):
        self.assertEqual(measure_canonical_base64("aW1n"), 3)
        self.assertEqual(measure_canonical_base64("YQ=="), 1)
        with self.assertRaisesRegex(ImageBudgetError, "non-canonical"):
            measure_canonical_base64("YR==")
        with self.assertRaisesRegex(ImageBudgetError, "non-canonical"):
            measure_canonical_base64("aW1n\n")
        predicted = estimate_encoded_image_request_bytes("image/png", 3)
        self.assertGreater(predicted, 4)
        self.assertEqual(predicted - estimate_encoded_image_request_bytes("image/png", 0), 4)

    def test_aggregate_count_decoded_and_encoded_limits_fail_at_plus_one(self):
        one = image_block(b"a")
        base = {
            "imageInput": True,
            "imageMimeTypes": ["image/png"],
            "maxImageCount": 2,
            "maxImageBytes": 10,
            "maxAggregateImageBytes": 2,
            "maxEncodedImageRequestBytes": 10_000,
        }
        admitted = admit_image_messages(
            messages=[{"role": "user", "content": [one, one]}],
            capabilities=base,
            provider="openai",
        )
        self.assertEqual(admitted["summary"]["count"], 2)
        self.assertEqual(admitted["summary"]["decodedBytes"], 2)
        with self.assertRaises(ImageBudgetError) as count_error:
            admit_image_messages(
                messages=[{"role": "user", "content": [one, one, one]}],
                capabilities={**base, "maxAggregateImageBytes": 10},
                provider="openai",
            )
        self.assertEqual(count_error.exception.code, "image_count_limit")
        with self.assertRaises(ImageBudgetError) as decoded_error:
            admit_image_messages(
                messages=[{"role": "user", "content": [one, image_block(b"bb")]}],
                capabilities={**base, "maxImageCount": 3},
                provider="openai",
            )
        self.assertEqual(decoded_error.exception.code, "aggregate_decoded_image_limit")
        encoded_limit = estimate_encoded_image_request_bytes("image/png", 1)
        with self.assertRaises(ImageBudgetError) as encoded_error:
            admit_image_messages(
                messages=[{"role": "user", "content": [one, one]}],
                capabilities={
                    **base,
                    "maxAggregateImageBytes": 10,
                    "maxEncodedImageRequestBytes": encoded_limit * 2 - 1,
                },
                provider="openai",
            )
        self.assertEqual(encoded_error.exception.code, "aggregate_encoded_image_limit")

    def test_per_item_capability_drop_is_bounded_and_aggregate_errors_remain_hard(self):
        dropped = []
        result = admit_image_messages(
            messages=[{"role": "user", "content": [image_block(b"img")]}],
            capabilities={**DEFAULT_CAPABILITIES, "imageInput": False},
            provider="openai",
            onDrop=dropped.append,
        )
        self.assertEqual(result["summary"]["count"], 0)
        self.assertEqual(result["messages"][0]["content"][0]["type"], "text")
        self.assertIn("Image input omitted.", result["messages"][0]["content"][0]["text"])
        self.assertEqual(len(dropped), 1)

    def test_measurement_includes_dimensions_tokens_and_stable_fingerprint(self):
        png = bytearray(32)
        png[:8] = b"\x89PNG\r\n\x1a\n"
        png[16:20] = (1024).to_bytes(4, "big")
        png[20:24] = (512).to_bytes(4, "big")
        messages = [{"role": "user", "content": [image_block(bytes(png))]}]
        first = measure_admitted_image_set(messages, "openai")
        second = measure_admitted_image_set(messages, "openai")
        self.assertEqual(first, second)
        self.assertEqual(first["count"], 1)
        self.assertEqual(first["images"][0]["estimatedInputTokens"], 425)
        self.assertRegex(first["fingerprint"], r"^[a-f0-9]{64}$")

    def test_concurrent_reservations_follow_provider_emission_order(self):
        async def exercise():
            capabilities = {
                **DEFAULT_CAPABILITIES,
                "maxImageCount": 1,
                "maxAggregateImageBytes": 10,
                "maxEncodedImageRequestBytes": 10_000,
            }
            reservation = create_image_budget_reservation(capabilities, [0, 1])
            later = asyncio.create_task(
                reservation.reserve({"mimeType": "image/png", "decodedBytes": 1}, 1)
            )
            await asyncio.sleep(0)
            self.assertFalse(later.done())
            earlier = await reservation.reserve({"mimeType": "image/png", "decodedBytes": 1}, 0)
            self.assertTrue(earlier["accepted"])
            await reservation.complete(0)
            rejected = await later
            self.assertFalse(rejected["accepted"])
            self.assertIn("count limit", rejected["reason"])
            await reservation.complete(1)

        run(exercise())

    def test_runtime_capability_limits_are_hard_capped(self):
        capabilities = resolve_runtime_provider_capabilities(
            {
                "providerCapabilities": {
                    "imageInput": True,
                    "imageMimeTypes": ["IMAGE/PNG", "bad", "image/png"],
                    "maxImageCount": 10_000,
                    "maxImageBytes": 10**12,
                    "maxAggregateImageBytes": 10**12,
                    "maxEncodedImageRequestBytes": 10**12,
                }
            }
        )
        self.assertEqual(capabilities["imageMimeTypes"], ["image/png"])
        self.assertEqual(capabilities["maxImageCount"], 32)
        self.assertEqual(capabilities["maxImageBytes"], 20 * MIB)
        self.assertEqual(capabilities["maxAggregateImageBytes"], 40 * MIB)
        self.assertEqual(capabilities["maxEncodedImageRequestBytes"], 56 * MIB)

    def test_custom_compatible_profile_defaults_text_only_and_explicit_opt_in_resolves(self):
        text_only = core.resolve_provider_profile(
            "custom", {"kind": "openai", "baseUrl": "https://compatible.test/v1", "defaultModel": "GLM-5"}
        )
        self.assertFalse(text_only["capabilities"]["imageInput"])
        enabled = core.resolve_provider_profile(
            "custom-image",
            {
                "kind": "openai", "baseUrl": "https://compatible.test/v1", "defaultModel": "GLM-5",
                "capabilities": {"imageInput": True, "imageMimeTypes": ["image/png"], "maxImageBytes": 1234},
            },
        )
        self.assertTrue(enabled["capabilities"]["imageInput"])
        self.assertEqual(enabled["capabilities"]["imageMimeTypes"], ["image/png"])
        self.assertEqual(enabled["capabilities"]["maxImageBytes"], 1234)

    def test_image_token_measurement_and_context_rejection_include_bounded_evidence(self):
        config = image_config("/tmp")
        request = {
            "model": "gpt-5.4", "system": "test", "tools": [],
            "messages": [{"role": "user", "content": [image_block(b"img")]}],
        }
        measurement = run(core.measure_request_input_tokens(config=config, request=request, session={}))
        self.assertEqual(measurement["imageCount"], 1)
        self.assertEqual(measurement["imageDecodedBytes"], 3)
        self.assertEqual(measurement["imageInputTokens"], 255)
        self.assertRegex(measurement["imageSetFingerprint"], r"^[a-f0-9]{64}$")
        with self.assertRaises(ImageBudgetError) as caught:
            assert_image_context_capacity(
                measurement,
                {"insufficient": True, "remainingOutputTokens": 1, "minUsefulOutputTokens": 2},
            )
        self.assertEqual(caught.exception.code, "image_context_limit")
        self.assertEqual(caught.exception.measurement["imageCount"], 1)


class Package31MultimodalIntegrationTests(TessCoreTestCase):
    def _read_tool(self):
        return next(tool for tool in core.create_builtin_tools() if tool.name == "Read")

    def _read_context(self, config: dict, session: dict, events: list | None = None) -> dict:
        events = events if events is not None else []
        return {
            "cwd": config["cwd"],
            "allowedRoots": [config["cwd"]],
            "config": config,
            "session": session,
            "interactive": False,
            "outputJson": lambda event, data: events.append((event, data)),
            "askQuestion": lambda *_: "",
            "persist_session": lambda: None,
            "getTools": core.create_builtin_tools,
        }

    def test_read_image_and_pdf_return_metadata_without_inline_binary_content(self):
        with tempfile.TemporaryDirectory(prefix="tess-core-p31-image-") as cwd:
            config = image_config(cwd)
            session = create_session(config)
            image_path = Path(cwd, "screen.png")
            image_path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"a" * 4096)
            result = run(self._read_tool().handler(
                {"file_path": str(image_path)}, self._read_context(config, session)
            ))
            payload = json.loads(result["content"])
            self.assertTrue(payload["file"]["attached"])
            self.assertFalse(payload["file"]["rawBytesInlined"])
            self.assertEqual(result["attachments"][0]["byteLength"], image_path.stat().st_size)
            self.assertNotIn("base64", result["content"])
            self.assertLess(len(result["content"]), 1000)

            pdf_path = Path(cwd, "report.pdf")
            pdf_path.write_bytes(b"%PDF-1.7\n" + b"b" * 4096)
            pdf_result = run(self._read_tool().handler(
                {"file_path": str(pdf_path)}, self._read_context(config, session)
            ))
            pdf_payload = json.loads(pdf_result["content"])
            self.assertEqual(pdf_payload["type"], "pdf")
            self.assertFalse(pdf_payload["file"]["rawBytesInlined"])
            self.assertNotIn("attachments", pdf_result)
            self.assertNotIn("base64", pdf_result["content"])

    def test_text_only_read_does_not_acquire_or_encode_image_bytes(self):
        with tempfile.TemporaryDirectory(prefix="tess-core-p31-no-image-") as cwd:
            config = image_config(cwd, enabled=False)
            session = create_session(config)
            path = Path(cwd, "screen.png")
            path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"a" * 4096)
            events = []
            with mock.patch(
                "tess_core.tools.files._read_regular_file_bounded",
                side_effect=AssertionError("image acquisition must not run"),
            ):
                result = run(self._read_tool().handler(
                    {"file_path": str(path)}, self._read_context(config, session, events)
                ))
            payload = json.loads(result["content"])
            self.assertFalse(payload["file"]["attached"])
            self.assertIn("does not support image input", payload["file"]["reason"])
            self.assertNotIn("attachments", result)
            self.assertTrue(any(event == "tool.attachment_dropped" for event, _ in events))

    def test_finalizer_emits_image_after_tool_result_and_drops_invalid_metadata(self):
        with tempfile.TemporaryDirectory(prefix="tess-core-p31-finalizer-") as cwd:
            config = image_config(cwd)
            attachment = {
                "type": "image",
                "filePath": str(Path(cwd, "screen.png")),
                "mimeType": "image/png",
                "byteLength": 3,
                "sha256": "a" * 64,
                "data": base64.b64encode(b"img").decode("ascii"),
            }
            events = []
            completed = run(finalize_tool_dispatch_result(
                config=config,
                sessionId="session-1",
                assistantMessageIndex=0,
                toolUse={"id": "read-1", "name": "Read", "input": {}},
                outcome={"kind": "returned", "result": {"content": "metadata", "attachments": [attachment]}},
                emit=lambda event, data: events.append((event, data)),
            ))
            self.assertEqual([block["type"] for block in completed["blocks"]], ["tool_result", "image"])
            self.assertTrue(any(event == "tool.attachment_included" for event, _ in events))

    def test_agent_sends_image_once_and_never_persists_raw_bytes(self):
        root, home, workspace, previous = self.temp_context()
        try:
            config = image_config(str(workspace), persistence=True)
            config["runtimeHome"] = str(home)
            session = create_session(config)
            path = workspace / "screen.png"
            path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"a" * 4096)
            encoded = base64.b64encode(path.read_bytes()).decode("ascii")
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
                        "content": [{
                            "type": "tool_use", "id": "read-image", "name": "Read",
                            "input": {"file_path": str(path)},
                        }],
                    }
                captured["request"] = json.loads(json.dumps(request))
                return {
                    "model": "gpt-5.4",
                    "stopReason": "end_turn",
                    "usage": {"input_tokens": 1, "output_tokens": 1},
                    "content": [{"type": "text", "text": "image inspected"}],
                }

            runtime = bind_test_runtime(
                session,
                {
                    "interactive": False,
                    "askQuestion": lambda *_: "",
                    "emitEvent": lambda event, data: events.append((event, data)),
                    "tools": core.create_builtin_tools(),
                },
            )
            result = run(core.run_agent_prompt(
                session=session,
                config=config,
                runtime=runtime,
                prompt="inspect the screenshot",
                providerOverride={"send": send},
            ))
            self.assertEqual(result["stopReason"], "end_turn")
            self.assertEqual(calls, 2)
            request_text = json.dumps(captured["request"])
            self.assertIn('"type": "image"', request_text)
            self.assertIn(encoded, request_text)
            self.assertTrue(any(event == "tool.attachment_included" for event, _ in events))
            live = json.dumps(session)
            self.assertNotIn(encoded, live)
            self.assertIn("Image attachment was available to the model on the original turn.", live)
            persisted = (
                Path(home, "sessions", f"{session['id']}.json").read_text(encoding="utf-8")
                + Path(core.session_messages_path(session["id"])).read_text(encoding="utf-8")
            )
            self.assertNotIn(encoded, persisted)
            self.assertNotIn("data:image", persisted)
            self.assertIn("Image attachment was available to the model on the original turn.", persisted)
        finally:
            self.restore_home(root, previous)

    def test_provider_boundary_drops_preexisting_image_for_disabled_profile(self):
        with tempfile.TemporaryDirectory(prefix="tess-core-p31-boundary-") as cwd:
            config = image_config(cwd, enabled=False)
            session = create_session(config)
            session["messages"].append({"role": "user", "content": [image_block(b"img")]})
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

            run(core.run_agent_prompt(
                session=session,
                config=config,
                runtime=bind_test_runtime(session, {
                    "interactive": False,
                    "askQuestion": lambda *_: "",
                    "emitEvent": lambda event, data: events.append((event, data)),
                    "tools": [],
                }),
                prompt="continue",
                providerOverride={"send": send},
            ))
            request_text = json.dumps(captured["request"])
            self.assertNotIn('"type": "image"', request_text)
            self.assertIn("Image input omitted.", request_text)
            self.assertTrue(any(event == "tool.attachment_dropped" for event, _ in events))

    def test_strip_in_memory_data_replaces_every_image_shape(self):
        messages = [{"role": "user", "content": [image_block(b"img")]}]
        stripped = strip_in_memory_image_data(messages)
        self.assertEqual(stripped[0]["content"][0]["type"], "text")
        self.assertIn("Payload bytes: 3", stripped[0]["content"][0]["text"])
        self.assertEqual(messages[0]["content"][0]["type"], "image")

    def test_provider_filter_rejects_aggregate_before_serialization(self):
        config = image_config("/tmp")
        config["providerCapabilities"] = {
            **DEFAULT_CAPABILITIES,
            "maxImageCount": 1,
            "maxAggregateImageBytes": 10,
            "maxEncodedImageRequestBytes": 10_000,
        }
        with self.assertRaises(ImageBudgetError) as caught:
            filter_images_for_provider_capabilities(
                messages=[{"role": "user", "content": [image_block(b"a"), image_block(b"b")]}],
                config=config,
            )
        self.assertEqual(caught.exception.code, "image_count_limit")

    def test_agent_aggregate_rejection_restores_sanitized_stable_state_and_emits(self):
        with tempfile.TemporaryDirectory(prefix="tess-core-p31-reject-") as cwd:
            config = image_config(cwd)
            config["providerCapabilities"] = {**DEFAULT_CAPABILITIES, "maxImageCount": 1}
            session = create_session(config)
            prompt = [{"type": "text", "text": "inspect"}, image_block(b"first"), image_block(b"second")]
            events = []

            async def send(_request):
                raise AssertionError("aggregate rejection must occur before provider dispatch")

            with self.assertRaises(ImageBudgetError):
                run(core.run_agent_prompt(
                    session=session,
                    config=config,
                    runtime=bind_test_runtime(session, {
                        "interactive": False, "askQuestion": lambda *_: "", "tools": [],
                        "emitEvent": lambda event, data: events.append((event, data)),
                    }),
                    prompt=prompt,
                    providerOverride={"send": send},
                ))
            self.assertTrue(any(event == "provider.image_budget_rejected" for event, _ in events))
            self.assertEqual(session["messages"], [])


if __name__ == "__main__":
    unittest.main()
