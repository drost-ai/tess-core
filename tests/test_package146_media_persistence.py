from __future__ import annotations

import base64
import copy
import io
import json
import tempfile
import unittest
from pathlib import Path

import tess_core._facade as core
from tess_core.headless.stream import JsonStreamSink
from tess_core.payloads.media_persistence import sanitize_media_for_persistence
from tests.support.tess_core_case import bind_test_runtime, run


_SENTINEL_BYTES = b"PACKAGE_146_PRIVATE_IMAGE_SENTINEL" * 32_768
_SENTINEL = base64.b64encode(_SENTINEL_BYTES).decode("ascii")
_DATA_URL = f"data:image/png;base64,{_SENTINEL}"


def _image_block() -> dict:
    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": "image/png",
            "data": _SENTINEL,
        },
        "path": "inbox/current.png",
    }


def _config(root: Path) -> dict:
    capabilities = {
        "imageInput": True,
        "imageMimeTypes": ["image/png"],
        "maxImageCount": 8,
        "maxImageBytes": 5 * 1024 * 1024,
        "maxAggregateImageBytes": 20 * 1024 * 1024,
        "maxEncodedImageRequestBytes": 24 * 1024 * 1024,
    }
    return {
        "cwd": str(root),
        "runtimeHome": str(root),
        "allowedRoots": [str(root)],
        "agents": {},
        "provider": "openai",
        "providerKind": "openai",
        "providerProfileName": "p146",
        "providerProfile": {
            "kind": "openai",
            "name": "p146",
            "apiMode": "responses",
            "capabilities": capabilities,
        },
        "providerCapabilities": capabilities,
        "providerProfiles": {
            "p146": {
                "kind": "openai",
                "name": "p146",
                "capabilities": capabilities,
            }
        },
        "customHeaders": {},
        "openaiApiMode": "responses",
        "model": "gpt-5.4",
        "effort": "medium",
        "assistantName": "Test",
        "assistantDescription": "",
        "systemPrompt": "test",
        "tools": [],
        "allowedTools": [],
        "disallowedTools": [],
        "permissionMode": "bypassPermissions",
        "sessionPersistence": True,
    }


def _session(root: Path, session_id: str) -> dict:
    return core.create_empty_session(
        cwd=str(root),
        provider="openai",
        providerProfileName="p146",
        model="gpt-5.4",
        effort="medium",
        systemPrompt="test",
        permissionMode="bypassPermissions",
        sessionId=session_id,
    )


def _assert_runtime_clean(test: unittest.TestCase, root: Path) -> None:
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        raw = path.read_bytes()
        test.assertNotIn(_SENTINEL.encode("ascii"), raw, str(path))
        test.assertNotIn(b"data:image", raw.lower(), str(path))


