from __future__ import annotations

import asyncio
import multiprocessing
import os
import time
import uuid
from pathlib import Path

import agent_core._facade as core
from tests.support.agent_core_case import AgentCoreTestCase, run


def _session_mutation_worker(
    runtime_home: str,
    session_id: str,
    task_id: str,
    barrier_dir: str,
) -> None:
    core.configure_runtime_home(runtime_home)
    Path(barrier_dir, f"ready-{task_id}").write_text("ready\n", encoding="utf-8")
    deadline = time.monotonic() + 15
    while not Path(barrier_dir, "go").exists():
        if time.monotonic() >= deadline:
            raise TimeoutError("timed out waiting for transaction barrier")
        time.sleep(0.005)

    async def mutate() -> None:
        await core.mutate_session_record_transaction(
            session_id,
            lambda latest: {
                "changed": task_id not in latest.get("metadata", {}).get("taskIds", []),
                "session": {
                    **latest,
                    "metadata": {
                        **latest.get("metadata", {}),
                        "taskIds": list(dict.fromkeys([
                            *latest.get("metadata", {}).get("taskIds", []),
                            task_id,
                        ])),
                    },
                },
            },
        )

    asyncio.run(mutate())


class SessionTransactionTests(AgentCoreTestCase):
    def test_twenty_five_cross_process_mutations_retain_every_task_id(self):
        root, home, workspace, previous = self.temp_context()
        processes: list[multiprocessing.Process] = []
        try:
            barrier = Path(root.name, "transaction-barrier")
            barrier.mkdir()
            session = core.create_empty_session(
                cwd=str(workspace),
                provider="anthropic",
                model="claude-sonnet-4-6",
                systemPrompt="test",
                permissionMode="default",
                name="transaction sentinel",
            )
            session["messages"] = [{"role": "user", "content": "preserve me"}]
            session["metadata"]["remoteSessionId"] = "remote-sentinel"
            run(core.save_session(session))
            task_ids = [str(uuid.UUID(int=index + 1, version=4)) for index in range(25)]
            context = multiprocessing.get_context("spawn")
            processes = [
                context.Process(
                    target=_session_mutation_worker,
                    args=(str(home), session["id"], task_id, str(barrier)),
                )
                for task_id in task_ids
            ]
            for process in processes:
                process.start()
            deadline = time.monotonic() + 20
            while len(list(barrier.glob("ready-*"))) < 25:
                self.assertLess(time.monotonic(), deadline, "workers did not reach the barrier")
                time.sleep(0.01)
            (barrier / "go").write_text("go\n", encoding="utf-8")
            for process in processes:
                process.join(20)
                self.assertEqual(process.exitcode, 0)

            persisted = run(core.load_session(session["id"]))
            self.assertIsNotNone(persisted)
            self.assertEqual(set(persisted["metadata"]["taskIds"]), set(task_ids))
            self.assertEqual(len(persisted["metadata"]["taskIds"]), 25)
            self.assertEqual(persisted["revision"], 25)
            self.assertEqual(persisted["name"], "transaction sentinel")
            self.assertEqual(persisted["metadata"]["remoteSessionId"], "remote-sentinel")
            self.assertEqual(persisted["messages"], [{"role": "user", "content": "preserve me"}])
        finally:
            for process in processes:
                if process.is_alive():
                    process.terminate()
                    process.join(5)
            self.restore_home(root, previous)

    def test_transactions_are_reentrant_and_noop_is_mtime_stable(self):
        root, home, workspace, previous = self.temp_context()
        try:
            session = core.create_empty_session(
                cwd=str(workspace),
                provider="anthropic",
                model="claude-sonnet-4-6",
                systemPrompt="test",
                permissionMode="default",
            )
            run(core.save_session(session))

            async def nested_update() -> dict:
                async with core.with_record_transaction("session", session["id"]):
                    return await core.mutate_session_record_transaction(
                        session["id"],
                        lambda latest: {"changed": True, "session": {**latest, "name": "nested"}},
                        requireExisting=True,
                    )

            committed = run(nested_update())
            self.assertEqual(committed["revision"], 1)
            path = core.session_path(session["id"])
            before = os.stat(path).st_mtime_ns
            no_op = run(core.mutate_session_record_transaction(
                session["id"],
                lambda latest: {"changed": False, "session": latest},
                requireExisting=True,
            ))
            after = os.stat(path).st_mtime_ns
            self.assertEqual(no_op["revision"], 1)
            self.assertEqual(after, before)
        finally:
            self.restore_home(root, previous)

    def test_torn_head_degrades_to_absent_and_full_save_repairs(self):
        root, _home, workspace, previous = self.temp_context()
        try:
            session = core.create_empty_session(
                cwd=str(workspace),
                provider="anthropic",
                model="claude-sonnet-4-6",
                systemPrompt="test",
                permissionMode="default",
            )
            session["messages"] = [{"role": "user", "content": "durable"}]
            run(core.save_session(session))
            path = Path(core.session_path(session["id"]))
            path.write_bytes(b'{"id": "torn')

            with self.assertRaises(core.SessionRecordNotFoundError):
                run(core.mutate_session_record_transaction(
                    session["id"],
                    lambda latest: {"changed": True, "session": latest},
                    requireExisting=True,
                ))
            self.assertIsNone(run(core.load_session(session["id"])))

            run(core.save_session(session))
            committed = run(core.mutate_session_record_transaction(
                session["id"],
                lambda latest: {"changed": True, "session": {**latest, "name": "repaired"}},
                requireExisting=True,
            ))
            self.assertEqual(committed["session"]["name"], "repaired")
            persisted = run(core.load_session(session["id"]))
            self.assertEqual(persisted["messages"], [{"role": "user", "content": "durable"}])
            self.assertEqual(persisted["name"], "repaired")
        finally:
            self.restore_home(root, previous)

    def test_stale_foreground_persist_preserves_concurrent_fields_and_revision(self):
        root, home, workspace, previous = self.temp_context()
        try:
            session = core.create_empty_session(
                cwd=str(workspace),
                provider="anthropic",
                model="claude-sonnet-4-6",
                systemPrompt="test",
                permissionMode="default",
            )
            session["messages"] = [{"role": "user", "content": "durable"}]
            run(core.save_session(session))
            stale = run(core.load_session(session["id"]))
            task_id = str(uuid.uuid4())
            run(core.mutate_session_record_transaction(
                session["id"],
                lambda latest: {
                    "changed": True,
                    "session": {
                        **latest,
                        "metadata": {
                            **latest["metadata"],
                            "taskIds": [task_id],
                            "daemonUnavailable": {"reason": "sentinel", "at": "2026-01-01T00:00:00Z"},
                        },
                    },
                },
            ))
            stale["name"] = "foreground update"
            stale["messages"].append({"role": "assistant", "content": "new turn"})
            run(core.persistSession(stale, {"sessionPersistence": True}, touch=False))

            persisted = run(core.load_session(session["id"]))
            self.assertEqual(persisted["name"], "foreground update")
            self.assertEqual(persisted["metadata"]["taskIds"], [task_id])
            self.assertEqual(persisted["metadata"]["daemonUnavailable"]["reason"], "sentinel")
            self.assertEqual(len(persisted["messages"]), 2)
            self.assertEqual(persisted["revision"], 2)
        finally:
            self.restore_home(root, previous)
