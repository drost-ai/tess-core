"""Sessions opened inside a started runtime share its one daemon. Real daemon
processes and real MCP server processes, because ownership is the claim.

Every test is one coroutine: a runtime belongs to the event loop that started
it (daemon events are routed onto that loop), as in a real host.
"""
from __future__ import annotations

import subprocess
import sys

from tests.support.tess_core_case import *

from tess_core import (
    SessionBusy,
    SessionOptions,
    SessionUnavailableError,
    Unavailable,
    UnknownSessionError,
    start_runtime,
)
from tess_core._utils import is_process_alive
from tess_core.runtime import host as host_module
from tess_core.storage import load_daemon_state
from tess_core_harness.mcp_fakes import _server_config, _server_payload, _stdio_server_script


class Listener:
    def __init__(self) -> None:
        self.events: list[tuple[str, str]] = []
        self.opened: list[tuple[str, str]] = []
        self.closed: list[tuple[str, str]] = []
        self.runtime_events: list[str] = []

    def on_event(self, session_id, envelope):
        self.events.append((session_id, envelope["event"]))

    def on_session_opened(self, session_id, reason):
        self.opened.append((session_id, reason))

    def on_session_closed(self, session_id, reason):
        self.closed.append((session_id, reason))

    def on_runtime_event(self, event, data):
        self.runtime_events.append(event)

    def saw(self, session_id, event):
        return any(sid == session_id and name == event for sid, name in self.events)


async def _wait(predicate, timeout=20.0, label=""):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.05)
    raise AssertionError(f"timeout waiting for {label}")


def _fake_mcp_server(workspace, name="fake"):
    script = Path(workspace) / f"{name}_mcp.py"
    script.write_text(_stdio_server_script(_server_payload({"name": name})), encoding="utf-8")
    return _server_config({"name": name, "updatedAt": "2026-01-01T00:00:00Z"}, "stdio", sys.executable, [str(script)])


def _mcp_pids(script_name: str) -> list[int]:
    out = subprocess.run(["pgrep", "-f", script_name], capture_output=True, text=True).stdout.split()
    return [int(pid) for pid in out if pid.isdigit()]


async def _daemon_pid(runtime) -> int:
    return (await load_daemon_state(runtime.instance_id))["pid"]


async def _start(home, workspace, options_for=None, listener=None, **kwargs):
    return await start_runtime(str(home), cwd=str(workspace), session_options=options_for, listener=listener or Listener(), startup_timeout_seconds=20, **kwargs)