class Package146MediaSanitizerTests(unittest.TestCase):
    def test_allowlisted_projection_covers_nested_and_provider_shapes(self) -> None:
        long_non_media_data = base64.b64encode(b"ordinary-record-data" * 256).decode(
            "ascii"
        )
        values = [
            {"type": "image", "mime_type": "image/png", "data": _SENTINEL},
            _image_block(),
            {"type": "input_image", "image_url": _DATA_URL},
            {"type": "image_url", "image_url": {"url": _DATA_URL, "detail": "high"}},
            {
                "type": "tool_result",
                "attachments": [
                    {
                        "type": "image",
                        "mimeType": "image/png",
                        "byteLength": len(_SENTINEL_BYTES),
                        "data": _SENTINEL,
                    }
                ],
            },
            {
                "type": "image",
                "source": {
                    "type": "future_binary",
                    "unknown": {"payload": _SENTINEL},
                },
            },
            {"message": f"provider echoed {_DATA_URL}"},
            {"type": "record", "data": long_non_media_data},
        ]
        original = copy.deepcopy(values)

        sanitized = sanitize_media_for_persistence(values)
        serialized = json.dumps(sanitized)

        self.assertEqual(values, original)
        self.assertNotIn(_SENTINEL, serialized)
        self.assertNotIn("data:image", serialized.lower())
        self.assertGreaterEqual(serialized.count("current-turn-only"), 6)
        self.assertEqual(sanitized[-1]["data"], long_non_media_data)
        self.assertEqual(
            sanitize_media_for_persistence({"type": "record", "data": "ordinary"}),
            {"type": "record", "data": "ordinary"},
        )

    def test_session_transcript_checkpoint_and_headless_event_are_clean(self) -> None:
        with tempfile.TemporaryDirectory(prefix="tess-core-p146-artifacts-") as raw:
            root = Path(raw)
            session = _session(root, "p146-artifacts")
            session["messages"] = [
                {"role": "user", "content": [{"type": "text", "text": "inspect"}, _image_block()]}
            ]

            async def persist() -> None:
                async with core.scoped_runtime_home(str(root)):
                    await core.ensure_base_layout(str(root))
                    await core.save_session(session)
                    await core.append_session_transcript_log_entries(
                        session["id"],
                        [
                            {
                                "type": "message",
                                "role": "user",
                                "content": session["messages"][0]["content"],
                            }
                        ],
                    )
                    await core.save_compaction_manifest(
                        {
                            "sessionId": session["id"],
                            "latestGeneration": 1,
                            "trace": _image_block(),
                        }
                    )
                    await core.save_compaction_checkpoint(
                        {
                            "sessionId": session["id"],
                            "generation": 1,
                            "trace": _image_block(),
                        }
                    )

            run(persist())
            stream = io.StringIO()
            JsonStreamSink(stream).emit(
                "provider.failed",
                {"request": _image_block(), "raw_error": _DATA_URL},
            )
            output = stream.getvalue()
            self.assertNotIn(_SENTINEL, output)
            self.assertNotIn("data:image", output.lower())
            _assert_runtime_clean(self, root)

    def test_loaded_legacy_session_is_clean_in_memory_before_normal_persist(self) -> None:
        with tempfile.TemporaryDirectory(prefix="tess-core-p146-legacy-") as raw:
            root = Path(raw)
            config = _config(root)
            legacy = _session(root, "p146-legacy-loaded")
            legacy["messages"] = [
                {"role": "user", "content": [{"type": "text", "text": "old"}, _image_block()]}
            ]

            async def execute() -> None:
                async with core.scoped_runtime_home(str(root)):
                    await core.ensure_base_layout(str(root))
                    path = Path(core.session_path(legacy["id"]))
                    path.write_text(json.dumps(legacy), encoding="utf-8")

                    loaded = await core.load_session(legacy["id"])
                    self.assertIsNotNone(loaded)
                    assert loaded is not None
                    self.assertNotIn(_SENTINEL, json.dumps(loaded))
                    self.assertIn(_SENTINEL, path.read_text(encoding="utf-8"))

                    loaded["messages"].append(
                        {"role": "user", "content": "current turn"}
                    )
                    await core.persist_session(loaded, config, touch=False)
                    reloaded = await core.load_session(legacy["id"])
                    self.assertNotIn(_SENTINEL, json.dumps(reloaded))
                    self.assertNotIn(_SENTINEL, json.dumps(loaded))
                    self.assertNotIn(_SENTINEL, path.read_text(encoding="utf-8"))

            run(execute())

    def test_archive_recovery_returns_the_sanitized_record_it_writes(self) -> None:
        with tempfile.TemporaryDirectory(prefix="tess-core-p146-recovery-") as raw:
            root = Path(raw)
            recovered = _session(root, "p146-legacy-recovery")
            recovered["messages"] = []

            async def execute() -> None:
                async with core.scoped_runtime_home(str(root)):
                    await core.ensure_base_layout(str(root))
                    archive = Path(core.session_transcript_log_path(recovered["id"]))
                    archive.parent.mkdir(parents=True, exist_ok=True)
                    archive.write_text(
                        "\n".join(
                            [
                                json.dumps(
                                    {
                                        "type": "session_header",
                                        "schema_version": 1,
                                        "session_id": recovered["id"],
                                        "session": recovered,
                                    }
                                ),
                                json.dumps(
                                    {
                                        "type": "message",
                                        "message_index": 0,
                                        "persisted_at": recovered["updatedAt"],
                                        "role": "user",
                                        "content": [_image_block()],
                                    }
                                ),
                            ]
                        )
                        + "\n",
                        encoding="utf-8",
                    )

                    result = await core.load_session_with_reconciliation(recovered["id"])
                    self.assertTrue(result["changed"])
                    self.assertEqual(result["reason"], "archive_recovery")
                    self.assertNotIn(_SENTINEL, json.dumps(result["record"]))
                    session_raw = (
                        Path(core.session_path(recovered["id"])).read_text(encoding="utf-8")
                        + Path(core.session_messages_path(recovered["id"])).read_text(encoding="utf-8")
                    )
                    self.assertNotIn(_SENTINEL, session_raw)
                    self.assertIn(_SENTINEL, archive.read_text(encoding="utf-8"))

            run(execute())


