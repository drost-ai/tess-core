"""A session on a google profile, through the real agent loop, against a scripted Gemini server.

The server speaks the Interactions API over a real socket, so what is asserted here is
what would be sent to Google: the path, the header that carries the key, and the steps.
"""
from __future__ import annotations

import base64
import struct
import zlib

from tests.support.tess_core_case import *

from tess_core import ProviderRegistry, SessionOptions, start_runtime
from tess_core.agent.providers import MISSING_CREDENTIAL_PREFIX, _create_provider
from tess_core.config import resolve_config
from tess_core.config._knobs import reset_configured_knobs
from tess_core.context.compaction_transport import create_default_compaction_model_client, resolve_compaction_execution_plan
from tess_core.runtime import host as host_module
from tess_core.tasks.collaboration_narration import author_collaboration_update_with_provider
from tess_core.tasks.monitor_narration import _author_monitor_update_with_provider
from tess_core_harness.fake_providers.google import googleInteractionEvents, googleInteractionsSseBody
from tess_core_harness.fake_providers.server import FakeProviderHttpServer

KEY_VARIABLE = "TESS_CORE_GOOGLE_LOOP_TEST_KEY"
IDLE_KNOB = "TESS_CORE_SESSIONS_IDLE_CLOSE_MS"
USAGE = {"total_input_tokens": 5000, "total_cached_tokens": 4200, "total_output_tokens": 20, "total_thought_tokens": 30, "total_tokens": 5050}


def _png(width: int = 2, height: int = 2) -> bytes:
    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    rows = b"".join(b"\x00" + b"\xff\x00\x00" * width for _ in range(height))
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b"")


def _sse(steps, **kwargs):
    return {"contentType": "text/event-stream", "rawBody": googleInteractionsSseBody(googleInteractionEvents(steps, **kwargs))}


def _steps(request):
    return [step["type"] for step in request["body"]["input"]]


