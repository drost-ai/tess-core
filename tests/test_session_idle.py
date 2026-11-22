"""Closed is the default state: an idle session is unloaded by the runtime's
one sweep, a handle reopens it, and nothing is closed under a turn. Real
daemon and MCP server processes where process ownership is the claim.

Every test is one coroutine: a runtime belongs to the loop that started it.
"""
from __future__ import annotations

import random
import subprocess
import sys
import threading

from tests.support.tess_core_case import *

from tess_core import ProviderRegistry, SessionOptions, start_runtime
from tess_core.config.registry import ProviderRegistryError
from tess_core.runtime.secrets import resolve_runtime_secret
from tess_core._utils import is_process_alive
from tess_core.config._knobs import reset_configured_knobs
from tess_core.runtime import host as host_module
from tess_core.storage import load_daemon_state
from tess_core_harness.mcp_fakes import _server_config, _server_payload, _stdio_server_script

KNOB = "TESS_CORE_SESSIONS_IDLE_CLOSE_MS"


class Listener:
    def __init__(self) -> None:
        self.events: list[tuple[str, str]] = []
        self.opened: list[tuple[str, str]] = []
        self.closed: list[tuple[str, str]] = []
        self.runtime_events: list[tuple[str, dict]] = []

    def on_event(self, session_id, envelope):
        self.events.append((session_id, envelope["event"]))

    def on_session_opened(self, session_id, reason):
        self.opened.append((session_id, reason))

    def on_session_closed(self, session_id, reason):
        self.closed.append((session_id, reason))

    def on_runtime_event(self, event, data):
        self.runtime_events.append((event, data))

    def saw(self, session_id, event):
        return any(sid == session_id and name == event for sid, name in self.events)


def _provider(text="ok", *, delay=0.0, seen=None):
    async def send(request):
        if seen is not None:
            seen.append(request)
        if delay:
            await asyncio.sleep(delay)
        return {"model": "gpt-5.4", "stopReason": "end_turn", "usage": {"input_tokens": 1, "output_tokens": 1}, "content": [{"type": "text", "text": text}]}

    return {"send": send}


def _fake_mcp_server(workspace, name):
    script = Path(workspace) / f"{name}_mcp.py"
    script.write_text(_stdio_server_script(_server_payload({"name": name})), encoding="utf-8")
    return _server_config({"name": name, "updatedAt": "2026-01-01T00:00:00Z"}, "stdio", sys.executable, [str(script)])


def _mcp_pids(script_name: str) -> list[int]:
    out = subprocess.run(["pgrep", "-f", script_name], capture_output=True, text=True).stdout.split()
    return [int(pid) for pid in out if pid.isdigit()]


async def _start(home, workspace, listener, **kwargs):
    return await start_runtime(str(home), cwd=str(workspace), listener=listener, startup_timeout_seconds=20, **kwargs)


async def _wait(predicate, timeout=20.0, label=""):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.02)
    raise AssertionError(f"timeout waiting for {label}")


def _open_fds() -> int:
    return len(os.listdir("/dev/fd"))


