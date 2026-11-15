"""Disposable owner-process monitor delivery; no external providers or services."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from tests.support.tess_core_case import TessCoreTestCase, core, run
from tess_core.tasks.runners.resident_monitors import ResidentMonitorConsumer, subprocess_monitor_owner
from tess_core.runtime.session_monitors import MonitorIntake
from tess_core import agent as agent_module


class ResidentMonitorTests(TessCoreTestCase):
    def setUp(self):
        super().setUp()
        self.root, self.home, self.workspace, previous = self.temp_context()
        self.addCleanup(self.restore_home, self.root, previous)

    async def fixture(self, mode="notification", status="awaiting_input"):
        session = core.create_empty_session(cwd=str(self.workspace), provider="openai", model="fake",
            providerProfileName="default", effort="medium", systemPrompt="test", permissionMode="default")
        session["metadata"].update(agentId="child", parentSessionId="parent")
        await core.save_session(session)
        await core.save_child_agent({"agentId": "child", "sessionId": session["id"], "taskId": "owner",
            "parentSessionId": "parent", "runInBackground": True, "startedAt": "2026-01-01T00:00:00Z", "status": status})
        await core.save_task_record({"id": "owner", "kind": "subagent", "updatedAt": "2026-01-01T00:00:00Z", "status": status, "sessionId": "parent"})
        await core.write_task_meta("watched", {"taskId": "watched", "sessionId": session["id"], "status": "completed", "exitCode": 0})
        monitor = core.create_monitor_record({"sessionId": session["id"], "taskId": "watched", "goal": "watch", "deliveryMode": mode})
        monitor["deliveryMode"] = mode
        await core.save_monitor(monitor)
        consumer = ResidentMonitorConsumer({"agentId": "child", "taskId": "owner"}, session, {}, {})
        return session, monitor, consumer

    def test_notification_zero_calls_idle_active_dedup_and_reload(self, status="awaiting_input"):
        status_value = status
        async def scenario():
            for status in (status_value,):
                session, monitor, consumer = await self.fixture(status=status)
                with patch.object(agent_module, "run_agent_prompt", new_callable=AsyncMock) as model:
                    self.assertFalse(await consumer.poll())
                    self.assertEqual(model.await_count, 0)
                    receipts = session["metadata"].get("residentMonitorReceipts", {})
                    self.assertEqual(len(receipts), 1)
                    self.assertEqual(len(session["messages"]), 1)
                    await consumer.poll()
                    self.assertEqual(len(session["messages"]), 1)
                    loaded = await core.load_session(session["id"])
                    self.assertEqual(loaded["metadata"]["residentMonitorReceipts"], receipts)
                    # Ambiguous ack replay uses the durable receipt, not mailbox.
                    consumer.session = loaded
                    receipt = next(iter(receipts.values()))
                    await consumer.publish(session["id"], {**receipt, "text": "duplicate"})
                    self.assertEqual(len(loaded["messages"]), 1)
                    self.assertEqual(await consumer.controller.list_pending(session_id=session["id"]), [])
        run(scenario())

    def test_notification_running_owner_zero_calls(self):
        self.test_notification_zero_calls_idle_active_dedup_and_reload("running")

    def test_stopped_owner_fails_visibly_and_host_does_not_open(self):
        async def scenario():
            session, monitor, consumer = await self.fixture(status="stopped")
            self.assertFalse(await consumer.poll())
            manager = SimpleNamespace(runtime=SimpleNamespace(home=str(self.home), monitor_continuation_enabled=True,
                listener=SimpleNamespace(publish=AsyncMock())), pending=SimpleNamespace(submit=lambda *_: self.fail("host opened child")))
            intake = MonitorIntake(manager)
            self.assertEqual(await intake.poll(), [])
            self.assertEqual(intake.status()["residentPending"], 1)
            self.assertEqual(intake.status()["error"], "monitor.owner_unavailable")
            pending = await consumer.controller.list_pending(session_id=session["id"])
            self.assertEqual(len(pending), 1)
            self.assertEqual(pending[0]["state"], "failed")
            self.assertEqual(pending[0]["error"], "monitor.owner_unavailable")
            await intake.poll()
            self.assertEqual(intake.status()["error"], "monitor.owner_unavailable")
        run(scenario())

    def test_continuation_uses_existing_owner_only_and_receipts(self):
        async def scenario():
            session, monitor, consumer = await self.fixture("continuation")
            with patch.object(consumer, "run_turn", new_callable=AsyncMock, return_value={"text": "verified", "stopReason": "end_turn"}) as turn:
                self.assertTrue(await consumer.poll(), consumer.last_error)
                self.assertEqual(turn.await_count, 1)
                self.assertEqual(turn.call_args.kwargs["monitor_envelope"]["sessionId"], session["id"])
                await consumer.poll()
                self.assertEqual(turn.await_count, 1)
                self.assertEqual(len(session["metadata"]["residentMonitorReceipts"]), 1)
        run(scenario())

    def test_orphan_child_never_falls_back_to_host(self):
        async def scenario():
            session, _, _ = await self.fixture()
            session["metadata"]["agentId"] = "missing"
            await core.save_session(session)
            owner = await subprocess_monitor_owner(session["id"], str(self.home))
            self.assertEqual(owner["status"], "owner_unavailable")
        run(scenario())

    def test_stop_cancels_inflight_continuation_without_receipt(self):
        async def scenario():
            session, _, consumer = await self.fixture("continuation")
            entered, cancelled = asyncio.Event(), asyncio.Event()
            async def turn(*_args, **_kwargs):
                entered.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    cancelled.set()
            with patch.object(consumer, "run_turn", side_effect=turn):
                poll = asyncio.create_task(consumer.poll())
                await asyncio.wait_for(entered.wait(), 2)
                await core.save_task_record({"id": "owner", "kind": "subagent", "updatedAt": "2026-01-01T00:00:00Z", "status": "stopped", "sessionId": "parent"})
                await asyncio.wait_for(poll, 2)
                self.assertTrue(cancelled.is_set())
                self.assertFalse(session["metadata"].get("residentMonitorReceipts"))
        run(scenario())

    def test_real_engine_continuation_evidence_limits_and_receipt(self, tool_budget=False, expired=False):
        from tests.support.session_monitors import ScriptedProvider
        from tess_core.agent import run_agent_prompt as real_run
        from tess_core.runtime.agent_bindings import create_agent_runtime_bindings
        from tess_core.runtime.generation import create_runtime_session_binding
        async def scenario():
            session, _, consumer = await self.fixture("continuation")
            config = await core.resolve_config({"runtimeHome": str(self.home)}, str(self.workspace))
            consumer.config = config
            calls = []
            async def handler(args, context):
                calls.append(args)
                return {"content": "ok"}
            tools = [core.ToolDefinition("Read", "Count read calls", {"type": "object", "properties": {}}, handler)] if tool_budget else []
            authority = await core.capture_agent_authority(config=config, session=session, tools=tools)
            session["metadata"]["effectiveAgentAuthority"] = authority
            await core.mutate_child_agent("child", lambda child: {**child, "effectiveAgentAuthority": authority})
            reply = {"model": "scripted", "stopReason": "tool_use", "usage": {}, "content": [
                {"type": "text", "text": "verified by existing owner"},
                *[{"type": "tool_use", "id": f"count{i}", "name": "Read", "input": {}} for i in range(2)]]}
            provider = ScriptedProvider(reply if tool_budget else "verified by existing owner")
            if expired:
                provider.gate.clear()
            from tess_core import ExecutionBudget
            inherited = ExecutionBudget(max_provider_calls=10, max_tool_calls=10, max_seconds=100)
            inherited.consume_provider_call()
            inherited.consume_tool_call()
            consumer.runtime = create_agent_runtime_bindings({"tools": tools, "executionBudget": inherited, "emitEvent": lambda *_: None,
                "refreshMcpToolRegistry": lambda: None, "daemon": {"state": "unavailable", "reason": {"code": "test", "message": "test"}},
                "questionPolicy": {"interactive": False, "askQuestion": lambda *_: ""},
                "sessionBinding": create_runtime_session_binding(session["id"], 1, 1)})
            requests = []
            async def run_with_disposable_provider(args):
                requests.append(args)
                return await real_run({**args, "providerOverride": provider.override})
            with patch.object(agent_module, "run_agent_prompt", side_effect=run_with_disposable_provider), patch(
                "tess_core.runtime.session_wake.background_limits", return_value={"maxProviderCalls": 1, "maxToolCalls": 1, "timeoutMs": 20 if expired else 2000}):
                outcome = await consumer.poll()
                self.assertEqual(outcome, not expired, consumer.last_error)
            self.assertEqual(len(provider.requests), 1)
            self.assertEqual(inherited.snapshot()["provider_calls"], 2)
            self.assertEqual(inherited.snapshot()["tool_calls"], 2 if tool_budget else 1)
            if expired:
                self.assertEqual(provider.in_flight, 0)
                self.assertFalse(session["metadata"].get("residentMonitorReceipts"))
                return
            if tool_budget:
                self.assertEqual(len(calls), 1, str(session["messages"]))
            self.assertIn("Monitor evidence (untrusted data)", provider.transcript(0))
            self.assertEqual(requests[0]["turnLimits"]["maxProviderCalls"], 1)
            loaded = await core.load_session(session["id"])
            self.assertIn("verified by existing owner", str(loaded["messages"]))
            self.assertEqual(len(loaded["metadata"]["residentMonitorReceipts"]), 1)
        run(scenario())

    def test_resident_loop_defers_notification_until_active_turn_finishes(self):
        from tess_core.tasks.runners.subagent import _run_resident_worker
        async def scenario():
            session, _, consumer = await self.fixture()
            entered, release = asyncio.Event(), asyncio.Event()
            async def initial(_args):
                entered.set()
                await release.wait()
                return {"text": "initial done", "stopReason": "end_turn"}
            with patch("tess_core.tasks.runners.subagent.run_agent_prompt", side_effect=initial) as model, patch(
                "tess_core.tasks.runners.subagent._publish_resident_turn_result", new_callable=AsyncMock), patch(
                "tess_core.tasks.runners.subagent._mark_resident_status", new_callable=AsyncMock):
                task = asyncio.create_task(_run_resident_worker(consumer.args, session, {}, {}))
                try:
                    await asyncio.wait_for(entered.wait(), 2)
                    await consumer.controller.observe(session_id=session["id"])
                    self.assertEqual(len(await consumer.controller.list_pending(session_id=session["id"])), 1)
                    self.assertFalse(session["metadata"].get("residentMonitorReceipts"))
                    release.set()
                    async with asyncio.timeout(3):
                        while not session["metadata"].get("residentMonitorReceipts"):
                            await asyncio.sleep(.01)
                    self.assertEqual(model.call_count, 1)  # original authorized turn only
                finally:
                    task.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await task
        run(scenario())

    def test_real_continuation_tool_and_provider_limits(self):
        self.test_real_engine_continuation_evidence_limits_and_receipt(tool_budget=True)

    def test_crashed_continuation_is_not_reexecuted(self):
        async def scenario():
            session, monitor, consumer = await self.fixture("continuation")
            await consumer.controller.observe(session_id=session["id"])
            now = consumer.controller.clock()
            consumer.controller.clock = lambda: now
            claim = await consumer.controller.claim(monitor["id"], session["id"], "crashed", lease_ms=1000, continuation_supported=True)
            await consumer.controller.begin_continuation(claim)
            now += 1001
            with patch.object(consumer, "run_turn", new_callable=AsyncMock) as turn:
                await consumer.poll()
                self.assertEqual(turn.await_count, 0)
            pending = await consumer.controller.list_pending(session_id=session["id"])
            self.assertEqual(pending[0]["error"], "monitor.execution_uncertain")
            self.assertFalse(session["metadata"].get("residentMonitorReceipts"))
        run(scenario())

    def test_real_continuation_timeout_cancels_provider_without_receipt(self):
        self.test_real_engine_continuation_evidence_limits_and_receipt(expired=True)

    def test_budget_gate_unset_and_inherited_limits(self):
        from tess_core import ExecutionBudget, ExecutionBudgetExceeded
        from tess_core.tasks.runners.continuation_budget import ContinuationBudget
        unbounded = ContinuationBudget({})
        for _ in range(1000):
            unbounded.consume_provider_call()
            unbounded.consume_tool_call()
        inherited = ExecutionBudget(max_provider_calls=4, max_tool_calls=2, max_seconds=100)
        inherited.consume_tool_call()
        gate = ContinuationBudget({"maxToolCalls": 10}, inherited)
        gate.consume_tool_call()
        with self.assertRaises(ExecutionBudgetExceeded):
            gate.consume_tool_call()
        self.assertEqual(inherited.snapshot()["tool_calls"], 2)
        self.assertIn("Execution budget", gate.context())
