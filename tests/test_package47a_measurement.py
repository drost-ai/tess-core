from __future__ import annotations

from tests.support.tess_core_case import *

from tess_core.storage.persistence_metrics import register_persistence_observer
from tess_core_harness.executor import run_scenario
from tess_core_harness.performance import (
    PERSISTENCE_BENCHMARK,
    REPORT_SCHEMA,
    runPersistenceBenchmark,
    writePerformanceReport,
)


class Package47APersistenceInstrumentationTests(TessCoreTestCase):
    def test_persist_session_reports_actual_calls_writes_and_bytes(self):
        root, _home, workspace, previous = self.temp_context()
        try:
            session = core.create_empty_session(
                cwd=str(workspace),
                provider="anthropic",
                model="claude-sonnet-4-6",
                systemPrompt="test",
                permissionMode="default",
                sessionId="package47a-observer",
            )
            session["messages"] = [{"role": "user", "content": "measure me"}]
            events: list[dict[str, object]] = []
            unsubscribe = register_persistence_observer(events.append)
            try:
                run(core.persist_session(session, {"sessionPersistence": True}, touch=False))
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
                Path(core.session_transcript_log_path(session["id"])).stat().st_size,
            )
            self.assertEqual(
                body_event["byteCount"],
                Path(core.session_messages_path(session["id"])).stat().st_size,
            )
            self.assertEqual(
                write_event["byteCount"],
                Path(core.session_path(session["id"])).stat().st_size,
            )
        finally:
            self.restore_home(root, previous)

    def test_skipped_persist_is_counted_without_storage_io(self):
        session = {"id": "package47a-skipped", "messages": []}
        events: list[dict[str, object]] = []
        unsubscribe = register_persistence_observer(events.append)
        try:
            self.assertIsNone(run(core.persist_session(session, {"sessionPersistence": True})))
        finally:
            unsubscribe()
        self.assertEqual([event["event"] for event in events], ["persist_call_started", "persist_call_finished"])
        self.assertEqual(events[-1]["outcome"], "skipped")

    def test_observer_failure_cannot_break_persistence(self):
        root, _home, workspace, previous = self.temp_context()
        try:
            session = core.create_empty_session(
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

            unsubscribe = register_persistence_observer(broken_observer)
            try:
                run(core.persist_session(session, {"sessionPersistence": True}, touch=False))
            finally:
                unsubscribe()
            persisted = run(core.load_session(session["id"]))
            self.assertEqual(persisted["id"], session["id"])
            self.assertTrue(Path(core.session_path(session["id"])).exists())
        finally:
            self.restore_home(root, previous)


class Package47AMeasurementSubstrateTests(TessCoreTestCase):
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


class Package47AAnthropicFaultMatrixTests(TessCoreTestCase):
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
