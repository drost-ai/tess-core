from __future__ import annotations

import asyncio
import copy
import json
import subprocess
import sys
import threading
import time
from unittest import mock

from tests.support.agent_core_case import *

from agent_core.agent.cancellation import (
    _capture_session_rollback,
    _restore_session_state,
)
from agent_core.storage.io_executor import (
    STORAGE_IO_MAX_WORKERS,
    reset_storage_executor_peak_for_testing,
    run_storage_io,
    storage_executor_snapshot,
)
from agent_core.payloads.attachments import strip_in_memory_image_data
from agent_core_harness.performance import (
    REPORT_SCHEMA,
    ROLLBACK_SNAPSHOT_BENCHMARK,
    runRollbackSnapshotBenchmark,
)


class Package46OffLoopStorageTests(AgentCoreTestCase):
    def test_generic_jsonl_appends_are_transactional_and_byte_valid(self):
        async def exercise(path: Path) -> None:
            from agent_core.storage.json_io import append_json_line, read_json_lines

            await asyncio.gather(
                *(
                    append_json_line(
                        str(path),
                        {"sequence": index, "payload": str(index) * 50_000},
                    )
                    for index in range(80)
                )
            )
            records = await read_json_lines(str(path))
            self.assertEqual(len(records), 80)
            self.assertEqual({record["sequence"] for record in records}, set(range(80)))

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "concurrent.jsonl"
            runtime_home = Path(tmp) / "runtime-home"

            async def scoped_exercise() -> None:
                async with core.scoped_runtime_home(str(runtime_home)):
                    await exercise(path)

            run(scoped_exercise())
            lines = path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 80)
            self.assertTrue(all(isinstance(json.loads(line), dict) for line in lines))

    def test_jsonl_transaction_serializes_cooperating_processes(self):
        child = """
import asyncio
import sys
import agent_core._facade as core
from agent_core.storage.json_io import append_json_line
home, path, prefix = sys.argv[1:]
core.configure_runtime_home(home)
async def main():
    await asyncio.gather(*(
        append_json_line(path, {"id": f"{prefix}-{index}", "payload": prefix * 20000})
        for index in range(25)
    ))
asyncio.run(main())
"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "multi-process.jsonl"
            home = root / "runtime-home"
            processes = [
                subprocess.Popen(
                    [sys.executable, "-c", child, str(home), str(path), str(index)],
                    cwd=Path(__file__).resolve().parents[1],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                for index in range(4)
            ]
            failures = []
            for process in processes:
                stdout, stderr = process.communicate(timeout=20)
                if process.returncode != 0:
                    failures.append((process.returncode, stdout, stderr))
            self.assertEqual(failures, [])
            records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(len(records), 100)
            self.assertEqual(len({record["id"] for record in records}), 100)

    def test_write_burst_keeps_event_loop_responsive(self):
        async def exercise(root: Path) -> tuple[int, float, float]:
            import agent_core.storage.json_io as json_io

            await asyncio.gather(
                *(run_storage_io(lambda: None) for _ in range(STORAGE_IO_MAX_WORKERS))
            )
            calls: list[str] = []

            def slow_write(path, value):
                time.sleep(0.01)
                calls.append(path)

            heartbeat: list[float] = []
            finished = asyncio.Event()

            async def beat() -> None:
                while not finished.is_set():
                    heartbeat.append(time.perf_counter())
                    await asyncio.sleep(0.001)

            ticker = asyncio.create_task(beat())
            try:
                with mock.patch.object(json_io, "_write_json_file_sync", slow_write):
                    for index in range(100):
                        await core.write_json_file(
                            str(root / f"record-{index}.json"),
                            {"index": index},
                        )
            finally:
                finished.set()
                await ticker
            gaps = [right - left for left, right in zip(heartbeat, heartbeat[1:])]
            self.assertEqual(len(calls), 100)
            ordered = sorted(gaps)
            p95 = ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))] if ordered else 0.0
            return len(heartbeat), p95, max(gaps, default=0.0)

        with tempfile.TemporaryDirectory() as tmp:
            beats, p95_gap, maximum_gap = run(exercise(Path(tmp)))
        self.assertGreater(beats, 200)
        self.assertLess(p95_gap, 0.005)
        self.assertLess(maximum_gap, 0.025)

    def test_storage_executor_caps_fifty_concurrent_sessions(self):
        async def exercise() -> dict[str, int]:
            release = threading.Event()

            def blocked_io() -> None:
                release.wait(timeout=5)

            reset_storage_executor_peak_for_testing()
            tasks = [asyncio.create_task(run_storage_io(blocked_io)) for _ in range(50)]
            deadline = time.monotonic() + 2
            while storage_executor_snapshot()["activeOperations"] < STORAGE_IO_MAX_WORKERS:
                if time.monotonic() >= deadline:
                    raise AssertionError("storage workers did not start")
                await asyncio.sleep(0.001)
            observed = storage_executor_snapshot()
            release.set()
            await asyncio.gather(*tasks)
            return observed | {"finalActive": storage_executor_snapshot()["activeOperations"]}

        observed = run(exercise())
        self.assertEqual(observed["maxWorkers"], STORAGE_IO_MAX_WORKERS)
        self.assertEqual(observed["peakActiveOperations"], STORAGE_IO_MAX_WORKERS)
        self.assertEqual(observed["finalActive"], 0)

    def test_cancelled_atomic_write_settles_worker_before_cancellation(self):
        async def exercise(path: Path) -> None:
            import agent_core.storage.json_io as json_io

            original = json_io._write_json_file_sync
            started = threading.Event()
            release = threading.Event()

            def held_write(file_path, value):
                started.set()
                release.wait(timeout=5)
                original(file_path, value)

            with mock.patch.object(json_io, "_write_json_file_sync", held_write):
                task = asyncio.create_task(core.write_json_file(str(path), {"committed": True}))
                while not started.is_set():
                    await asyncio.sleep(0)
                task.cancel()
                asyncio.get_running_loop().call_later(0.01, release.set)
                with self.assertRaises(asyncio.CancelledError):
                    await task

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "atomic.json"
            run(exercise(path))
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"committed": True})

    def test_cancelled_lock_open_closes_handle_and_releases_process_lock(self):
        async def exercise(path: Path) -> None:
            import agent_core.storage.json_io as json_io

            original = json_io._open_lock_handle
            started = threading.Event()
            release = threading.Event()
            handles = []

            def held_open(file_path):
                handle = original(file_path)
                handles.append(handle)
                started.set()
                release.wait(timeout=5)
                return handle

            async def acquire() -> None:
                async with core.file_lock(str(path)):
                    raise AssertionError("cancelled acquisition entered its critical section")

            with mock.patch.object(json_io, "_open_lock_handle", held_open):
                task = asyncio.create_task(acquire())
                while not started.is_set():
                    await asyncio.sleep(0)
                task.cancel()
                asyncio.get_running_loop().call_later(0.01, release.set)
                with self.assertRaises(asyncio.CancelledError):
                    await task

            self.assertEqual(len(handles), 1)
            self.assertTrue(handles[0].closed)
            async with core.file_lock(str(path), timeoutMs=100):
                pass

        with tempfile.TemporaryDirectory() as tmp:
            run(exercise(Path(tmp) / "record.json"))

    def test_repeated_cancellation_cannot_escape_before_cleanup_settles(self):
        async def exercise() -> tuple[bool, list[str], int]:
            started = threading.Event()
            release = threading.Event()
            cleanup_finished = threading.Event()
            cleanup_calls: list[str] = []

            def held_operation() -> str:
                started.set()
                release.wait(timeout=5)
                return "lock-handle"

            def cleanup(value: str) -> None:
                cleanup_calls.append(value)
                time.sleep(0.01)
                cleanup_finished.set()

            task = asyncio.create_task(
                run_storage_io(held_operation, cancelCleanup=cleanup)
            )
            while not started.is_set():
                await asyncio.sleep(0)
            task.cancel()
            await asyncio.sleep(0)
            task.cancel()
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
            return task.cancelled(), cleanup_calls, storage_executor_snapshot()["activeOperations"]

        cancelled, cleanup_calls, active = run(exercise())
        self.assertTrue(cancelled)
        self.assertEqual(cleanup_calls, ["lock-handle"])
        self.assertEqual(active, 0)

    def test_cancelled_storage_operation_keeps_cancellation_precedence_over_worker_error(self):
        async def exercise() -> bool:
            started = threading.Event()
            release = threading.Event()

            def failing_operation() -> None:
                started.set()
                release.wait(timeout=5)
                raise OSError("disk failed after cancellation")

            task = asyncio.create_task(run_storage_io(failing_operation))
            while not started.is_set():
                await asyncio.sleep(0)
            task.cancel()
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
            return task.cancelled()

        self.assertTrue(run(exercise()))

    def test_mailbox_append_and_rewrite_share_one_transaction(self):
        root, _home, _workspace, previous = self.temp_context()
        try:
            async def exercise() -> tuple[list[dict[str, Any]], Path]:
                child_session_id = "package46-child"
                first = await core.sendTaskMessage(
                    "package46-task",
                    {
                        "messageId": "seed",
                        "childSessionId": child_session_id,
                        "content": "seed",
                    },
                )
                await asyncio.gather(
                    core.markTaskMessageDelivered("package46-task", first["messageId"]),
                    *(
                        core.sendTaskMessage(
                            "package46-task",
                            {
                                "messageId": f"message-{index}",
                                "childSessionId": child_session_id,
                                "content": f"message-{index}:" + "x" * 50_000,
                            },
                        )
                        for index in range(60)
                    ),
                )
                return (
                    await core.readTaskMailboxMessages("package46-task"),
                    Path(core.task_dir("package46-task")) / "mailbox.jsonl",
                )

            messages, path = run(exercise())
            self.assertEqual(len(messages), 61)
            self.assertEqual({item["messageId"] for item in messages}, {"seed", *(f"message-{index}" for index in range(60))})
            self.assertEqual(next(item for item in messages if item["messageId"] == "seed")["status"], "delivered")
            self.assertTrue(all(isinstance(json.loads(line), dict) for line in path.read_text(encoding="utf-8").splitlines()))
        finally:
            self.restore_home(root, previous)

    def test_transcript_appends_remain_serial_and_byte_exact(self):
        root, _home, _workspace, previous = self.temp_context()
        try:
            async def exercise() -> Path:
                await asyncio.gather(
                    *(
                        core.append_session_transcript_log_entries(
                            "package46-transcript",
                            [{"type": "probe", "sequence": index}],
                        )
                        for index in range(50)
                    )
                )
                return Path(core.session_transcript_log_path("package46-transcript"))

            path = run(exercise())
            lines = path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 50)
            self.assertEqual(
                [json.loads(line)["sequence"] for line in lines],
                list(range(50)),
            )
            self.assertTrue(all(line == json.dumps(json.loads(line), ensure_ascii=False) for line in lines))
        finally:
            self.restore_home(root, previous)


class Package46RollbackRecordTests(AgentCoreTestCase):
    def _session(self, message_count: int = 8) -> dict[str, Any]:
        session = core.create_empty_session(
            cwd="/tmp/package46",
            provider="anthropic",
            model="claude-sonnet-4-6",
            systemPrompt="package 46",
            permissionMode="default",
            sessionId="package46-rollback",
        )
        session["messages"] = [
            {
                "role": "user" if index % 2 == 0 else "assistant",
                "content": f"message-{index}",
            }
            for index in range(message_count)
        ]
        session["todos"] = [{"content": "stable", "status": "pending"}]
        session["metadata"]["nested"] = {"stable": [1, 2, 3]}
        return session

    def _mutate_after_boundary(self, session: dict[str, Any], *, replace_messages: bool) -> None:
        if replace_messages:
            session["messages"] = copy.deepcopy(session["messages"])
        session["messages"].extend(
            [
                {"role": "assistant", "content": "unstable"},
                {"role": "user", "content": "tool result"},
            ]
        )
        session["todos"][0]["status"] = "completed"
        session["metadata"]["nested"]["stable"].append(4)
        session["metadata"]["transient"] = True
        session["permissionMode"] = "bypassPermissions"
        session["turnOnly"] = "remove me"

    def test_watermark_restore_is_legacy_byte_equal_for_append_and_replacement(self):
        for replace_messages in (False, True):
            with self.subTest(replace_messages=replace_messages):
                current = self._session()
                legacy_live = copy.deepcopy(current)
                legacy_snapshot = copy.deepcopy(legacy_live)
                legacy_snapshot["messages"] = strip_in_memory_image_data(legacy_snapshot["messages"])
                rollback = _capture_session_rollback(current)

                self._mutate_after_boundary(current, replace_messages=replace_messages)
                self._mutate_after_boundary(legacy_live, replace_messages=replace_messages)
                _restore_session_state(current, rollback)
                _restore_session_state(legacy_live, legacy_snapshot)

                self.assertEqual(current, legacy_live)
                self.assertEqual(
                    json.dumps(current, sort_keys=True, ensure_ascii=False),
                    json.dumps(legacy_live, sort_keys=True, ensure_ascii=False),
                )

    def test_rollback_strips_transient_media_only_when_restore_is_needed(self):
        session = self._session(0)
        session["messages"] = [
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {"media_type": "image/png", "data": "aGVsbG8="},
                    }
                ],
            }
        ]
        original_messages = session["messages"]
        rollback = _capture_session_rollback(session)
        self.assertIs(session["messages"], original_messages)
        self.assertEqual(session["messages"][0]["content"][0]["type"], "image")

        session["messages"].append({"role": "assistant", "content": "later"})
        _restore_session_state(session, rollback)

        self.assertEqual(len(session["messages"]), 1)
        self.assertEqual(session["messages"][0]["content"][0]["type"], "text")
        self.assertNotIn("aGVsbG8=", json.dumps(session))

    def test_rollback_record_is_authoritative_after_in_place_tool_mutation(self):
        session = self._session()
        legacy = copy.deepcopy(session)
        rollback = _capture_session_rollback(session)
        session["messages"][0]["content"] = "mutated by tool"
        session["messages"].clear()
        _restore_session_state(session, rollback)
        self.assertEqual(session, legacy)

    def test_incremental_capture_owns_messages_and_reuses_frozen_prefix(self):
        session = self._session(5_000)
        marker = session["messages"][2_500]
        cold = _capture_session_rollback(session)
        session["messages"].append({"role": "user", "content": "new boundary"})
        started = time.perf_counter_ns()
        rollback = _capture_session_rollback(session, cold)
        elapsed_ns = time.perf_counter_ns() - started

        self.assertIs(rollback.messageSource, session["messages"])
        self.assertIsNot(rollback.frozenMessages[2_500], marker)
        self.assertIs(rollback.frozenMessages[2_500], cold.frozenMessages[2_500])
        self.assertIsNot(rollback.frozenMessages[-1], session["messages"][-1])
        self.assertNotIn("messages", rollback.head)
        # A generous ceiling catches a regression back to a whole-history copy
        # at every stable boundary. The benchmark carries the tighter budget.
        self.assertLess(elapsed_ns, 2_000_000)

    def test_rollback_benchmark_records_before_after_at_long_session_scale(self):
        report = runRollbackSnapshotBenchmark(
            message_counts=[200, 5_000],
            samples=5,
            content_bytes=128,
        )
        self.assertEqual(report["schemaVersion"], REPORT_SCHEMA)
        self.assertEqual(report["benchmark"], ROLLBACK_SNAPSHOT_BENCHMARK)
        long_session = report["series"][-1]
        self.assertEqual(long_session["messageCount"], 5_000)
        self.assertLess(
            long_session["rollbackCaptureMedianNs"],
            long_session["legacyDeepcopyMedianNs"],
        )
        self.assertGreater(long_session["speedupRatio"], 5)
