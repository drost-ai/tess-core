from __future__ import annotations

from tests.support.agent_core_case import *


class RuntimeGenerationSafetyTests(AgentCoreTestCase):
    def test_delayed_generation_a_work_cannot_commit_after_b_to_c_switch(self):
        root, home, workspace, previous = self.temp_context()

        async def scenario():
            config = await core.resolveConfig(
                {
                    "provider": "openai",
                    "runtimeHome": str(home),
                    "permissionMode": "default",
                },
                str(workspace),
            )
            sessions = {
                name: core.createEmptySession(
                    cwd=str(workspace),
                    provider=config["provider"],
                    providerProfileName=config["providerProfileName"],
                    model=config["model"],
                    effort=config["effort"],
                    systemPrompt=config["systemPrompt"],
                    permissionMode="default",
                    sessionId=name,
                )
                for name in ("a", "b", "c")
            }
            entered = asyncio.Event()
            release = asyncio.Event()
            events = []

            async def emit(event, data):
                events.append((event, data))
                if event == "subagent.status_changed" and data.get("session_id") == "a":
                    entered.set()
                    await release.wait()
                    raise RuntimeError("delayed generation-A consumer failure")

            runtime = await core.createCollaborationRuntime(
                {
                    "session": sessions["a"],
                    "config": config,
                    "runtimeBinding": core.createRuntimeSessionBinding("a", 1, 1),
                    "startTicker": False,
                    "emit": emit,
                }
            )
            try:
                sessions["a"]["metadata"]["childAgentIds"] = ["agent-a"]
                await core.saveChildAgent(_child_agent("agent-a", "a"))
                delayed_a = asyncio.create_task(runtime["refresh"]())
                await asyncio.wait_for(entered.wait(), 2)

                await runtime["activateSession"](
                    sessions["b"], config, core.createRuntimeSessionBinding("b", 2, 2)
                )
                await runtime["activateSession"](
                    sessions["c"], config, core.createRuntimeSessionBinding("c", 3, 3)
                )
                release.set()
                await delayed_a

                sessions["c"]["metadata"]["childAgentIds"] = ["agent-c"]
                await core.saveChildAgent(_child_agent("agent-c", "c"))
                await runtime["refresh"]()
                self.assertEqual(
                    [worker["id"] for worker in runtime["getSnapshot"]()["workers"]],
                    ["agent-c"],
                )
                self.assertFalse(any(data.get("session_id") == "b" for _, data in events))
                self.assertTrue(
                    any(
                        event == "runtime.stale_work_discarded"
                        and data.get("stale_generation") == 1
                        and data.get("source_event") == "collaboration.tick_failed"
                        for event, data in events
                    )
                )
            finally:
                release.set()
                await runtime["close"]()

        try:
            run(scenario())
        finally:
            self.restore_home(root, previous)

    def test_bootstrap_installs_only_latest_prepared_generation(self):
        root, home, workspace, previous = self.temp_context()

        async def scenario():
            workspaces = {}
            for name in ("a", "b", "c"):
                path = Path(root.name) / name
                path.mkdir()
                workspaces[name] = path
            config_a = await core.resolveConfig(
                {
                    "provider": "openai",
                    "runtimeHome": str(home),
                    "permissionMode": "default",
                },
                str(workspaces["a"]),
            )
            configs = {
                name: {**config_a, "cwd": str(workspaces[name]), "allowedRoots": [str(workspaces[name])]}
                for name in ("a", "b", "c")
            }
            sessions = {
                name: core.createEmptySession(
                    cwd=str(workspaces[name]),
                    provider=config_a["provider"],
                    providerProfileName=config_a["providerProfileName"],
                    model=config_a["model"],
                    effort=config_a["effort"],
                    systemPrompt=config_a["systemPrompt"],
                    permissionMode="default",
                    sessionId=name,
                )
                for name in ("a", "b", "c")
            }
            daemon = _DaemonHarness("b")
            runtime = await core.bootstrapRuntime(
                {"session": sessions["a"], "config": configs["a"], "daemonClient": daemon.client}
            )
            observed = []
            runtime["subscribeEvents"](observed.append)
            try:
                activating_b = runtime["setActiveSession"](sessions["b"], configs["b"])
                await daemon.subscribed.wait()
                activating_c = runtime["setActiveSession"](sessions["c"], configs["c"])
                context_c = await activating_c
                daemon.release.set()
                with self.assertRaises(core.RuntimeActivationSupersededError):
                    await activating_b

                self.assertEqual(context_c["binding"]["sessionId"], "c")
                self.assertEqual(context_c["session"]["id"], "c")
                self.assertEqual(context_c["retiredGeneration"]["binding"]["sessionId"], "a")
                self.assertEqual(context_c["retiredGeneration"]["status"], "drained")
                self.assertEqual(context_c["workspace"].getSnapshot()["cwd"], str(workspaces["c"]))
                self.assertEqual(runtime["getState"]()["session"]["activeSessionId"], "c")
                self.assertEqual(runtime["workspace"].getSnapshot()["cwd"], str(workspaces["c"]))
                self.assertEqual(daemon.active_session_ids(), ["c"])
                with self.assertRaises(TypeError):
                    context_c["session"]["id"] = "mutated"

                daemon.deliver_old("a", "turn.started", {"prompt": "stale-a"})
                daemon.deliver_old("b", "turn.started", {"prompt": "stale-b"})
                daemon.deliver("c", "turn.started", {"prompt": "current-c"})
                self.assertEqual(runtime["getState"]()["turn"]["prompt"], "current-c")
                self.assertEqual(
                    len([entry for entry in observed if entry["event"] == "turn.started"]), 1
                )
                self.assertTrue(
                    any(entry["event"] == "runtime.stale_work_discarded" for entry in observed)
                )

                failed_config = {**configs["c"], "pluginDirs": [str(Path(root.name) / "missing-plugin")]}
                failed_session = core.createEmptySession(
                    cwd=str(workspaces["c"]),
                    provider=config_a["provider"],
                    providerProfileName=config_a["providerProfileName"],
                    model=config_a["model"],
                    effort=config_a["effort"],
                    systemPrompt=config_a["systemPrompt"],
                    permissionMode="default",
                    sessionId="failed",
                )
                with self.assertRaises(Exception):
                    await runtime["setActiveSession"](failed_session, failed_config)
                runtime["startTurn"]({"prompt": "still-c"})
                self.assertEqual(runtime["getState"]()["session"]["activeSessionId"], "c")
                self.assertEqual(runtime["getState"]()["turn"]["prompt"], "still-c")
                self.assertEqual(daemon.active_session_ids(), ["c"])
            finally:
                daemon.release.set()
                await asyncio.gather(runtime["close"](), runtime["close"]())
            with self.assertRaises(core.LifecycleClosedError):
                runtime["setActiveSession"](sessions["a"], configs["a"])

        try:
            run(scenario())
        finally:
            self.restore_home(root, previous)