class GoogleAgentLoopTests(TessCoreTestCase):
    def setUp(self):
        host_module._ACTIVE = None
        self.root, self.home, self.workspace, self.previous = self.temp_context()
        os.environ[KEY_VARIABLE] = "loop-google-key"

    def tearDown(self):
        os.environ.pop(KEY_VARIABLE, None)
        os.environ.pop(IDLE_KNOB, None)
        reset_configured_knobs()
        self.restore_home(self.root, self.previous)

    def _server(self, responses):
        requests: list[dict] = []
        server = FakeProviderHttpServer(provider="google", script={"interactions": responses}, request_recorder=requests, record_all_headers=True).start()
        self.addCleanup(server.close)
        return server, requests

    def _registry(self, server, **profile):
        record = {"kind": "google", "baseUrl": server.base_url, "defaultModel": "gemini-4-hydrogen", "reasoningEffort": "xhigh", "auth": {"kind": "google_api_key", "envVar": KEY_VARIABLE}}
        record.update(profile)
        return ProviderRegistry.from_document({"schema_version": 1, "providerProfiles": {"gemini": record}})

    async def _start(self, registry, **kwargs):
        return await start_runtime(
            str(self.home), cwd=str(self.workspace), daemon_enabled=False, startup_timeout_seconds=20, provider_registry=registry,
            session_options=lambda *_: SessionOptions(provider="gemini", permission_mode="bypassPermissions", extra={"settingSources": ""}), **kwargs,
        )

    def test_a_tool_loop_sends_back_every_step_and_the_next_turn_extends_it(self):
        (Path(self.workspace) / "notes.txt").write_text("the word is pelican\n")
        path = str(Path(self.workspace) / "notes.txt")
        server, requests = self._server([
            _sse([
                {"type": "thought", "signature": "sig-plan", "summary": "I should read the file."},
                {"type": "text", "text": "Reading it."},
                {"type": "thought", "signature": "sig-call"},
                {"type": "function_call", "id": "call_read", "name": "Read", "arguments": {"file_path": path}},
            ], usage=USAGE),
            _sse([{"type": "thought", "signature": "sig-answer"}, {"type": "text", "text": "The word is pelican."}], usage=USAGE),
            _sse([{"type": "text", "text": "Seven letters."}], usage=USAGE),
        ])

        async def scenario():
            runtime = await self._start(self._registry(server))
            try:
                handle = await runtime.sessions.open()
                first = await handle.run_turn("What word is in notes.txt?")
                second = await handle.run_turn("How many letters?")
                return first, second, await core.load_session(handle.id)
            finally:
                await runtime.stop()

        first, second, stored = run(scenario())
        self.assertEqual((first["text"], first["stopReason"], first["toolCount"]), ("Reading it.\n\nThe word is pelican.", "end_turn", 1))
        self.assertEqual(second["text"], "Seven letters.")
        self.assertEqual(len(requests), 3)

        opening = requests[0]
        self.assertEqual(opening["path"], "/v1/interactions")
        self.assertEqual(opening["headers"]["x-goog-api-key"], "loop-google-key")
        self.assertNotIn("authorization", opening["headers"])
        body = opening["body"]
        self.assertIs(body["store"], False)
        self.assertNotIn("previous_interaction_id", body)
        self.assertEqual(body["model"], "gemini-4-hydrogen")
        self.assertEqual(body["generation_config"]["thinking_level"], "xhigh")
        self.assertTrue(body["system_instruction"])
        self.assertIn("Read", [tool["name"] for tool in body["tools"]])
        self.assertTrue(all(tool["type"] == "function" and tool["parameters"]["type"] == "object" for tool in body["tools"]))
        self.assertEqual(body["input"], [{"type": "user_input", "content": [{"type": "text", "text": "What word is in notes.txt?"}]}])

        follow_up = requests[1]["body"]["input"]
        self.assertEqual(_steps(requests[1]), ["user_input", "thought", "model_output", "thought", "function_call", "function_result"])
        self.assertEqual(follow_up[1], {"type": "thought", "signature": "sig-plan", "summary": [{"type": "text", "text": "I should read the file."}]})
        self.assertEqual(follow_up[3], {"type": "thought", "signature": "sig-call"})
        self.assertEqual(follow_up[4], {"type": "function_call", "id": "call_read", "name": "Read", "arguments": {"file_path": path}})
        self.assertEqual((follow_up[5]["name"], follow_up[5]["call_id"]), ("Read", "call_read"))
        self.assertIn("the word is pelican", follow_up[5]["result"][0]["text"])

        # Each call's steps begin with the previous call's: the prefix Gemini's cache reads.
        for earlier, later in ((requests[0], requests[1]), (requests[1], requests[2])):
            sent, extended = earlier["body"]["input"], later["body"]["input"]
            self.assertEqual(extended[: len(sent)], sent)
            for key in ("model", "system_instruction", "tools", "generation_config"):
                self.assertEqual(later["body"][key], earlier["body"][key])
        self.assertEqual(_steps(requests[2])[6:], ["thought", "model_output", "user_input"])

        self.assertEqual(stored["provider"], "google")
        assistant = [message for message in stored["messages"] if message["role"] == "assistant"]
        self.assertEqual(assistant[0]["content"][0]["provider_metadata"]["google_thoughts"][0]["signature"], "sig-plan")
        usage = stored["metadata"]["usage"]
        self.assertEqual((usage["inputTokens"], usage["outputTokens"], usage["cacheReadInputTokens"]), (2400, 150, 12600))
        self.assertNotIn("loop-google-key", json.dumps(stored))

    def test_parallel_calls_and_an_image_a_tool_returned(self):
        (Path(self.workspace) / "a.txt").write_text("alpha\n")
        (Path(self.workspace) / "shot.png").write_bytes(_png())
        text_path, image_path = str(Path(self.workspace) / "a.txt"), str(Path(self.workspace) / "shot.png")
        server, requests = self._server([
            _sse([
                {"type": "function_call", "id": "call_text", "name": "Read", "arguments": {"file_path": text_path}},
                {"type": "function_call", "id": "call_image", "name": "Read", "arguments": {"file_path": image_path}},
            ]),
            _sse([{"type": "text", "text": "A red square and the word alpha."}]),
        ])

        async def scenario():
            runtime = await self._start(self._registry(server))
            try:
                handle = await runtime.sessions.open()
                return await handle.run_turn("Describe both files.")
            finally:
                await runtime.stop()

        result = run(scenario())
        self.assertEqual((result["text"], result["toolCount"]), ("A red square and the word alpha.", 2))
        steps = requests[1]["body"]["input"]
        self.assertEqual(_steps(requests[1]), ["user_input", "function_call", "function_call", "function_result", "function_result"])
        self.assertEqual([step["call_id"] for step in steps[3:]], ["call_text", "call_image"])
        self.assertIn("alpha", steps[3]["result"][0]["text"])
        images = [item for item in steps[4]["result"] if item["type"] == "image"]
        self.assertEqual(len(images), 1)
        self.assertEqual(images[0]["mime_type"], "image/png")
        self.assertEqual(base64.b64decode(images[0]["data"]), _png())

    def test_a_session_reopened_after_an_idle_close_still_has_its_thoughts(self):
        os.environ[IDLE_KNOB] = "300"
        server, requests = self._server([
            _sse([{"type": "thought", "signature": "sig-kept", "summary": "Noting it."}, {"type": "text", "text": "Noted: pelican."}]),
            _sse([{"type": "text", "text": "pelican"}]),
        ])

        async def scenario():
            closed: list = []

            class Listener:
                def on_event(self, *_): pass
                def on_session_opened(self, *_): pass
                def on_session_closed(self, session_id, reason): closed.append(reason)
                def on_runtime_event(self, *_): pass

            runtime = await self._start(self._registry(server), listener=Listener())
            try:
                handle = await runtime.sessions.open()
                await handle.run_turn("Remember the word pelican.")
                del os.environ[KEY_VARIABLE]          # the host holds the key only while it opens the session
                for _ in range(400):
                    if closed:
                        break
                    await asyncio.sleep(0.05)
                self.assertEqual(closed, ["idle"])
                self.assertFalse(handle.is_open)
                return await handle.run_turn("Which word?")
            finally:
                await runtime.stop()

        result = run(scenario())
        self.assertEqual(result["text"], "pelican")
        self.assertEqual(requests[1]["headers"]["x-goog-api-key"], "loop-google-key")
        self.assertEqual(_steps(requests[1]), ["user_input", "thought", "model_output", "user_input"])
        self.assertEqual(requests[1]["body"]["input"][1]["signature"], "sig-kept")

    def test_a_sub_agent_of_a_google_session_is_restored_on_the_same_profile(self):
        from tess_core.subagents import prepare_subagent
        from tess_core.tasks.runners.subagent import _session_config_options as child_options

        server, _ = self._server([_sse([{"type": "text", "text": "unused"}])])
        registry_path = Path(self.home) / "providers.json"
        registry_path.write_text(json.dumps({"schema_version": 1, "providerProfiles": self._registry(server).profiles}))

        async def scenario():
            runtime = await self._start(registry_path)
            try:
                handle = await runtime.sessions.open()
                live = runtime.sessions.live(handle.id)
                context = {"session": live.record, "config": live.config, "interactive": False, "cwd": str(self.workspace), "persist_session": lambda: None}
                child = (await prepare_subagent({"context": context, "description": "child", "prompt": "test", "runInBackground": True}))["childSession"]
                # The worker process restores the child from its own record.
                options = child_options(child, str(self.home))
                restored = await resolve_config(options, str(self.workspace))
                return child, options, restored, await core.resolve_provider_config_secrets(restored)
            finally:
                await runtime.stop()

        child, options, restored, resolved = run(scenario())
        self.assertEqual((child["provider"], child["model"]), ("google", "gemini-4-hydrogen"))
        self.assertEqual((options["provider"], options["providerRegistry"]), ("gemini", str(registry_path)))
        self.assertEqual((restored["provider"], restored["providerProfileName"], restored["googleBaseUrl"]), ("google", "gemini", server.base_url))
        self.assertEqual(resolved["googleApiKey"], "loop-google-key")
        self.assertIsNone(resolved["apiKey"])
        self.assertIsNone(resolved["openaiToken"])

    def test_a_truncated_response_ends_the_turn_as_max_tokens(self):
        server, _ = self._server([_sse([{"type": "text", "text": "Half an ans"}], status="incomplete")])

        async def scenario():
            runtime = await self._start(self._registry(server))
            try:
                return await (await runtime.sessions.open()).run_turn("Go.")
            finally:
                await runtime.stop()

        result = run(scenario())
        self.assertEqual((result["text"], result["stopReason"]), ("Half an ans", "max_tokens"))

    def test_a_profile_without_its_key_answers_that_it_has_none(self):
        server, requests = self._server([_sse([{"type": "text", "text": "unreachable"}])])

        async def scenario():
            config = await resolve_config({"runtimeHome": str(self.home), "providerRegistry": self._registry(server, auth={"kind": "none"}), "provider": "gemini", "settingSources": ""}, str(self.workspace))
            keyed = {**config, "providerProfile": {**config["providerProfile"], "auth": {"kind": "google_api_key", "envVar": KEY_VARIABLE}}}
            answer = _create_provider(keyed, {})["send"]({"model": "gemini-4-hydrogen"})
            return answer, await _create_provider(config, {})["send"]({"model": "gemini-4-hydrogen", "messages": [{"role": "user", "content": "hi"}], "tools": []})

        missing, unauthenticated = run(scenario())
        self.assertTrue(missing["content"][0]["text"].startswith(MISSING_CREDENTIAL_PREFIX))
        self.assertIn(KEY_VARIABLE, missing["content"][0]["text"])
        self.assertEqual(unauthenticated["content"], [{"type": "text", "text": "unreachable"}])
        self.assertNotIn("x-goog-api-key", requests[0]["headers"])
        with self.assertRaises(ValueError):
            _create_provider({"provider": "vertex"}, {})


