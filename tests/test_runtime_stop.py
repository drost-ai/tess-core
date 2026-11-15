"""Stopping is not undoing: a bounded runtime.stop() that refuses new work
and says what it did; honest records for workers killed by a stop, by the
daemon's dead-man switch after a host crash, or found dead at the next
start; nothing resumed, nothing redelivered. Real daemon, worker and host
processes. Every test is one coroutine.
"""
from __future__ import annotations

import signal
import subprocess
import sys
import textwrap

from tests.support.tess_core_case import *
from tests.test_session_end import Listener, _child_pid, _reply, _settings, _spawn_child, _wait, _wait_async

from tess_core import RuntimeStopped, SessionOptions, start_runtime
from tess_core._utils import is_process_alive
from tess_core.config._knobs import reset_configured_knobs
from tess_core.runtime import host as host_module
from tess_core.runtime.owner_lease import read_owner_lease
from tess_core.storage.records import save_task_record
from tess_core.tasks.communication import read_task_events, read_task_mailbox_messages, send_task_message

_ENV = ("TESS_CORE_SUBAGENTS_RESIDENT_POLL_MS", "TESS_CORE_TEST_MISSING_END_KEY", "TESS_CORE_SESSIONS_IDLE_CLOSE_MS", "TESS_CORE_DAEMON_MAILBOX_WATCH_MS")

HOST_SCRIPT = textwrap.dedent(
    """
    import asyncio, json, os, sys
    import tess_core._facade as core
    from tess_core import start_runtime

    async def main():
        runtime = await start_runtime(sys.argv[1], cwd=os.getcwd(), startup_timeout_seconds=20)
        session = await runtime.sessions.open()
        daemon = runtime.sessions.live(session.id).bindings["daemon"]["client"]
        started = await daemon["startBackgroundCommand"]({"sessionId": session.id, "command": "sleep 120", "cwd": os.getcwd(), "label": "long"})
        pid = None
        for _ in range(200):
            meta = await core.read_task_meta(started["taskId"]) or {}
            pid = meta.get("pid")
            if isinstance(pid, int):
                break
            await asyncio.sleep(0.05)
        print(json.dumps({"pid": os.getpid(), "daemon_pid": runtime.status()["daemon"]["pid"], "task_id": started["taskId"], "worker_pid": pid, "session_id": session.id}), flush=True)
        await asyncio.sleep(3600)   # until killed

    asyncio.run(main())
    """
)