class _DaemonHarness:
    def __init__(self, delayed_session_id):
        self.delayed_session_id = delayed_session_id
        self.listeners = {}
        self.active = {}
        self.subscribed = asyncio.Event()
        self.release = asyncio.Event()
        self.client = {
            "subscribe": self.subscribe,
            "getStatus": self.get_status,
            "close": self.close,
        }

    async def subscribe(self, session_id, listener):
        self.listeners.setdefault(session_id, []).append(listener)
        if session_id == self.delayed_session_id:
            self.subscribed.set()
            await self.release.wait()
        self.active[session_id] = listener

        def unsubscribe():
            if self.active.get(session_id) is listener:
                self.active.pop(session_id, None)

        return unsubscribe

    async def get_status(self):
        return None

    async def close(self):
        return None

    def active_session_ids(self):
        return sorted(self.active)

    def deliver(self, session_id, event, data):
        listener = self.active.get(session_id)
        if listener:
            listener({"event": event, "data": data})

    def deliver_old(self, session_id, event, data):
        for listener in self.listeners.get(session_id, []):
            listener({"event": event, "data": data})


def _child_agent(agent_id, parent_session_id):
    return {
        "agentId": agent_id,
        "sessionId": f"child-{agent_id}",
        "parentSessionId": parent_session_id,
        "name": agent_id,
        "provider": "openai",
        "model": "test-model",
        "mode": "default",
        "runInBackground": True,
        "status": "backgrounded",
        "startedAt": "2026-01-01T00:00:00Z",
        "taskId": f"task-{agent_id}",
    }