class GoogleCompactionAndNarrationTests(TessCoreTestCase):
    def setUp(self):
        host_module._ACTIVE = None
        self.root, self.home, self.workspace, self.previous = self.temp_context()
        os.environ[KEY_VARIABLE] = "loop-google-key"
        self.requests: list[dict] = []
        self.server = FakeProviderHttpServer(provider="google", script={"interactions": [_sse([{"type": "thought", "signature": "s"}, {"type": "text", "text": "A checkpoint."}], usage=USAGE)]}, request_recorder=self.requests, record_all_headers=True).start()
        self.addCleanup(self.server.close)

    def tearDown(self):
        os.environ.pop(KEY_VARIABLE, None)
        reset_configured_knobs()
        self.restore_home(self.root, self.previous)

    def _registry(self):
        profile = lambda model, variable: {"kind": "google", "baseUrl": self.server.base_url, "defaultModel": model, "auth": {"kind": "google_api_key", "envVar": variable}}
        return ProviderRegistry.from_document({"schema_version": 1, "providerProfiles": {
            "gemini": profile("gemini-4-hydrogen", KEY_VARIABLE),
            "flash": profile("gemini-3.8-flash", "TESS_CORE_GOOGLE_LOOP_FLASH_KEY"),
            "console": {"kind": "anthropic", "defaultModel": "claude-test", "auth": {"kind": "anthropic_api_key", "envVar": "TESS_CORE_GOOGLE_LOOP_ANTHROPIC_KEY"}},
        }})

    async def _config(self, **options):
        return await resolve_config({"runtimeHome": str(self.home), "providerRegistry": self._registry(), "provider": "gemini", "settingSources": "", **options}, str(self.workspace))

    def test_a_google_session_compacts_on_its_own_profile(self):
        async def scenario():
            config = await self._config()
            plan = resolve_compaction_execution_plan({"config": config, "session": {"provider": "google", "model": "gemini-4-hydrogen", "effort": "xhigh"}})
            self.assertEqual((plan["synthesis"]["provider"], plan["synthesis"]["model"], plan["synthesis"]["effort"], plan["synthesis"]["providerProfileName"]), ("google", "gemini-4-hydrogen", "xhigh", "gemini"))
            client = create_default_compaction_model_client()
            return await client.complete({"config": config, "model": plan["synthesis"], "system": "Summarise.", "prompt": "The conversation so far."})

        result = run(scenario())
        self.assertEqual(result["text"], "A checkpoint.")
        self.assertEqual(result["usage"]["cache_read_input_tokens"], 4200)
        record = self.requests[0]
        self.assertEqual((record["path"], record["headers"]["x-goog-api-key"]), ("/v1/interactions", "loop-google-key"))
        self.assertEqual(record["body"]["system_instruction"], "Summarise.")
        self.assertNotIn("tools", record["body"])
        self.assertIs(record["body"]["store"], False)
        self.assertEqual(record["body"]["input"], [{"type": "user_input", "content": [{"type": "text", "text": "The conversation so far."}]}])

    def test_compaction_bound_to_another_google_profile_uses_that_profiles_key(self):
        async def scenario():
            config = await self._config(compactionProviderProfile="flash")
            plan = resolve_compaction_execution_plan({"config": config, "session": {"provider": "google", "model": "gemini-4-hydrogen"}})
            self.assertEqual((plan["synthesis"]["model"], plan["synthesis"]["providerProfileName"]), ("gemini-3.8-flash", "flash"))
            return await create_default_compaction_model_client().complete({"config": config, "model": plan["synthesis"], "system": "s", "prompt": "p"})

        with mock.patch.dict(os.environ, {"TESS_CORE_GOOGLE_LOOP_FLASH_KEY": "flash-google-key"}):
            run(scenario())
        self.assertEqual(self.requests[0]["headers"]["x-goog-api-key"], "flash-google-key")
        self.assertEqual(self.requests[0]["body"]["model"], "gemini-3.8-flash")

    def test_an_anthropic_session_cannot_compact_on_google_without_a_google_profile_bound(self):
        async def scenario():
            with mock.patch.dict(os.environ, {"TESS_CORE_GOOGLE_LOOP_ANTHROPIC_KEY": "fake-anthropic"}):
                config = await self._config(provider="console")
            await create_default_compaction_model_client().complete({"config": config, "model": {"provider": "google", "model": "gemini-4-hydrogen", "purpose": "synthesis"}, "system": "s", "prompt": "p"})

        with self.assertRaises(RuntimeError) as error:
            run(scenario())
        self.assertIn("requires a Google provider profile", str(error.exception))
        self.assertEqual(self.requests, [])
        with self.assertRaises(ValueError):
            run(create_default_compaction_model_client().complete({"config": {}, "model": {"provider": "vertex", "model": "x", "purpose": "synthesis"}, "system": "s", "prompt": "p"}))

    def test_background_updates_are_written_by_the_sessions_gemini_model(self):
        async def scenario():
            config = await self._config()
            session = {"id": "s1", "provider": "google", "model": "gemini-4-hydrogen", "metadata": {}}
            monitor = await _author_monitor_update_with_provider(config, session, {"goal": "the build", "status": "active"}, {"status": "completed", "exitCode": 0}, "completed", "done", 0)
            collaboration = await author_collaboration_update_with_provider({"config": config, "session": {**session, "metadata": {}}, "worker": {"id": "w"}, "summary": "finished"})
            return monitor, collaboration, session

        monitor, collaboration, session = run(scenario())
        self.assertEqual((monitor, collaboration), ("A checkpoint.", "A checkpoint."))
        self.assertEqual(len(self.requests), 2)
        self.assertTrue(all(record["headers"]["x-goog-api-key"] == "loop-google-key" for record in self.requests))
        self.assertEqual((session["metadata"]["usage"]["inputTokens"], session["metadata"]["usage"]["cacheReadInputTokens"]), (800, 4200))
