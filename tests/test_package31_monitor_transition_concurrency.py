from __future__ import annotations

from tests.support.agent_core_case import *
from agent_core._utils import now_iso


class Package31MonitorTransitionConcurrencyTests(AgentCoreTestCase):
    def _session(self, home: Path, workspace: Path):
        config = run(
            core.resolve_config(
                {
                    "provider": "openai",
                    "runtimeHome": str(home),
                    "permissionMode": "bypassPermissions",
                },
                str(workspace),
            )
        )
        session = core.create_empty_session(
            cwd=str(workspace),
            provider=config["provider"],
            providerProfileName=config["providerProfileName"],
            model=config["model"],
            effort=config["effort"],
            systemPrompt=config["systemPrompt"],
            permissionMode="bypassPermissions",
        )
        run(core.save_session(session))
        return config, session

    def _race(self, home, session_id, task_id, monitor_id, operations):
        ready = home / f"ready-{time.time_ns()}.log"
        go = home / f"go-{time.time_ns()}"
        events = home / f"events-{time.time_ns()}.jsonl"
        ready.write_text("", encoding="utf-8")
        events.write_text("", encoding="utf-8")
        repo = Path(__file__).parents[1]
        env = os.environ.copy()
        env["PYTHONPATH"] = (
            str(repo)
            if not env.get("PYTHONPATH")
            else f"{repo}{os.pathsep}{env['PYTHONPATH']}"
        )
        processes = []
        results = []
        for index, operation in enumerate(operations):
            result = home / f"result-{time.time_ns()}-{index}.json"
            results.append(result)
            processes.append(
                subprocess.Popen(
                    [
                        sys.executable,
                        "-m",
                        "tests.fixtures.monitor_transition_worker",
                        str(home),
                        session_id,
                        task_id,
                        monitor_id,
                        operation,
                        str(ready),
                        str(go),
                        str(result),
                        str(events),
                    ],
                    cwd=str(repo),
                    env=env,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
            )
        deadline = time.monotonic() + 10
        while len(ready.read_text(encoding="utf-8").splitlines()) < len(processes):
            if time.monotonic() >= deadline:
                self.fail("monitor workers did not reach barrier")
            time.sleep(0.01)
        go.write_text("go", encoding="utf-8")
        for process in processes:
            _stdout, stderr = process.communicate(timeout=15)
            self.assertEqual(process.returncode, 0, stderr)
        rows = [json.loads(path.read_text(encoding="utf-8")) for path in results]
        event_rows = [
            json.loads(line)
            for line in events.read_text(encoding="utf-8").splitlines()
            if line
        ]
        return rows, event_rows

    def test_cross_process_start_and_stop_are_exactly_once(self):
        root, home, workspace, previous = self.temp_context()
        try:
            _config, session = self._session(home, workspace)
            starts, start_events = self._race(
                home, session["id"], "task-start-race", "unused", ["start", "start"]
            )
            self.assertEqual(sorted(row["changed"] for row in starts), [False, True])
            self.assertEqual(len({row["monitorId"] for row in starts}), 1)
            self.assertEqual(len(start_events), 1)
            monitor_id = starts[0]["monitorId"]
            stored = run(core.load_monitor(monitor_id))
            self.assertEqual((stored["status"], stored["revision"]), ("active", 1))
            self.assertEqual(stored["lastTransitionId"], start_events[0]["transitionId"])

            stops, stop_events = self._race(
                home, session["id"], "task-start-race", monitor_id, ["stop", "stop"]
            )
            self.assertEqual(sorted(row["changed"] for row in stops), [False, True])
            self.assertEqual(len(stop_events), 1)
            stored = run(core.load_monitor(monitor_id))
            self.assertEqual((stored["status"], stored["revision"]), ("stopped", 2))
            self.assertEqual(stored["lastTransitionId"], stop_events[0]["transitionId"])
        finally:
            self.restore_home(root, previous)

    def test_terminal_race_preserves_first_status_and_enriches_observation(self):
        root, home, workspace, previous = self.temp_context()
        try:
            _config, session = self._session(home, workspace)
            monitor = core.create_monitor_record(
                {
                    "id": "monitor-terminal-race",
                    "sessionId": session["id"],
                    "taskId": "task-terminal-race",
                    "goal": "watch terminal",
                }
            )
            run(core.save_monitor(monitor))
            rows, events = self._race(
                home,
                session["id"],
                monitor["taskId"],
                monitor["id"],
                ["stop", "complete"],
            )
            self.assertEqual(len(events), 1)
            stored = run(core.load_monitor(monitor["id"]))
            self.assertIn(stored["status"], {"stopped", "completed"})
            self.assertEqual(stored["status"], events[0]["status"])
            self.assertEqual(stored["lastTransitionId"], events[0]["transitionId"])
            self.assertEqual(stored["lastObservedTaskStatus"], "completed")
            self.assertEqual(stored["lastObservedOutputSize"], 321)
            self.assertIn(stored["revision"], {1, 2})
            self.assertEqual(stored["revision"], max(row["revision"] for row in rows))
        finally:
            self.restore_home(root, previous)

    def test_parallel_model_tools_return_one_changed_result_and_typed_event(self):
        root, home, workspace, previous = self.temp_context()
        try:
            config, session = self._session(home, workspace)
            task_id = "task-monitor-tool-race"
            now = now_iso()
            session["metadata"]["taskIds"] = [task_id]
            run(
                core.save_task_record(
                    {
                        "id": task_id,
                        "sessionId": session["id"],
                        "sourceId": task_id,
                        "kind": "background_command",
                        "status": "running",
                        "title": "race task",
                        "createdAt": now,
                        "updatedAt": now,
                        "startedAt": now,
                    }
                )
            )
            run(
                core.write_task_meta(
                    task_id,
                    {
                        "taskId": task_id,
                        "sessionId": session["id"],
                        "type": "background_command",
                        "status": "running",
                        "cwd": str(workspace),
                        "command": "printf race",
                    },
                )
            )
            tools = {tool.name: tool for tool in core.create_builtin_tools()}
            events = []

            def context():
                return {
                    "session": json.loads(json.dumps(session)),
                    "config": config,
                    "interactive": False,
                    "askQuestion": lambda *_: "",
                    "outputJson": lambda event, data: events.append((event, data)),
                    "cwd": str(workspace),
                    "persist_session": lambda: None,
                }

            async def scenario():
                starts = await asyncio.gather(
                    tools["TaskMonitorStart"].handler(
                        {"task_id": task_id, "goal": "watch race"}, context()
                    ),
                    tools["TaskMonitorStart"].handler(
                        {"task_id": task_id, "goal": "watch race"}, context()
                    ),
                )
                start_payloads = [json.loads(result["content"]) for result in starts]
                monitor_id = start_payloads[0]["monitor_id"]
                session["metadata"]["monitorIds"] = [monitor_id]
                stops = await asyncio.gather(
                    tools["TaskMonitorStop"].handler(
                        {"monitor_id": monitor_id}, context()
                    ),
                    tools["TaskMonitorStop"].handler(
                        {"monitor_id": monitor_id}, context()
                    ),
                )
                return start_payloads, [json.loads(result["content"]) for result in stops]

            starts, stops = run(scenario())
            self.assertEqual(sorted(row["changed"] for row in starts), [False, True])
            self.assertEqual(sorted(row["changed"] for row in stops), [False, True])
            created = [data for event, data in events if event == "monitor.created"]
            changed = [data for event, data in events if event == "monitor.status_changed"]
            self.assertEqual((len(created), len(changed)), (1, 1))
            self.assertEqual(changed[0]["transition_id"], next(row["transition_id"] for row in stops if row["changed"]))
            typed = core.create_runtime_event(
                {"event": "monitor.status_changed", "data": changed[0]}
            )
            self.assertEqual(typed["payload"]["transitionId"], changed[0]["transition_id"])
            self.assertEqual(typed["payload"]["revision"], changed[0]["revision"])
            self.assertTrue(typed["payload"]["changed"])
        finally:
            self.restore_home(root, previous)


if __name__ == "__main__":
    unittest.main()