class SessionIdleTests(TessCoreTestCase):
    def setUp(self):
        host_module._ACTIVE = None

    def tearDown(self):
        os.environ.pop(KNOB, None)
        reset_configured_knobs()   # resolve_config folded the env into the process-wide knobs

    def test_an_idle_session_closes_with_its_mcp_servers_while_the_daemon_stays(self):
        root, home, workspace, previous = self.temp_context()
        try:
            os.environ[KNOB] = "600"
            server = _fake_mcp_server(workspace, "idle_one")

            async def scenario():
                listener = Listener()
                runtime = await _start(home, workspace, listener, session_options=lambda sid, reason: SessionOptions(mcp_servers=(server,)))
                try:
                    handle = await runtime.sessions.open()
                    self.assertIn("mcp__idle_one__echo", handle.tool_names)
                    self.assertTrue(_mcp_pids("idle_one_mcp.py"))
                    await handle.run_turn("hello", provider_override=_provider())
                    finished = time.monotonic()
                    await _wait(lambda: listener.closed, label="idle close")
                    self.assertGreaterEqual(time.monotonic() - finished, 0.55)
                    self.assertEqual(listener.closed, [(handle.id, "idle")])
                    self.assertFalse(handle.is_open)
                    self.assertEqual(runtime.status()["sessions"]["open"], [])
                    await _wait(lambda: not _mcp_pids("idle_one_mcp.py"), label="MCP server exit")
                    daemon_pid = (await load_daemon_state(runtime.instance_id))["pid"]
                    self.assertTrue(is_process_alive(daemon_pid))
                    self.assertTrue(runtime.status()["daemon"]["connected"])
                    self.assertEqual(listener.runtime_events, [])
                finally:
                    await runtime.stop()

            run(scenario())
        finally:
            self.restore_home(root, previous)

    def test_a_reopened_session_keeps_the_credential_it_was_opened_with(self):
        """A host may hold a key in the environment only while it opens a session. The turn that
        reopens the session after an idle unload reads the key again, and must still find it."""
        root, home, workspace, previous = self.temp_context()
        variable = "TESS_CORE_IDLE_TEST_KEY"
        try:
            os.environ[KNOB] = "300"
            registry = ProviderRegistry.from_document({"schema_version": 1, "providerProfiles": {"model": {
                "kind": "openai", "apiMode": "chat_completions", "baseUrl": "http://127.0.0.1:9/v1",
                "defaultModel": "gpt-5.4", "auth": {"kind": "custom_bearer", "envVar": variable}}}})

            async def scenario():
                listener = Listener()
                runtime = await _start(home, workspace, listener, provider_registry=registry,
                                       session_options=lambda sid, reason: SessionOptions(provider="model"))

                async def key_in_use(handle):
                    token = runtime.sessions.live(handle.id).config["openaiToken"]
                    return bytes(await resolve_runtime_secret(token, token["purpose"])).decode()

                async def idle_out(handle, times):
                    await _wait(lambda: len(listener.closed) == times, label=f"idle close {times}")
                    self.assertFalse(handle.is_open)

                try:
                    os.environ[variable] = "first-key"
                    handle = await runtime.sessions.open()
                    del os.environ[variable]                      # the host takes the key away again
                    await handle.run_turn("hello", provider_override=_provider())
                    await idle_out(handle, 1)

                    # The turn reopens the session: the key is gone from the environment, and it has it.
                    result = await handle.run_turn("again", provider_override=_provider())
                    self.assertEqual(result["stopReason"], "end_turn")
                    self.assertEqual(await key_in_use(handle), "first-key")
                    self.assertEqual([reason for _, reason in listener.closed], ["idle"])

                    # A key the environment holds wins: this is how a rotated key reaches the session.
                    await idle_out(handle, 2)
                    os.environ[variable] = "second-key"
                    await handle.run_turn("rotated", provider_override=_provider())
                    self.assertEqual(await key_in_use(handle), "second-key")

                    # And it is the newest one that is kept once the environment loses it again.
                    del os.environ[variable]
                    await idle_out(handle, 3)
                    await handle.run_turn("after", provider_override=_provider())
                    self.assertEqual(await key_in_use(handle), "second-key")

                    # Another session is not given this one's key.
                    with self.assertRaises(ProviderRegistryError):
                        await runtime.sessions.open()
                    self.assertEqual(set(runtime.sessions._credentials), {handle.id})

                    # Ending the session forgets it.
                    await runtime.sessions.end(handle.id)
                    self.assertEqual(runtime.sessions._credentials, {})
                finally:
                    await runtime.stop()

            run(scenario())
        finally:
            os.environ.pop(variable, None)
            self.restore_home(root, previous)

    def test_a_session_moves_to_another_profile_only_when_the_host_says_so(self):
        """A session keeps the profile it was opened with when the host's default changes; a host
        whose owner chose another model for that session says so, and the session is moved."""
        root, home, workspace, previous = self.temp_context()
        variable = "TESS_CORE_REBIND_TEST_KEY"
        try:
            os.environ[variable] = "a-key"

            def profile(model):
                return {"kind": "openai", "apiMode": "chat_completions", "baseUrl": "http://127.0.0.1:9/v1",
                        "defaultModel": model, "auth": {"kind": "custom_bearer", "envVar": variable}}

            registry = ProviderRegistry.from_document({"schema_version": 1, "providerProfiles": {"first": profile("gpt-5.4"), "second": profile("gpt-5.4-mini")}})
            wanted = {"options": SessionOptions(provider="first")}

            async def scenario():
                from tess_core.storage import load_session

                listener = Listener()
                runtime = await _start(home, workspace, listener, provider_registry=registry, session_options=lambda sid, reason: wanted["options"])

                async def bound(handle):
                    record = runtime.sessions.live(handle.id).record
                    stored = await load_session(handle.id)
                    self.assertEqual((stored["providerProfileName"], stored["model"]), (record["providerProfileName"], record["model"]))
                    return record["providerProfileName"], record["model"], handle.provider_profile["model"]

                try:
                    handle = await runtime.sessions.open()
                    await handle.run_turn("hello", provider_override=_provider())
                    self.assertEqual(await bound(handle), ("first", "gpt-5.4", "gpt-5.4"))

                    # The host's selection changed: the session stays where it is.
                    wanted["options"] = SessionOptions(provider="second")
                    await runtime.sessions.close(handle.id)
                    await handle.run_turn("still here", provider_override=_provider())
                    self.assertEqual(await bound(handle), ("first", "gpt-5.4", "gpt-5.4"))

                    # The owner chose the other model for this session: it is moved, and its history comes with it.
                    wanted["options"] = SessionOptions(provider="second", rebind_provider=True)
                    await runtime.sessions.close(handle.id)
                    result = await handle.run_turn("moved", provider_override=_provider())
                    self.assertEqual(result["stopReason"], "end_turn")
                    self.assertEqual(await bound(handle), ("second", "gpt-5.4-mini", "gpt-5.4-mini"))
                    self.assertEqual(sum(message.get("role") == "user" for message in runtime.sessions.live(handle.id).record["messages"]), 3)

                    # And it stays moved when the host stops saying so.
                    wanted["options"] = SessionOptions(provider="first")
                    await runtime.sessions.close(handle.id)
                    await handle.run_turn("settled", provider_override=_provider())
                    self.assertEqual(await bound(handle), ("second", "gpt-5.4-mini", "gpt-5.4-mini"))

                    # Back again, with a model and an effort named.
                    wanted["options"] = SessionOptions(provider="first", model="gpt-5.4", effort="high", rebind_provider=True)
                    await runtime.sessions.close(handle.id)
                    await handle.run_turn("back", provider_override=_provider())
                    self.assertEqual(await bound(handle), ("first", "gpt-5.4", "gpt-5.4"))
                    self.assertEqual(runtime.sessions.live(handle.id).record["effort"], "high")
                finally:
                    await runtime.stop()

            run(scenario())
        finally:
            os.environ.pop(variable, None)
            self.restore_home(root, previous)

    def test_a_session_with_a_live_child_still_closes(self):
        root, home, workspace, previous = self.temp_context()
        try:
            os.environ[KNOB] = "600"

            async def scenario():
                listener = Listener()
                runtime = await _start(home, workspace, listener)
                try:
                    handle = await runtime.sessions.open()
                    daemon = runtime.sessions.live(handle.id).bindings["daemon"]["client"]
                    started = await daemon["startBackgroundCommand"]({"sessionId": handle.id, "command": "sleep 30", "cwd": str(workspace), "label": "long"})

                    async def task_status():
                        listed = await runtime.daemon["listTasks"](handle.id)
                        mine = [task for task in listed if started["taskId"] in (task.get("taskId"), task.get("id"))]
                        return mine[0].get("status") if mine else None

                    live_statuses = {"running", "backgrounded"}
                    deadline = time.monotonic() + 10
                    while await task_status() not in live_statuses and time.monotonic() < deadline:
                        await asyncio.sleep(0.05)
                    self.assertIn(await task_status(), live_statuses)
                    await _wait(lambda: listener.closed, label="idle close with a live child")
                    self.assertEqual(listener.closed, [(handle.id, "idle")])
                    self.assertIn(await task_status(), live_statuses)   # the child outlives the parent's unload
                finally:
                    await runtime.stop()

            run(scenario())
        finally:
            self.restore_home(root, previous)

    def test_run_turn_on_a_stale_handle_reopens_and_continues_the_transcript(self):
        root, home, workspace, previous = self.temp_context()
        try:
            os.environ[KNOB] = "300"
            asked = []

            def options_for(session_id, reason):
                asked.append((session_id, reason))
                return SessionOptions()

            async def scenario():
                listener = Listener()
                runtime = await _start(home, workspace, listener, daemon_enabled=False, session_options=options_for)
                try:
                    handle = await runtime.sessions.open()
                    await handle.run_turn("remember the word pelican", provider_override=_provider("noted: pelican"))
                    await _wait(lambda: listener.closed == [(handle.id, "idle")], label="idle close")
                    self.assertFalse(handle.is_open)

                    seen = []
                    result = await handle.run_turn("which word?", provider_override=_provider("pelican", seen=seen))
                    self.assertEqual(result["text"], "pelican")
                    self.assertTrue(handle.is_open)
                    self.assertEqual(listener.opened, [(handle.id, "client"), (handle.id, "client")])
                    self.assertEqual(asked, [(handle.id, "client"), (handle.id, "client")])
                    transcript = json.dumps(seen[0]["messages"])
                    self.assertIn("remember the word pelican", transcript)
                    self.assertIn("noted: pelican", transcript)
                    self.assertIn("which word?", transcript)
                    self.assertEqual(handle.message_count, 4)
                    stored = await core.load_session(handle.id)
                    self.assertEqual(len(stored["messages"]), 4)
                finally:
                    await runtime.stop()

            run(scenario())
        finally:
            self.restore_home(root, previous)

    def test_open_close_cycles_leak_nothing(self):
        root, home, workspace, previous = self.temp_context()
        try:
            os.environ[KNOB] = "100"
            server = _fake_mcp_server(workspace, "cycle")

            async def scenario():
                listener = Listener()
                runtime = await _start(home, workspace, listener, daemon_enabled=False, session_options=lambda sid, reason: SessionOptions(mcp_servers=(server,)))
                try:
                    async def cycle(n):
                        handle = await runtime.sessions.open()
                        self.assertIn("mcp__cycle__echo", handle.tool_names)
                        await handle.run_turn(f"turn {n}", provider_override=_provider())
                        await _wait(lambda: len(listener.closed) == n, label=f"idle close {n}")
                        return handle.id

                    await cycle(1)   # warm-up: lazy imports and thread pools
                    await _wait(lambda: not _mcp_pids("cycle_mcp.py"), label="warm-up MCP exit")
                    threads, fds, tasks = threading.active_count(), _open_fds(), len(asyncio.all_tasks())
                    ids = {await cycle(n) for n in range(2, 52)}
                    self.assertEqual(len(ids), 50)
                    await _wait(lambda: not _mcp_pids("cycle_mcp.py"), label="last MCP exit")
                    self.assertEqual({reason for _, reason in listener.closed}, {"idle"})
                    self.assertEqual(len(listener.closed), 51)
                    self.assertEqual(runtime.sessions.open_ids(), [])
                    self.assertLessEqual(threading.active_count(), threads + 2)
                    self.assertLessEqual(_open_fds(), fds + 4)
                    self.assertLessEqual(len(asyncio.all_tasks()), tasks + 1)
                    self.assertEqual(listener.runtime_events, [])
                finally:
                    await runtime.stop()

            run(scenario())
        finally:
            self.restore_home(root, previous)

    def test_status_says_why_a_session_is_open(self):
        root, home, workspace, previous = self.temp_context()
        try:
            os.environ[KNOB] = "400"

            async def scenario():
                listener = Listener()
                runtime = await _start(home, workspace, listener, daemon_enabled=False)
                try:
                    handle = await runtime.sessions.open()

                    def status():
                        return runtime.status()["sessions"]["open"][0]

                    self.assertEqual(status()["state"], "idle")
                    self.assertEqual(status()["openedAt"][:4], "2026")
                    self.assertGreaterEqual(status()["idleSeconds"], 0.0)
                    gate = asyncio.Event()

                    async def blocked(_request):
                        await gate.wait()
                        return (await _provider("done")["send"](_request))

                    first = asyncio.ensure_future(handle.run_turn("slow", turn_id="t-slow", provider_override={"send": blocked}))
                    second = asyncio.ensure_future(handle.run_turn("next", turn_id="t-next", provider_override=_provider("next")))
                    await _wait(lambda: handle.turns()["queued"], label="a queued turn")
                    self.assertEqual((status()["state"], status()["idleSeconds"], status()["queued"], status()["turn"]["id"]), ("running", None, 1, "t-slow"))
                    await handle.cancel_turn("t-next")
                    self.assertEqual((status()["state"], status()["queued"]), ("running", 0))
                    gate.set()
                    await first
                    await second
                    self.assertEqual(status()["state"], "idle")
                    self.assertLess(status()["idleSeconds"], 0.3)
                    self.assertEqual(await runtime.sessions.idle.sweep_once(), [])   # not idle long enough yet
                    self.assertTrue(handle.is_open)

                    await _wait(lambda: listener.closed, label="idle close")
                    self.assertEqual(listener.closed, [(handle.id, "idle")])
                    self.assertEqual(runtime.status()["sessions"]["open"], [])
                    self.assertEqual(await runtime.sessions.idle.sweep_once(), [])   # nothing left to close
                finally:
                    await runtime.stop()

            run(scenario())
        finally:
            self.restore_home(root, previous)

    def test_the_sweep_never_closes_a_busy_session(self):
        root, home, workspace, previous = self.temp_context()
        try:
            os.environ[KNOB] = "100"

            async def scenario():
                listener = Listener()
                runtime = await _start(home, workspace, listener, daemon_enabled=False)
                try:
                    handle = await runtime.sessions.open()
                    # Always busy: turns longer than the limit, back to back.
                    until = time.monotonic() + 1.2
                    while time.monotonic() < until:
                        await handle.run_turn("busy", provider_override=_provider(delay=0.15))
                    self.assertEqual(listener.closed, [])

                    # Gaps around the limit: closes happen, and never break a turn.
                    rng = random.Random(7)
                    for n in range(20):
                        await asyncio.sleep(rng.uniform(0.05, 0.16))
                        result = await handle.run_turn(f"gap {n}", provider_override=_provider(f"reply {n}"))
                        self.assertEqual(result["text"], f"reply {n}")
                    self.assertIn(len(listener.opened) - len(listener.closed), (0, 1))
                    self.assertEqual({reason for _, reason in listener.closed}, {"idle"})

                    # The sweep and a new turn in the same tick: whichever runs
                    # first, the turn completes and the session ends up open once.
                    for n in range(30):
                        await asyncio.sleep(0.11)
                        _closed, result = await asyncio.gather(runtime.sessions.idle.sweep_once(), handle.run_turn(f"race {n}", provider_override=_provider(f"race {n}")))
                        self.assertEqual(result["text"], f"race {n}")
                        self.assertEqual(runtime.sessions.open_ids(), [handle.id])
                    self.assertEqual(listener.runtime_events, [])
                    stored = await core.load_session(handle.id)
                    prompts = [m["content"] for m in stored["messages"] if m["role"] == "user" and isinstance(m.get("content"), str)]
                    self.assertEqual(prompts[-30:], [f"race {n}" for n in range(30)])   # nothing lost across the reopens
                finally:
                    await runtime.stop()

            run(scenario())
        finally:
            self.restore_home(root, previous)

    def test_stopping_the_sweeper_waits_for_a_release_it_was_in_the_middle_of(self):
        from tess_core.runtime.session_idle import IdleSweeper

        async def scenario():
            releasing, finish, released = asyncio.Event(), asyncio.Event(), []

            class Manager:
                _live = {"s1": object()}

                def detach_if_idle(self, session_id, limit):
                    return self._live.pop(session_id, None)

                async def release(self, session_id, live, reason):
                    releasing.set()
                    await finish.wait()
                    released.append(session_id)

            sweeper = IdleSweeper(Manager())
            sweeper._task = asyncio.ensure_future(sweeper.sweep_once())
            await releasing.wait()
            stopping = asyncio.ensure_future(sweeper.stop())
            await asyncio.sleep(0.05)
            self.assertFalse(stopping.done())    # the half-closed session is still being released
            finish.set()
            await stopping
            self.assertEqual(released, ["s1"])

        run(scenario())