class SessionManagerTests(TessCoreTestCase):
    def setUp(self):
        host_module._ACTIVE = None

    def test_sessions_share_the_runtime_daemon_and_close_independently(self):
        root, home, workspace, previous = self.temp_context()
        try:
            async def scenario():
                listener = Listener()
                runtime = await _start(home, workspace, listener=listener)
                try:
                    alpha = await runtime.sessions.open()
                    bravo = await runtime.sessions.open()
                    self.assertNotEqual(alpha.id, bravo.id)
                    self.assertEqual(os.listdir(home / "daemon" / "instances"), [runtime.instance_id])
                    self.assertEqual(sorted(runtime.sessions.open_ids()), sorted([alpha.id, bravo.id]))
                    self.assertEqual({s["id"] for s in runtime.status()["sessions"]["open"]}, {alpha.id, bravo.id})
                    for handle in (alpha, bravo):
                        self.assertIn("Agent", handle.tool_names)  # a connected daemon: sub-agents available
                        self.assertEqual(runtime.sessions.live(handle.id).bindings["daemon"]["state"], "connected")
                    self.assertEqual(listener.opened, [(alpha.id, "client"), (bravo.id, "client")])
                    self.assertTrue(listener.saw(alpha.id, "session.activated"))

                    await runtime.sessions.close(alpha.id)
                    self.assertFalse(alpha.is_open)
                    self.assertTrue(bravo.is_open)
                    self.assertEqual(listener.closed, [(alpha.id, "client")])
                    daemon_pid = await _daemon_pid(runtime)
                    self.assertTrue(is_process_alive(daemon_pid))
                    self.assertTrue(runtime.status()["daemon"]["connected"])
                    # The survivor's daemon proxy still works.
                    listed = await runtime.sessions.live(bravo.id).bindings["daemon"]["client"]["listTasks"](bravo.id)
                    self.assertIsInstance(listed, list)
                    await runtime.sessions.close(alpha.id)  # closing a closed session is a no-op
                finally:
                    await runtime.stop()
                self.assertEqual(listener.closed[-1], (bravo.id, "runtime_stopped"))
                self.assertFalse(is_process_alive(daemon_pid))

            run(scenario())
        finally:
            self.restore_home(root, previous)

    def test_closing_a_session_releases_its_mcp_servers_only(self):
        root, home, workspace, previous = self.temp_context()
        try:
            async def scenario():
                servers = [_fake_mcp_server(workspace, "one"), _fake_mcp_server(workspace, "two")]

                def options_for(session_id, reason):
                    return SessionOptions(mcp_servers=(servers.pop(0),))

                runtime = await _start(home, workspace, options_for=options_for)
                try:
                    one = await runtime.sessions.open()
                    two = await runtime.sessions.open()
                    self.assertIn("mcp__one__echo", one.tool_names)
                    self.assertIn("mcp__two__echo", two.tool_names)
                    # The launcher may run a server behind a shell: count presence, not processes.
                    self.assertTrue(_mcp_pids("one_mcp.py"))
                    two_before = _mcp_pids("two_mcp.py")
                    self.assertTrue(two_before)

                    await runtime.sessions.close(one.id)
                    await _wait(lambda: not _mcp_pids("one_mcp.py"), label="session one's MCP server exit")
                    self.assertEqual(_mcp_pids("two_mcp.py"), two_before)
                    self.assertTrue(is_process_alive(await _daemon_pid(runtime)))
                finally:
                    await runtime.stop()
                await _wait(lambda: not _mcp_pids("two_mcp.py"), label="session two's MCP server exit at stop")

            run(scenario())
        finally:
            self.restore_home(root, previous)

    def test_daemon_events_reach_only_their_session(self):
        root, home, workspace, previous = self.temp_context()
        try:
            async def scenario():
                listener = Listener()
                runtime = await _start(home, workspace, listener=listener)
                try:
                    alpha = await runtime.sessions.open()
                    bravo = await runtime.sessions.open()
                    daemon = runtime.sessions.live(alpha.id).bindings["daemon"]["client"]
                    started = await daemon["startBackgroundCommand"]({"sessionId": alpha.id, "command": "echo hello-from-alpha", "cwd": str(workspace), "label": "hello"})
                    await _wait(lambda: listener.saw(alpha.id, "task.completed"), label="alpha's task completion event")
                    self.assertEqual([e for sid, e in listener.events if sid == bravo.id and e.startswith("task.")], [])
                    output = await daemon["read_task_output"](started["taskId"], None)
                    self.assertIn("hello-from-alpha", json.dumps(output))
                finally:
                    await runtime.stop()

            run(scenario())
        finally:
            self.restore_home(root, previous)

    def test_options_come_from_the_host_and_unavailable_blocks_open(self):
        root, home, workspace, previous = self.temp_context()
        try:
            async def scenario():
                asked = []
                workspace_b = Path(root.name) / "elsewhere"
                workspace_b.mkdir()

                def options_for(session_id, reason):
                    asked.append((session_id, reason))
                    if session_id == "archived-one":
                        return Unavailable("archived", retry=False)
                    return SessionOptions(cwd=str(workspace_b), permission_mode="bypassPermissions", add_dirs=(str(workspace),))

                runtime = await _start(home, workspace, options_for=options_for)
                try:
                    handle = await runtime.sessions.open()
                    live = runtime.sessions.live(handle.id)
                    self.assertEqual(asked, [(handle.id, "client")])  # asked with the id it was about to create
                    self.assertEqual(live.config["cwd"], str(workspace_b))
                    self.assertEqual(handle.permission_mode, "bypassPermissions")
                    self.assertIn(str(workspace), live.config["allowedRoots"])

                    with self.assertRaises(SessionUnavailableError) as refused:
                        await runtime.sessions.open("archived-one")
                    self.assertEqual((refused.exception.reason, refused.exception.retry), ("archived", False))
                    self.assertEqual(runtime.sessions.open_ids(), [handle.id])

                    with self.assertRaises(UnknownSessionError):
                        await runtime.sessions.open("does-not-exist")
                    self.assertIsNotNone(await core.load_session(handle.id))  # a new session exists from its open
                    self.assertIsNone(await core.load_session("does-not-exist"))  # nothing was created for an unknown id
                finally:
                    await runtime.stop()

            run(scenario())
        finally:
            self.restore_home(root, previous)

    def test_reopening_returns_the_same_handle_and_continues_the_record(self):
        root, home, workspace, previous = self.temp_context()
        try:
            async def scenario():
                runtime = await _start(home, workspace)
                try:
                    first = await runtime.sessions.open()
                    self.assertIs(await runtime.sessions.open(first.id), first)
                    live = runtime.sessions.live(first.id)
                    live.record["messages"].append({"role": "user", "content": "remember heliotrope"})
                    await core.save_session(live.record)
                    await runtime.sessions.close(first.id)
                    again = await runtime.sessions.open(first.id)
                    self.assertIs(again, first)
                    self.assertTrue(again.is_open)
                    self.assertIn("heliotrope", json.dumps(runtime.sessions.live(first.id).record["messages"]))
                finally:
                    await runtime.stop()

            run(scenario())
        finally:
            self.restore_home(root, previous)

    def test_root_authority_follows_its_options_after_a_spawn_pinned_it(self):
        """The engine pins a session's authority when it spawns a child. A root
        must still answer to whoever opens it next: a wider permission mode and
        an extra root reach it, while a child keeps what it was granted."""
        root, home, workspace, previous = self.temp_context()
        try:
            async def scenario():
                elsewhere = Path(root.name) / "elsewhere"
                elsewhere.mkdir()
                phase = {"mode": "default", "dirs": ()}

                def options_for(session_id, reason):
                    return SessionOptions(permission_mode=phase["mode"], add_dirs=phase["dirs"])

                runtime = await _start(home, workspace, options_for=options_for)
                try:
                    handle = await runtime.sessions.open()
                    live = runtime.sessions.live(handle.id)
                    pinned = await core.capture_agent_authority(config=live.config, session=live.record, tools=live.components["tools"])
                    live.record.setdefault("metadata", {})["effectiveAgentAuthority"] = pinned  # what a spawn does
                    await core.save_session(live.record)
                    self.assertEqual(pinned["mode"], "default")
                    await runtime.sessions.close(handle.id)

                    phase.update(mode="bypassPermissions", dirs=(str(elsewhere),))
                    await runtime.sessions.open(handle.id)
                    reopened = runtime.sessions.live(handle.id)
                    self.assertEqual(reopened.config["permissionMode"], "bypassPermissions")
                    self.assertEqual(reopened.record["permissionMode"], "bypassPermissions")
                    self.assertIn(os.path.realpath(elsewhere), [os.path.realpath(p) for p in reopened.config["allowedRoots"]])
                    # The next spawn derives from the root's current authority, not the stale pin.
                    fresh = await core.capture_agent_authority(config=reopened.config, session=reopened.record, tools=reopened.components["tools"])
                    self.assertEqual(fresh["mode"], "bypassPermissions")
                    self.assertTrue(any(os.path.realpath(elsewhere) == os.path.realpath(r) for r in fresh["readableRoots"]))

                    # A child session keeps its granted authority whatever the config says.
                    child = core.create_empty_session(cwd=str(workspace), provider=reopened.config["provider"], providerProfileName=reopened.config.get("providerProfileName"), model=reopened.config["model"], effort=reopened.config.get("effort"), systemPrompt=reopened.config["systemPrompt"], permissionMode="default")
                    child["metadata"]["parentSessionId"] = handle.id
                    child["metadata"]["effectiveAgentAuthority"] = pinned
                    child_config = dict(reopened.config)
                    enforced = await core.enforce_session_agent_authority(session=child, config=child_config)
                    self.assertEqual((enforced["mode"], child_config["permissionMode"]), ("default", "default"))
                finally:
                    await runtime.stop()

            run(scenario())
        finally:
            self.restore_home(root, previous)

    def test_close_is_refused_while_a_turn_is_in_flight(self):
        root, home, workspace, previous = self.temp_context()
        try:
            async def scenario():
                runtime = await _start(home, workspace, daemon_enabled=False)
                try:
                    handle = await runtime.sessions.open()
                    live = runtime.sessions.live(handle.id)
                    live.controller["startTurn"]({"prompt": "working"})
                    with self.assertRaises(SessionBusy):
                        await runtime.sessions.close(handle.id)
                    self.assertTrue(handle.is_open)
                    live.controller["finishTurn"]({"kind": "completed"})
                    await runtime.sessions.close(handle.id)
                    self.assertFalse(handle.is_open)
                finally:
                    await runtime.stop()

            run(scenario())
        finally:
            self.restore_home(root, previous)

    def test_daemon_disabled_sessions_have_no_supervisor(self):
        root, home, workspace, previous = self.temp_context()
        try:
            async def scenario():
                runtime = await _start(home, workspace, daemon_enabled=False)
                try:
                    handle = await runtime.sessions.open()
                    binding = runtime.sessions.live(handle.id).bindings["daemon"]
                    self.assertEqual(binding["state"], "unavailable")
                    self.assertEqual(binding["reason"]["code"], "runtime.daemon_disabled")
                finally:
                    await runtime.stop()

            run(scenario())
        finally:
            self.restore_home(root, previous)

    def test_the_host_has_the_last_word_on_the_config_and_may_ask_for_raw_provider_events(self):
        root, home, workspace, previous = self.temp_context()
        try:
            async def scenario():
                seen = []

                async def adjust(config):
                    seen.append(dict(config))
                    return {**config, "model": "adjusted-model"}

                def options_for(session_id, reason):
                    return SessionOptions(model="requested-model", adjust_config=adjust, stream_provider_messages=True)

                runtime = await _start(home, workspace, options_for=options_for, daemon_enabled=False)
                try:
                    handle = await runtime.sessions.open()
                    self.assertEqual(seen[0]["model"], "requested-model")   # resolved first, then the host's word
                    self.assertEqual(handle.model, "adjusted-model")
                    live = runtime.sessions.live(handle.id)
                    self.assertEqual(live.config["model"], "adjusted-model")
                    self.assertTrue(live.bindings["streamProviderMessages"])
                finally:
                    await runtime.stop()

            run(scenario())
        finally:
            self.restore_home(root, previous)

    def test_a_host_may_note_metadata_narrow_a_turns_tools_and_read_the_profile(self):
        root, home, workspace, previous = self.temp_context()
        try:
            async def scenario():
                seen = []

                async def send(request):
                    seen.append([tool.get("name") for tool in request.get("tools") or []])
                    return {"model": "gpt-5.4", "stopReason": "end_turn", "usage": {}, "content": [{"type": "text", "text": "ok"}]}

                runtime = await _start(home, workspace, options_for=lambda sid, reason: SessionOptions(provider_override={"send": send}), daemon_enabled=False)
                try:
                    handle = await runtime.sessions.open()
                    handle.set_metadata("hostNote", {"origin": "test"})
                    self.assertEqual(runtime.sessions.live(handle.id).record["metadata"]["hostNote"], {"origin": "test"})
                    profile = handle.provider_profile
                    self.assertEqual(profile["model"], handle.model)
                    self.assertIn("kind", profile["profile"])
                    await handle.run_turn("all tools")
                    await handle.run_turn("no tools", tools=[])
                    await handle.run_turn("one tool", tools=["Read"])
                    await handle.run_turn("all again")
                    self.assertGreater(len(seen[0]), 1)
                    self.assertEqual(seen[1], [])
                    self.assertEqual(seen[2], ["Read"])
                    self.assertEqual(seen[3], seen[0])   # the narrowing was one turn's
                    with self.assertRaises(RuntimeError):
                        await handle.mcp_call("nope", "tool")  # no MCP server named so
                finally:
                    await runtime.stop()

            run(scenario())
        finally:
            self.restore_home(root, previous)

    def test_sessions_follow_the_daemon_across_a_recovery(self):
        root, home, workspace, previous = self.temp_context()
        try:
            os.environ["TESS_CORE_DAEMON_HEARTBEAT_MS"] = "300"

            async def scenario():
                listener = Listener()
                runtime = await _start(home, workspace, listener=listener)
                try:
                    alpha = await runtime.sessions.open()
                    first_pid = await _daemon_pid(runtime)
                    os.kill(first_pid, 9)
                    await _wait(lambda: runtime.status()["daemon"]["recoveries"] == 1, label="daemon recovery")
                    daemon = runtime.sessions.live(alpha.id).bindings["daemon"]["client"]  # the proxy follows the new daemon
                    started = await daemon["startBackgroundCommand"]({"sessionId": alpha.id, "command": "echo after-recovery", "cwd": str(workspace), "label": "after"})
                    await _wait(lambda: listener.saw(alpha.id, "task.completed"), label="task event through the re-subscribed daemon")
                    self.assertIn("after-recovery", json.dumps(await daemon["read_task_output"](started["taskId"], None)))
                finally:
                    await runtime.stop()

            run(scenario())
        finally:
            os.environ.pop("TESS_CORE_DAEMON_HEARTBEAT_MS", None)
            self.restore_home(root, previous)
