from __future__ import annotations

import asyncio
import os
import time
import json
import math
import sys
from unittest.mock import patch
from pathlib import Path

from tests.support.agent_core_case import AgentCoreTestCase, core, run
from agent_core.tasks.supervisor_parts.monitor_watch import tick_daemon_monitors
from agent_core import MonitorDeliveryController, MonitorDeliveryError
from agent_core._utils import parse_iso_ms


def _progress_fixture_time(now, acknowledged_ms):
    # Persisted delivery scheduling uses integer milliseconds. Round the parsed
    # float boundary upward so progress remains strictly beyond acknowledgement.
    return max(now, math.ceil(acknowledged_ms)) + 20001


class MonitorDeliveryTests(AgentCoreTestCase):
    def setUp(self):
        super().setUp()
        self.root, self.home, self.workspace, self.previous = self.temp_context()
        self.addCleanup(self.restore_home, self.root, self.previous)
        self.session_id = "70000000-0000-4000-8000-000000000001"
        self.now = int(time.time() * 1000)
        self.controller = MonitorDeliveryController(str(self.home), clock=lambda: self.now)

    async def fixture(self, status="running", muted=False):
        monitor = core.create_monitor_record({"sessionId": self.session_id, "taskId": "watched", "goal": "inspect build"})
        monitor["muted"] = muted
        monitor["checkIntervalMs"] = 1
        await core.save_monitor(monitor)
        await self.task(status)
        return monitor

    async def task(self, status):
        await core.write_task_meta("watched", {"taskId": "watched", "sessionId": self.session_id, "status": status, "exitCode": 0 if status == "completed" else None, "pid": os.getpid()})

    async def tick(self):
        self.now += 10
        with patch("agent_core.tasks.supervisor_parts.monitor_watch.time.time", return_value=self.now / 1000):
            await tick_daemon_monitors(lambda *_: None)

    async def claim(self, monitor, owner="host"):
        return await self.controller.claim(monitor["id"], self.session_id, owner)

    def receipt(self, payload):
        return {"deliveryId": payload["deliveryId"], "seq": 1}

    def test_daemon_retains_terminal_intent_without_subscriber(self):
        async def scenario():
            monitor = await self.fixture("completed")
            await tick_daemon_monitors(lambda *_: None)
            saved = await core.load_monitor(monitor["id"])
            self.assertEqual(saved["status"], "completed")
            self.assertEqual(saved["delivery"]["queue"][0]["reason"], "completed")
            self.assertNotIn("lastAssistantUpdateAt", saved)
        run(scenario())

    def test_unmute_delivers_latest_terminal_once_and_stop_preserves_task(self):
        async def scenario():
            monitor = await self.fixture(muted=True)
            path = Path(core.task_output_path("watched"))
            path.write_text("initial")
            await self.tick()
            self.assertIsNone(await self.claim(monitor))
            path.write_text("initial progress")
            await self.tick()
            await self.task("completed")
            await self.tick()
            await self.controller.set_muted(monitor["id"], self.session_id, False)
            claim = await self.claim(monitor)
            self.assertEqual(claim["envelope"]["reason"], "completed")
            self.assertIn("progress", claim["envelope"]["outputExcerpt"])
            await self.controller.checkpoint(claim, "Completed.")
            await self.controller.publish(claim, self.receipt)
            self.assertEqual(await self.controller.list_pending(), [])
            await self.controller.stop(monitor["id"], self.session_id)
            self.assertEqual((await core.read_task_meta("watched"))["status"], "completed")
            self.assertEqual((await core.load_monitor(monitor["id"]))["delivery"]["lastReceipt"]["seq"], 1)
        run(scenario())

    def test_stop_and_mute_revoke_claims_without_publishing(self):
        async def scenario():
            monitor = await self.fixture("completed")
            await self.tick()
            claim = await self.claim(monitor)
            await self.controller.checkpoint(claim, "Done")
            await self.controller.set_muted(monitor["id"], self.session_id, True)
            with self.assertRaises(MonitorDeliveryError):
                await self.controller.publish(claim, lambda _: self.fail("muted publisher"))
            await self.controller.set_muted(monitor["id"], self.session_id, False)
            replacement = await self.claim(monitor)
            self.assertEqual(replacement["result"]["text"], "Done")
            await self.controller.stop(monitor["id"], self.session_id)
            with self.assertRaises(MonitorDeliveryError):
                await self.controller.publish(replacement, lambda _: self.fail("stopped publisher"))
            await self.tick()
            self.assertEqual(await self.controller.list_pending(), [])
            self.assertEqual((await core.load_monitor(monitor["id"]))["status"], "completed")
        run(scenario())

    def test_claim_race_expiry_fences_old_worker_and_observation_does_not(self):
        async def scenario():
            monitor = await self.fixture()
            await self.tick()
            ready = asyncio.Event()
            async def compete(owner):
                await ready.wait()
                return await self.claim(monitor, owner)
            tasks = [asyncio.create_task(compete(str(i))) for i in range(2)]
            ready.set()
            claims = await asyncio.gather(*tasks)
            claim = next(claim for claim in claims if claim)
            self.assertEqual(sum(claim is not None for claim in claims), 1)
            for _ in range(4):
                await self.tick()
            await self.controller.checkpoint(claim, "Watching")
            self.now += 60_001
            replacement = await self.claim(monitor, "replacement")
            self.assertEqual(replacement["result"]["text"], "Watching")
            with self.assertRaises(MonitorDeliveryError):
                await self.controller.publish(claim, self.receipt)
            await self.controller.publish(replacement, self.receipt)
        run(scenario())

    def test_crash_after_journal_append_replays_checkpoint_and_receipt(self):
        async def scenario():
            monitor = await self.fixture("completed")
            await self.tick()
            claim = await self.claim(monitor)
            await self.controller.checkpoint(claim, "Done")
            journal = {}
            def publish(payload):
                return journal.setdefault(payload["deliveryId"], self.receipt(payload))
            with patch("agent_core.tasks.monitor_transactions.save_monitor", side_effect=OSError("crash before ack")):
                with self.assertRaises(OSError):
                    await self.controller.publish(claim, publish)
            saved = await core.load_monitor(monitor["id"])
            self.assertNotIn("lastAssistantUpdateAt", saved)
            self.assertNotIn("lastReceipt", saved["delivery"])
            self.assertEqual(saved["delivery"]["queue"][0]["id"], claim["deliveryId"])
            self.assertEqual(len(journal), 1)  # published is not yet acknowledged
            self.now += 60_001
            restarted = MonitorDeliveryController(str(self.home), clock=lambda: self.now)
            recovered = await restarted.claim(monitor["id"], self.session_id, "restarted")
            self.assertEqual(recovered["deliveryId"], claim["deliveryId"])
            self.assertEqual(recovered["result"]["text"], "Done")
            receipt = await restarted.publish(recovered, publish)
            self.assertEqual(len(journal), 1)
            self.assertEqual(await restarted.list_pending(), [])
            saved = await core.load_monitor(monitor["id"])
            self.assertEqual(saved["delivery"]["lastReceipt"], receipt)
            self.assertEqual(saved["delivery"]["queue"], [])
        run(scenario())

    def test_receipt_acknowledgement_holds_the_lock_until_durable_save(self):
        async def scenario():
            monitor = await self.fixture("completed")
            await self.tick()
            claim = await self.claim(monitor)
            await self.controller.checkpoint(claim, "Done")
            before_ack, release_ack, stop_entered = asyncio.Event(), asyncio.Event(), asyncio.Event()
            save = core.save_monitor
            journal = {}

            def publisher(payload):
                return journal.setdefault(payload["deliveryId"], self.receipt(payload))

            async def save_with_barrier(record):
                if record["delivery"].get("lastReceipt"):
                    before_ack.set()
                    await release_ack.wait()
                await save(record)

            async def stop_after_entry():
                stop_entered.set()
                await self.controller.stop(monitor["id"], self.session_id)

            with patch("agent_core.tasks.monitor_transactions.save_monitor", save_with_barrier):
                publish = asyncio.create_task(self.controller.publish(claim, publisher))
                stop = None
                try:
                    await asyncio.wait_for(before_ack.wait(), timeout=5)
                    self.assertEqual(len(journal), 1)
                    saved = await core.load_monitor(monitor["id"])
                    self.assertNotIn("lastReceipt", saved["delivery"])
                    self.assertEqual(saved["delivery"]["queue"][0]["id"], claim["deliveryId"])
                    self.assertFalse(publish.done())
                    stop = asyncio.create_task(stop_after_entry())
                    await asyncio.wait_for(stop_entered.wait(), timeout=5)
                    self.assertFalse(stop.done())
                    release_ack.set()
                    receipt = await asyncio.wait_for(publish, timeout=5)
                    await asyncio.wait_for(stop, timeout=5)
                finally:
                    release_ack.set()
                    await asyncio.gather(publish, *([stop] if stop else []), return_exceptions=True)
            saved = await core.load_monitor(monitor["id"])
            self.assertEqual(saved["delivery"]["lastReceipt"], receipt)
            self.assertEqual(saved["delivery"]["queue"], [])
            # Stop sees the acknowledged terminal record, not a pending queue.
            # Its documented already-terminal behavior is a no-op.
            self.assertEqual(saved["status"], "completed")
            self.assertNotIn("deliveryStoppedAt", saved)
            self.assertEqual(len(journal), 1)
        run(scenario())

    def test_publication_and_stop_are_ordered_at_durable_callback(self):
        async def scenario():
            monitor = await self.fixture("completed")
            await self.tick()
            claim = await self.claim(monitor)
            await self.controller.checkpoint(claim, "Done")
            entered, release = asyncio.Event(), asyncio.Event()
            async def publisher(payload):
                entered.set()
                await release.wait()
                return self.receipt(payload)
            publish = asyncio.create_task(self.controller.publish(claim, publisher))
            await entered.wait()
            stop = asyncio.create_task(self.controller.stop(monitor["id"], self.session_id))
            await asyncio.sleep(0)
            self.assertFalse(stop.done())
            release.set()
            await asyncio.gather(publish, stop)
            self.assertEqual((await core.load_monitor(monitor["id"]))["delivery"]["lastReceipt"]["seq"], 1)
        run(scenario())

    def test_continuation_crash_is_uncertain_and_never_reruns_tools(self):
        async def scenario():
            monitor = await self.fixture("completed")
            monitor["delivery"]["mode"] = "continuation"
            await core.save_monitor(monitor)
            await self.tick()
            self.assertIsNone(await self.claim(monitor))
            claim = await self.controller.claim(monitor["id"], self.session_id, "host", continuation_supported=True)
            self.assertNotIn("token", claim["envelope"])
            await self.controller.begin_continuation(claim)
            self.now += 60_001
            self.assertIsNone(await self.controller.claim(monitor["id"], self.session_id, "new", continuation_supported=True))
            self.assertEqual((await self.controller.list_pending())[0]["error"], "monitor.execution_uncertain")
            with self.assertRaisesRegex(RuntimeError, "execution_uncertain"):
                await self.controller.retry_failed(monitor["id"], self.session_id)
        run(scenario())

    def test_no_visible_progress_advances_processed_only_and_retries_are_bounded(self):
        async def scenario():
            monitor = await self.fixture()
            path = Path(core.task_output_path("watched"))
            path.write_bytes(b"x" * 10000)
            await self.tick()
            claim = await self.claim(monitor)
            self.assertEqual(len(claim["envelope"]["outputExcerpt"]), 8192)
            await self.controller.checkpoint(claim, None, rationale="heartbeat noise")
            await self.controller.publish(claim, lambda _: self.fail("no message expected"))
            state = (await core.load_monitor(monitor["id"]))["delivery"]
            self.assertEqual(state["processedBytes"], 10000)
            self.assertEqual(state["publishedBytes"], 0)
            await self.task("completed")
            await self.tick()
            for i in range(3):
                claim = await self.claim(monitor)
                self.assertIsNotNone(claim)
                await self.controller.retry(claim, "provider_timeout")
                self.now += 60001
            self.assertIsNone(await self.claim(monitor))
            self.assertEqual((await self.controller.list_pending())[0]["state"], "failed")
            await self.controller.retry_failed(monitor["id"], self.session_id)
            claim = await self.claim(monitor)
            self.assertIsNotNone(claim)
            await self.controller.checkpoint(claim, "Completed after storage recovered")
            await self.controller.publish(claim, self.receipt)
            self.assertEqual(await self.controller.list_pending(), [])
        run(scenario())

    def test_rotation_and_native_session_runtime_isolation(self):
        async def scenario():
            monitor = await self.fixture()
            path = Path(core.task_output_path("watched"))
            path.write_bytes(b"old" * 1000)
            await self.tick()
            claim = await self.claim(monitor)
            await self.controller.checkpoint(claim, "Initial")
            await self.controller.publish(claim, self.receipt)
            path.write_bytes(b"new")
            self.now += 20001
            await self.tick()
            claim = await self.claim(monitor)
            self.assertEqual(claim["envelope"]["outputGeneration"], 1)
            self.assertEqual(claim["envelope"]["outputExcerpt"], "new")
            other = MonitorDeliveryController(str(self.workspace))
            self.assertEqual(await other.list_pending(), [])
            with self.assertRaises(Exception):
                await self.controller.claim(monitor["id"], "other-session", "foreign")
        run(scenario())

    def test_explicit_mode_rejects_unsupported_host_without_registration(self):
        async def scenario():
            await self.task("running")
            tools = {tool.name: tool for tool in core.create_builtin_tools()}
            context = {"session": {"id": self.session_id, "metadata": {"taskIds": ["watched"]}}, "config": {}}
            with self.assertRaisesRegex(RuntimeError, "continuation_unsupported"):
                await tools["TaskMonitorStart"].handler({"task_id": "watched", "goal": "inspect", "delivery_mode": "continuation"}, context)
            self.assertEqual(await core.list_monitors(), [])
        run(scenario())

    def test_failed_progress_cannot_strand_terminal(self):
        async def scenario():
            monitor = await self.fixture()
            await self.tick()
            claim = await self.claim(monitor)
            await self.controller.checkpoint(claim, "Started")
            for attempt in range(3):
                await self.controller.retry(claim, "journal_unavailable")
                self.now += 60001
                if attempt < 2:
                    claim = await self.claim(monitor)
            self.assertIsNone(await self.claim(monitor))
            await self.task("completed")
            await self.tick()
            terminal = await self.claim(monitor)
            self.assertEqual(terminal["envelope"]["reason"], "completed")
            await self.controller.checkpoint(terminal, "Done")
            await self.controller.publish(terminal, self.receipt)
            self.assertEqual(await self.controller.list_pending(), [])
        run(scenario())

    def test_pagination_and_old_terminal_adoption_policy(self):
        async def scenario():
            first = await self.fixture("completed")
            second = {**first, "id": "second-monitor"}
            await core.save_monitor(second)
            old = {**first, "id": "old-terminal", "status": "completed"}
            old.pop("delivery")
            await core.save_monitor(old)
            await self.tick()
            page1 = await self.controller.list_pending(limit=1)
            page2 = await self.controller.list_pending(limit=1, after=page1[-1]["cursor"])
            self.assertEqual(len(page2), 1)
            self.assertNotEqual(page1[0]["deliveryId"], page2[0]["deliveryId"])
            self.assertEqual(await self.controller.list_pending(after=page2[-1]["cursor"]), [])
            self.assertNotIn("delivery", await core.load_monitor(old["id"]))
        run(scenario())

    def test_two_processes_compete_for_one_claim_at_a_barrier(self):
        async def scenario():
            monitor = await self.fixture("completed")
            await self.tick()
            barrier = self.workspace / "go"
            code = '''import asyncio, json, sys
from pathlib import Path
from agent_core import MonitorDeliveryController
async def main():
    home, monitor, session, ready, barrier = sys.argv[1:]
    Path(ready).write_text('ready')
    async with asyncio.timeout(5):
        while not Path(barrier).exists():
            await asyncio.sleep(.01)
    claim = await MonitorDeliveryController(home).claim(monitor, session, ready)
    print(json.dumps({'claimed': claim is not None}))
asyncio.run(main())
'''
            processes = []
            for index in range(2):
                processes.append(await asyncio.create_subprocess_exec(sys.executable, "-c", code,
                    str(self.home), monitor["id"], self.session_id, str(self.workspace / str(index)), str(barrier),
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE))
            try:
                async with asyncio.timeout(5):
                    while not all((self.workspace / str(index)).exists() for index in range(2)):
                        await asyncio.sleep(.01)
                barrier.write_text("go")
                results = await asyncio.gather(*(process.communicate() for process in processes))
                self.assertTrue(all(process.returncode == 0 for process in processes), results)
                self.assertEqual(sum(json.loads(stdout)["claimed"] for stdout, _ in results), 1)
            finally:
                for process in processes:
                    if process.returncode is None:
                        process.kill()
                        await process.wait()
        run(scenario())

    def test_muted_terminal_intent_survives(self):
        async def scenario():
            monitor = await self.fixture("completed", muted=True)
            await tick_daemon_monitors(lambda *_: None)
            saved = await core.load_monitor(monitor["id"])
            self.assertTrue(saved["muted"])
            self.assertEqual(saved["status"], "completed")
            self.assertEqual(saved["delivery"]["queue"][0]["reason"], "completed")
        run(scenario())

    def test_progress_clock_ack_boundary_stays_integer(self):
        for now, acknowledged_ms, expected in ((1000, 1001.25, 21003), (1003, 1001.25, 21004)):
            with self.subTest(now=now, acknowledged_ms=acknowledged_ms):
                advanced = _progress_fixture_time(now, acknowledged_ms)
                self.assertIs(type(advanced), int)
                self.assertEqual(advanced, expected)
                self.assertGreaterEqual(advanced, acknowledged_ms + 20001)

    def test_mute_preserves_superseded_execution_and_checkpoint_history(self):
        async def scenario(phase, completion_before_mute):
            monitor = await self.fixture()
            Path(core.task_output_path("watched")).write_text("")
            monitor["delivery"]["mode"] = "continuation"
            await core.save_monitor(monitor)
            await self.tick()
            initial = await self.controller.claim(monitor["id"], self.session_id, "initial", continuation_supported=True)
            await self.controller.checkpoint(initial, "Watching for progress")
            await self.controller.publish(initial, self.receipt)
            # Publication acknowledges at real wall time; anchor the fixture
            # clock to that persisted boundary before testing progress spacing.
            acknowledged = await core.load_monitor(monitor["id"])
            acknowledged_ms = parse_iso_ms(acknowledged["delivery"]["lastProcessedAt"])
            self.assertIsNotNone(acknowledged_ms)
            Path(core.task_output_path("watched")).write_text("progress\n" * 1024)
            self.now = _progress_fixture_time(self.now, acknowledged_ms)
            self.assertIs(type(self.now), int)
            await self.tick()
            first = await self.controller.claim(monitor["id"], self.session_id, "first", continuation_supported=True)
            self.assertEqual(first["envelope"]["reason"], "progress")
            await self.controller.begin_continuation(first)
            if phase == "authored":
                await self.controller.checkpoint(first, "Earlier inspection checkpoint")
            elif phase == "uncertain":
                await self.controller.retry(first, "interrupted")
                retried = await core.load_monitor(monitor["id"])
                self.assertIsNotNone(retried)
                self.assertIs(type(retried["delivery"]["queue"][0]["availableAt"]), int)
            if completion_before_mute:
                await self.task("completed")
                await self.tick()
                self.assertEqual(len((await core.load_monitor(monitor["id"]))["delivery"]["queue"]), 2)
            await self.controller.set_muted(monitor["id"], self.session_id, True)
            if not completion_before_mute:
                await self.task("completed")
                await self.tick()
            held = await core.load_monitor(monitor["id"])
            self.assertEqual(len(held["delivery"]["queue"]), 1)
            self.assertNotIn("result", held["delivery"]["queue"][0])  # no new prose while muted
            history = held["delivery"]["interruption"]["deliveries"]
            self.assertEqual(history[0]["id"], first["deliveryId"])
            self.assertTrue(history[0]["executionStarted"])
            self.assertNotIn("token", history[0])
            if phase == "authored":
                self.assertEqual(history[0]["result"]["text"], "Earlier inspection checkpoint")
            else:
                self.assertTrue(held["delivery"]["interruption"]["uncertain"])
            recovered = MonitorDeliveryController(str(self.home), clock=lambda: self.now)
            self.assertIsNone(await recovered.claim(monitor["id"], self.session_id, "muted", continuation_supported=True))
            await recovered.set_muted(monitor["id"], self.session_id, False)
            claim = await recovered.claim(monitor["id"], self.session_id, "recovered", continuation_supported=True)
            self.assertEqual(claim["envelope"]["reason"], "completed")
            self.assertIn("completed", claim["result"]["text"])
            self.assertIn("paused", claim["result"]["text"])
            if phase != "authored":
                self.assertIn("interrupted", claim["result"]["text"])
            with self.assertRaisesRegex(MonitorDeliveryError, "continuation_interrupted"):
                await recovered.begin_continuation(claim)
            with self.assertRaises(MonitorDeliveryError):
                await recovered.checkpoint(first, "Late stale author")
            journal = []
            def publish(payload):
                journal.append(payload)
                return self.receipt(payload)
            await recovered.publish(claim, publish)
            self.assertEqual(len(journal), 1)
            self.assertIsNone(await recovered.claim(monitor["id"], self.session_id, "again", continuation_supported=True))
            self.assertEqual((await core.load_monitor(monitor["id"]))["delivery"]["interruption"]["deliveries"], history)
        for phase in ("executing", "authored", "uncertain"):
            for completion_before_mute in (True, False):
                with self.subTest(phase=phase, completion_before_mute=completion_before_mute):
                    run(scenario(phase, completion_before_mute))

    def test_interruption_survives_publication_and_blocks_later_terminal_actions(self):
        async def scenario():
            monitor = await self.fixture()
            monitor["delivery"]["mode"] = "continuation"
            await core.save_monitor(monitor)
            await self.tick()
            first = await self.controller.claim(monitor["id"], self.session_id, "host", continuation_supported=True)
            await self.controller.begin_continuation(first)
            await self.controller.set_muted(monitor["id"], self.session_id, True)
            await self.controller.set_muted(monitor["id"], self.session_id, False)
            recovery = await self.controller.claim(monitor["id"], self.session_id, "host", continuation_supported=True)
            await self.controller.publish(recovery, self.receipt)
            await self.task("completed")
            await self.tick()
            terminal = await self.controller.claim(monitor["id"], self.session_id, "host", continuation_supported=True)
            self.assertIn("completed", terminal["result"]["text"])
            self.assertIn("interrupted", terminal["result"]["text"])
            self.assertTrue((await self.controller.list_pending())[0]["continuationBlocked"])
            with self.assertRaisesRegex(MonitorDeliveryError, "continuation_interrupted"):
                await self.controller.begin_continuation(terminal)
            await self.controller.publish(terminal, self.receipt)
            saved = await core.load_monitor(monitor["id"])
            self.assertEqual(len(saved["delivery"]["interruption"]["deliveries"]), 1)
            self.assertEqual(saved["delivery"]["interruption"]["deliveries"][0]["id"], first["deliveryId"])
        run(scenario())