class RuntimeStopTests(TessCoreTestCase):
    def setUp(self):
        host_module._ACTIVE = None
        os.environ["TESS_CORE_SUBAGENTS_RESIDENT_POLL_MS"] = "100"

    def tearDown(self):
        for name in _ENV:
            os.environ.pop(name, None)
        reset_configured_knobs()

    def test_stop_refuses_new_work_and_reports_what_it_did(self):
        root, home, workspace, previous = self.temp_context()
        try:
            async def scenario():
                listener = Listener()
                runtime = await start_runtime(str(home), cwd=str(workspace), listener=listener, daemon_enabled=False,
                                              session_options=lambda sid, reason: SessionOptions(provider_override=_reply()), startup_timeout_seconds=20)
                gate, release = asyncio.Event(), asyncio.Event()

                async def blocked(_request):
                    await gate.wait()
                    return {"model": "gpt-5.4", "stopReason": "end_turn", "usage": {}, "content": [{"type": "text", "text": "late"}]}

                async def resistant():
                    while not release.is_set():
                        try:
                            await release.wait()
                        except asyncio.CancelledError:
                            pass

                try:
                    handle = await runtime.sessions.open()
                    live = runtime.sessions.live(handle.id)
                    running = asyncio.ensure_future(handle.run_turn("slow", turn_id="t-slow", provider_override={"send": blocked}))
                    queued = asyncio.ensure_future(handle.run_turn("next", turn_id="t-next"))
                    await _wait(lambda: handle.turns()["queued"], label="a queued turn")
                    live.monitor_cleanup.add(asyncio.ensure_future(resistant()))   # unfinished monitor cleanup, quarantined

                    summary = await runtime.stop(grace=0.2)
                    self.assertEqual(summary["sessions"], {"closed": 1, "runningTurns": 1, "queuedTurns": 1, "unfinishedCleanup": 1})
                    self.assertIsNone(summary["daemon"])
                    self.assertEqual((await running)["stopReason"], "cancelled")    # beyond the grace: aborted
                    self.assertEqual((await queued)["stopReason"], "cancelled")     # never started
                    self.assertEqual(listener.closed, [(handle.id, "runtime_stopped")])
                    with self.assertRaises(RuntimeStopped):
                        await handle.run_turn("anyone?")
                    with self.assertRaises(RuntimeStopped):
                        await runtime.sessions.open()
                    self.assertEqual(await runtime.stop(), summary)   # idempotent, same account
                finally:
                    release.set()
                    gate.set()
                    await runtime.stop()

            run(scenario())
        finally:
            self.restore_home(root, previous)

    def test_stop_with_a_working_child_records_runtime_stopped_and_never_resumes_it(self):
        root, home, workspace, previous = self.temp_context()
        try:
            settings_path = _settings(home)

            def options_for(session_id, reason):
                return SessionOptions(provider="end-openai", settings=str(settings_path), permission_mode="bypassPermissions", provider_override=_reply())

            async def scenario():
                listener = Listener()
                runtime = await start_runtime(str(home), cwd=str(workspace), listener=listener, session_options=options_for, startup_timeout_seconds=20)
                daemon_pid = runtime.status()["daemon"]["pid"]
                handle = await runtime.sessions.open()
                await handle.run_turn("start a child")
                config = runtime.sessions.live(handle.id).config
                child_session, task_id = await _spawn_child(runtime, handle.id, "child-stop", settings_path, workspace, config)
                pid = await _wait_async(lambda: _child_pid("child-stop"), label="child alive")
                self.assertTrue(is_process_alive(pid))

                summary = await runtime.stop()
                self.assertEqual(summary["workers"]["stoppedTasks"], [task_id])
                self.assertEqual(summary["workers"]["failures"], [])
                self.assertEqual(summary["daemon"], {"pid": daemon_pid, "exited": True})
                self.assertFalse(is_process_alive(pid))
                self.assertFalse(is_process_alive(daemon_pid))
                record = await core.load_child_agent("child-stop")
                self.assertEqual((record["status"], record["stopReason"], record["retryable"], record["stoppable"]), ("stopped", "runtime_stopped", False, False))
                meta = await core.read_task_meta(task_id)
                # The meta keeps the engine's process vocabulary (a killed worker is
                # "terminated"); the record the parent sees says stopped, and both say why.
                self.assertIn(meta["status"], {"stopped", "terminated"})
                self.assertEqual(meta["stopReason"], "runtime_stopped")
                self.assertEqual([e["type"] for e in await read_task_events(task_id) if e.get("type") == "task.runtime_stopped"], ["task.runtime_stopped"])
                self.assertIsNone(await read_owner_lease(str(home)))
                self.assertEqual(listener.closed, [(handle.id, "runtime_stopped")])

                # The next start finds terminal records and resumes nothing.
                host_module._ACTIVE = None
                restarted = await start_runtime(str(home), cwd=str(workspace), listener=Listener(), session_options=options_for, startup_timeout_seconds=20)
                try:
                    await asyncio.sleep(1.0)
                    self.assertEqual((await core.load_child_agent("child-stop"))["status"], "stopped")
                    self.assertEqual((await core.read_task_meta(task_id))["stopReason"], "runtime_stopped")
                    self.assertFalse(is_process_alive(pid))
                    listed = await restarted.daemon["listTasks"](handle.id)
                    self.assertEqual([t.get("status") for t in listed if t.get("id") == task_id or t.get("taskId") == task_id], ["stopped"])
                finally:
                    await restarted.stop()

            run(scenario())
        finally:
            self.restore_home(root, previous)

    def test_a_killed_host_loses_its_daemon_by_the_dead_man_switch_and_the_next_start_reconciles_once(self):
        root, home, workspace, previous = self.temp_context()
        host = None
        try:
            env = {**os.environ, "TESS_CORE_HOME": str(home), "TESS_CORE_DAEMON_OWNER_POLL_MS": "300",
                   "PYTHONPATH": str(Path(core.__file__).resolve().parents[1])}
            host = subprocess.Popen([sys.executable, "-c", HOST_SCRIPT, str(home)], cwd=str(workspace), env=env, text=True,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            started = json.loads(host.stdout.readline())
            self.assertTrue(is_process_alive(started["daemon_pid"]))
            self.assertIsInstance(started["worker_pid"], int)
            self.assertTrue(is_process_alive(started["worker_pid"]))

            os.kill(host.pid, signal.SIGKILL)   # no stop ran
            host.wait(timeout=10)

            async def scenario():
                await _wait(lambda: not is_process_alive(started["daemon_pid"]), timeout=30, label="the daemon's dead-man exit")
                await _wait(lambda: not is_process_alive(started["worker_pid"]), timeout=10, label="the worker's exit")
                meta = await core.read_task_meta(started["task_id"])
                self.assertEqual(meta["stopReason"], "runtime_stopped")
                self.assertIn(meta["status"], {"stopped", "terminated"})
                record_before = await core.load_task_record(started["task_id"])
                self.assertIn(record_before["status"], {"stopped", "terminated"})
                events_before = [e["type"] for e in await read_task_events(started["task_id"])]
                self.assertEqual(events_before.count("task.runtime_stopped"), 1)

                lease = await read_owner_lease(str(home))
                self.assertEqual(lease["pid"], started["pid"])   # the dead host's lease is still there

                host_module._ACTIVE = None
                runtime = await start_runtime(str(home), cwd=str(workspace), listener=Listener(), startup_timeout_seconds=30)
                try:
                    reclaimed = await read_owner_lease(str(home))
                    self.assertEqual((reclaimed["pid"], reclaimed["reclaimedFrom"]["pid"]), (os.getpid(), started["pid"]))
                    await asyncio.sleep(1.5)   # the boot sweep has run
                    record_after = await core.load_task_record(started["task_id"])
                    self.assertEqual((record_after["status"], record_after["updatedAt"]), (record_before["status"], record_before["updatedAt"]))
                    events_after = [e["type"] for e in await read_task_events(started["task_id"])]
                    self.assertEqual(events_after.count("task.runtime_stopped"), 1)
                    self.assertEqual((await core.read_task_meta(started["task_id"]))["stopReason"], "runtime_stopped")
                    self.assertFalse(is_process_alive(started["worker_pid"]))
                finally:
                    await runtime.stop()

            run(scenario())
        finally:
            if host is not None and host.poll() is None:
                host.kill()
            self.restore_home(root, previous)

    def test_published_results_are_not_redelivered_after_a_restart(self):
        root, home, workspace, previous = self.temp_context()
        try:
            os.environ["TESS_CORE_SESSIONS_IDLE_CLOSE_MS"], os.environ["TESS_CORE_DAEMON_MAILBOX_WATCH_MS"] = "300", "200"

            async def scenario():
                calls = {"first": 0, "second": 0}

                def provider(name):
                    async def send(_request):
                        calls[name] += 1
                        return {"model": "gpt-5.4", "stopReason": "end_turn", "usage": {}, "content": [{"type": "text", "text": "noted"}]}
                    return {"send": send}

                listener = Listener()
                first = await start_runtime(str(home), cwd=str(workspace), listener=listener, session_options=lambda sid, r: SessionOptions(provider_override=provider("first")), startup_timeout_seconds=20)
                parent = await first.sessions.open()
                await parent.run_turn("hello")
                live = first.sessions.live(parent.id)
                task_id = "task-published"
                await core.write_task_meta(task_id, {"type": "subagent", "taskId": task_id, "id": task_id, "sessionId": "child-p", "session_id": "child-p",
                                                     "parent_session_id": parent.id, "status": "completed", "cwd": str(workspace),
                                                     "startedAt": "2026-01-01T00:00:00Z", "updatedAt": "2026-01-01T00:00:00Z"})
                await save_task_record({"id": task_id, "sessionId": parent.id, "kind": "subagent", "status": "completed", "title": task_id,
                                        "createdAt": "2026-01-01T00:00:00Z", "updatedAt": "2026-01-01T00:00:00Z"})
                live.record["metadata"]["taskIds"] = [task_id]
                await core.persist_session(live.record, live.config)
                await send_task_message(task_id, {"direction": "to_parent", "content": "child result 7", "kind": "result", "parentSessionId": parent.id})
                await _wait(lambda: calls["first"] >= 2, timeout=20, label="the continuity turn delivered it")
                self.assertEqual({m["status"] for m in await read_task_mailbox_messages(task_id)}, {"delivered"})
                parent_id = parent.id
                await first.stop()

                host_module._ACTIVE = None
                listener = Listener()
                second = await start_runtime(str(home), cwd=str(workspace), listener=listener, session_options=lambda sid, r: SessionOptions(provider_override=provider("second")), startup_timeout_seconds=20)
                try:
                    await asyncio.sleep(2.5)   # several mailbox sweeps
                    self.assertEqual(calls["second"], 0)          # delivered once, never again
                    self.assertEqual(listener.opened, [])
                    self.assertEqual(second.status()["sessions"]["pending"], [])
                    stored = await core.load_session(parent_id)
                    self.assertEqual(json.dumps(stored["messages"]).count("child result 7"), 1)
                finally:
                    await second.stop()

            run(scenario())
        finally:
            self.restore_home(root, previous)
