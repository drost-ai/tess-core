from __future__ import annotations

from tests.support.agent_core_case import *

from agent_core.persistence_metrics import registerPersistenceObserver
from agent_core_harness.executor import run_scenario
from agent_core_harness.performance import (
    PERSISTENCE_BENCHMARK,
    REPORT_SCHEMA,
    runPersistenceBenchmark,
    writePerformanceReport,
)


class Package47APersistenceInstrumentationTests(AgentCoreTestCase):
    def test_persist_session_reports_actual_calls_writes_and_bytes(self):
        root, _home, workspace, previous = self.temp_context()
        try:
            session = core.createEmptySession(
                cwd=str(workspace),
                provider="anthropic",
                model="claude-sonnet-4-6",
                systemPrompt="test",
                permissionMode="default",
                sessionId="package47a-observer",
            )
            session["messages"] = [{"role": "user", "content": "measure me"}]
            events: list[dict[str, object]] = []
            unsubscribe = registerPersistenceObserver(events.append)
            try:
                run(core.persistSession(session, {"sessionPersistence": True}, touch=False))
            finally:
                unsubscribe()

            started = [event for event in events if event["event"] == "persist_call_started"]
            finished = [event for event in events if event["event"] == "persist_call_finished"]
            io_events = [event for event in events if event["event"] == "storage_io"]
            self.assertEqual(len(started), 1)
            self.assertEqual(len(finished), 1)
            self.assertEqual(finished[0]["outcome"], "persisted")
            self.assertEqual(started[0]["persistenceCallId"], finished[0]["persistenceCallId"])
            self.assertTrue(all(event["persistenceCallId"] == started[0]["persistenceCallId"] for event in io_events))
            self.assertEqual([event["operation"] for event in io_events], ["append", "append", "write"])
            transcript_event, body_event, write_event = io_events
            self.assertEqual(
                transcript_event["byteCount"],
                Path(core.sessionTranscriptLogPath(session["id"])).stat().st_size,
            )
            self.assertEqual(
                body_event["byteCount"],
                Path(core.sessionMessagesPath(session["id"])).stat().st_size,
            )
            self.assertEqual(
                write_event["byteCount"],
                Path(core.sessionPath(session["id"])).stat().st_size,
            )
        finally:
            self.restore_home(root, previous)

    def test_skipped_persist_is_counted_without_storage_io(self):
        session = {"id": "package47a-skipped", "messages": []}
        events: list[dict[str, object]] = []
        unsubscribe = registerPersistenceObserver(events.append)
        try:
            self.assertIsNone(run(core.persistSession(session, {"sessionPersistence": True})))
        finally:
            unsubscribe()
        self.assertEqual([event["event"] for event in events], ["persist_call_started", "persist_call_finished"])
        self.assertEqual(events[-1]["outcome"], "skipped")

    def test_observer_failure_cannot_break_persistence(self):
        root, _home, workspace, previous = self.temp_context()
        try:
            session = core.createEmptySession(
                cwd=str(workspace),
                provider="anthropic",
                model="claude-sonnet-4-6",
                systemPrompt="test",
                permissionMode="default",
                sessionId="package47a-isolation",
            )
            session["messages"] = [{"role": "user", "content": "still persist"}]

            def broken_observer(_event):
                raise RuntimeError("observer defect")

            unsubscribe = registerPersistenceObserver(broken_observer)
            try:
                persisted = run(core.persistSession(session, {"sessionPersistence": True}, touch=False))
            finally:
                unsubscribe()
            self.assertEqual(persisted["id"], session["id"])
            self.assertTrue(Path(core.sessionPath(session["id"])).exists())
        finally:
            self.restore_home(root, previous)


class Package47AMeasurementSubstrateTests(AgentCoreTestCase):
    def test_persistence_benchmark_uses_observed_functional_metrics(self):
        report = runPersistenceBenchmark(
            message_counts=[6, 12],
            samples=2,
            persists_per_sample=2,
            content_bytes=32,
        )
        self.assertEqual(report["schemaVersion"], REPORT_SCHEMA)
        self.assertEqual(report["benchmark"], PERSISTENCE_BENCHMARK)
        self.assertEqual(report["parameters"]["messageCounts"], [6, 12])
        for series in report["series"]:
            self.assertGreater(series["serializedSessionBytesMedian"], 0)
            self.assertEqual(series["median"]["persistCallCount"], 2)
            self.assertEqual(series["median"]["storageWriteCount"], 2)
            self.assertEqual(series["median"]["storageAppendCount"], 0)
            self.assertGreater(series["median"]["storageBytes"], 0)
            self.assertTrue(all(sample["persistOutcomes"] == {"persisted": 2} for sample in series["samples"]))

    def test_performance_report_writer_is_stable_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "nested" / "report.json"
            writePerformanceReport({"schemaVersion": REPORT_SCHEMA, "value": 1}, str(target))
            self.assertEqual(
                json.loads(target.read_text(encoding="utf-8")),
                {"schemaVersion": REPORT_SCHEMA, "value": 1},
            )


class Package47AAnthropicFaultMatrixTests(AgentCoreTestCase):
    def test_initial_fault_matrix_matches_committed_golden(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = run_scenario(
                "providers.anthropic-fault-matrix-pack",
                runs_dir=tmp,
                cleanup="never",
                golden="compare",
            )
            self.assertEqual(result.status, "passed", result.failure)
            observations = json.loads(Path(result.observationsPath).read_text(encoding="utf-8"))
            matrix = observations["provider_fault_matrix"]
            self.assertEqual(matrix["coverage"]["executedCases"], 16)
            self.assertFalse(matrix["coverage"]["complete"])
            self.assertTrue(all(case["verdict"] == "matched" for case in matrix["cases"]))
            disconnects = [case for case in matrix["cases"] if case["caseId"] == "anthropic.s0.f1-disconnect-retry"]
            self.assertEqual(len(disconnects), 2)
            self.assertTrue(all("package45-closed" in case["tags"] for case in disconnects))
            self.assertEqual(matrix["connectionModeParity"]["status"], "passed")