class Package146TransientOverlayTests(unittest.TestCase):
    def test_provider_receives_exact_bytes_while_canonical_state_stays_clean(self) -> None:
        with tempfile.TemporaryDirectory(prefix="tess-core-p146-overlay-") as raw:
            root = Path(raw)
            config = _config(root)
            session = _session(root, "p146-overlay-success")
            provider_message = {
                "role": "user",
                "content": [
                    {"type": "text", "text": "before"},
                    _image_block(),
                    {"type": "text", "text": "after"},
                ],
            }
            canonical_message = sanitize_media_for_persistence(provider_message)
            canonical_message["metadata"] = {"host": {"turn_id": "turn-146"}}
            session["messages"] = [canonical_message]
            captured: list[dict] = []

            async def send(request: dict) -> dict:
                captured.append(copy.deepcopy(request))
                self.assertEqual(
                    request["messages"][0]["content"][1]["source"]["data"],
                    _SENTINEL,
                )
                _assert_runtime_clean(self, root)
                return {
                    "model": "gpt-5.4",
                    "stopReason": "end_turn",
                    "usage": {"input_tokens": 1, "output_tokens": 1},
                    "content": [{"type": "text", "text": "done"}],
                }

            async def execute() -> None:
                async with core.scoped_runtime_home(str(root)):
                    await core.ensure_base_layout(str(root))
                    await core.persist_session(session, config, touch=False)
                    await core.run_agent_prompt(
                        session=session,
                        config=config,
                        runtime=bind_test_runtime(session),
                        prompt="",
                        transientInputOverlay={
                            "turnId": "turn-146",
                            "message": provider_message,
                        },
                        providerOverride={"send": send},
                    )

            run(execute())
            self.assertEqual(len(captured), 1)
            self.assertEqual(
                provider_message["content"][1]["source"]["data"],
                _SENTINEL,
            )
            self.assertNotIn(_SENTINEL, json.dumps(session))
            self.assertIn("current-turn-only", json.dumps(session))
            _assert_runtime_clean(self, root)

    def test_fail_before_first_event_sanitizes_failure_and_releases_overlay(self) -> None:
        with tempfile.TemporaryDirectory(prefix="tess-core-p146-failure-") as raw:
            root = Path(raw)
            config = _config(root)
            session = _session(root, "p146-overlay-failure")
            provider_message = {
                "role": "user",
                "content": [{"type": "text", "text": "inspect"}, _image_block()],
            }
            canonical_message = sanitize_media_for_persistence(provider_message)
            canonical_message["metadata"] = {"host": {"turn_id": "turn-failure"}}
            session["messages"] = [canonical_message]
            events: list[tuple[str, dict]] = []

            async def send(_request: dict) -> dict:
                _assert_runtime_clean(self, root)
                raise core.create_provider_request_error(
                    core.create_provider_failure_record(
                        provider="openai",
                        providerLabel="p146",
                        phase="request",
                        category="client_request",
                        retryable=False,
                        attempt=1,
                        hadAssistantOutput=False,
                        hadToolUse=False,
                        rawMessage=f"echoed {_DATA_URL}",
                        rawBody=json.dumps(_image_block()),
                    )
                )

            async def execute() -> None:
                async with core.scoped_runtime_home(str(root)):
                    await core.ensure_base_layout(str(root))
                    await core.persist_session(session, config, touch=False)
                    await core.run_agent_prompt(
                        session=session,
                        config=config,
                        runtime=bind_test_runtime(
                            session,
                            {"emitEvent": lambda event, data: events.append((event, data))},
                        ),
                        prompt="",
                        transientInputOverlay={
                            "turnId": "turn-failure",
                            "message": provider_message,
                        },
                        providerOverride={"send": send},
                    )

            with self.assertRaises(core.ProviderRequestError):
                run(execute())
            self.assertNotIn(_SENTINEL, json.dumps(session))
            self.assertNotIn(_SENTINEL, json.dumps(events))
            self.assertNotIn("data:image", json.dumps(events).lower())
            _assert_runtime_clean(self, root)


if __name__ == "__main__":
    unittest.main()
