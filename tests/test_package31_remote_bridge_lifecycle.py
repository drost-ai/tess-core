from __future__ import annotations

import asyncio
import hashlib
import json
import os
import threading
from pathlib import Path

from tests.support.tess_core_case import TessCoreTestCase, core, run

import tess_core.remote.bridge_recovery as bridge_recovery
from tess_core.storage.remote import (
    append_bridge_event,
    append_bridge_inbox_message,
    bridge_events_path,
    bridge_inbox_path,
    bridge_recovery_path,
    load_bridge_delivery_ledger,
    replace_bridge_inbox,
    save_bridge_delivery_ledger,
)


class Package31RemoteBridgeLifecycleTests(TessCoreTestCase):
    def _session_remote(self, workspace):
        session = core.create_empty_session(
            cwd=str(workspace), provider="openai", providerProfileName="openai",
            model="gpt-5.4", effort="high", systemPrompt="test",
            permissionMode="bypassPermissions",
        )
        run(core.save_session(session))
        remote = run(core.create_remote_session({"session": session, "channelServers": []}))
        return session, remote

    def test_replacement_copies_pending_and_stale_close_cannot_close_winner(self):
        root, _home, workspace, previous = self.temp_context()
        try:
            session, remote = self._session_remote(workspace)
            first = run(core.create_bridge_runtime({
                "session": session, "remoteSessionId": remote["id"],
                "startPolling": False, "emit": lambda *_args: None,
            }))
            queued = run(core.send_bridge_message({
                "bridgeId": first["bridge"]["id"], "generation": first["generation"],
                "idempotencyKey": "survive-replacement", "text": "pending",
            }))
            second = run(core.create_bridge_runtime({
                "session": session, "remoteSessionId": remote["id"],
                "startPolling": False, "emit": lambda *_args: None,
            }))
            self.assertEqual(second["generation"], first["generation"] + 1)
            self.assertEqual(run(core.load_bridge(first["bridge"]["id"]))["status"], "superseded")
            copied = run(core.load_bridge_inbox(second["bridge"]["id"]))
            self.assertEqual([(item["id"], item["idempotencyKey"]) for item in copied], [
                (queued["id"], "survive-replacement")
            ])
            run(first["close"]())
            self.assertEqual(run(core.load_bridge(second["bridge"]["id"]))["status"], "active")
            with self.assertRaisesRegex(RuntimeError, "not the active session generation"):
                run(core.send_bridge_message({"bridgeId": first["bridge"]["id"], "text": "stale"}))
            run(second["close"]())
            self.assertEqual(run(core.load_bridge(second["bridge"]["id"]))["status"], "closed")
        finally:
            self.restore_home(root, previous)

    def test_superseded_during_construction_never_returns_stale_runtime(self):
        root, _home, workspace, previous = self.temp_context()
        try:
            session, remote = self._session_remote(workspace)
            entered = threading.Event()
            release = threading.Event()

            def delayed(event, _data):
                if event == "bridge.started":
                    entered.set()
                    release.wait(2)

            async def race():
                first = asyncio.create_task(core.create_bridge_runtime({
                    "session": session, "remoteSessionId": remote["id"],
                    "emit": delayed, "startPolling": False,
                }))
                self.assertTrue(await asyncio.to_thread(entered.wait, 1))
                winner = await core.create_bridge_runtime({
                    "session": session, "remoteSessionId": remote["id"],
                    "emit": lambda *_args: None, "startPolling": False,
                })
                release.set()
                with self.assertRaisesRegex(RuntimeError, "superseded during construction"):
                    await first
                return winner

            winner = run(race())
            active = [bridge for bridge in run(core.list_bridges()) if bridge["status"] == "active"]
            self.assertEqual([bridge["id"] for bridge in active], [winner["bridge"]["id"]])
            run(winner["close"]())
        finally:
            self.restore_home(root, previous)

    def test_unsettled_emit_is_not_overlapped_or_allowed_to_commit_late(self):
        root, _home, workspace, previous = self.temp_context()
        try:
            session, remote = self._session_remote(workspace)
            release = threading.Event()
            entered = threading.Event()
            calls = {"active": 0, "maximum": 0, "messages": 0, "failures": 0}

            def emit(event, _data):
                if event == "bridge.poll_failed":
                    calls["failures"] += 1
                    return
                if event != "bridge.message":
                    return
                calls["messages"] += 1
                calls["active"] += 1
                calls["maximum"] = max(calls["maximum"], calls["active"])
                entered.set()
                release.wait(2)
                calls["active"] -= 1

            runtime = run(core.create_bridge_runtime({
                "session": session, "remoteSessionId": remote["id"], "emit": emit,
                "pollIntervalMs": 5, "pollDeadlineMs": 25,
            }))
            run(core.send_bridge_message({"bridgeId": runtime["bridge"]["id"], "text": "late"}))
            self.assertTrue(entered.wait(1))
            run(asyncio.sleep(0.08))
            self.assertEqual((calls["messages"], calls["maximum"], calls["failures"]), (1, 1, 0))
            failures = [
                event for event in run(core.load_bridge_events(runtime["bridge"]["id"]))
                if event["event"] == "bridge.poll_failed"
            ]
            self.assertEqual(len(failures), 1)
            with self.assertRaises(RuntimeError) as caught:
                run(runtime["close"]())
            self.assertEqual(getattr(caught.exception, "code", None), "bridge.close_unsettled")
            before = run(load_bridge_delivery_ledger(runtime["bridge"]["id"]))
            self.assertEqual((before["cursor"], before["entries"][-1]["disposition"]), (0, "pending"))
            release.set()
            run(asyncio.sleep(0.04))
            run(runtime["close"]())
            after = run(load_bridge_delivery_ledger(runtime["bridge"]["id"]))
            self.assertEqual((after["cursor"], after["entries"][-1]["disposition"]), (0, "pending"))
        finally:
            self.restore_home(root, previous)

    def test_admission_is_idempotent_and_receipt_repairs_without_reemit(self):
        root, _home, workspace, previous = self.temp_context()
        try:
            session, remote = self._session_remote(workspace)
            activated = run(bridge_recovery.activate_bridge_generation({
                "sessionId": session["id"], "cwd": str(workspace),
                "remoteSessionId": remote["id"],
            }))
            bridge = activated["bridge"]

            async def admit_many():
                shared = await asyncio.gather(*[
                    core.send_bridge_message({
                        "bridgeId": bridge["id"], "idempotencyKey": "shared",
                        "text": "same",
                    }) for _ in range(20)
                ])
                await asyncio.gather(*[
                    core.send_bridge_message({
                        "bridgeId": bridge["id"], "idempotencyKey": f"distinct-{index}",
                        "text": str(index),
                    }) for index in range(20)
                ])
                return shared

            shared = run(admit_many())
            self.assertEqual(len({message["id"] for message in shared}), 1)
            self.assertEqual(len(run(core.load_bridge_inbox(bridge["id"]))), 21)
            ledger = run(load_bridge_delivery_ledger(bridge["id"]))
            self.assertEqual(len({entry["key"] for entry in ledger["entries"]}), 21)

            message = shared[0]
            key = message["idempotencyKey"]
            run(append_bridge_event({
                "id": f"delivery-{hashlib.sha256(key.encode()).hexdigest()[:48]}",
                "bridgeId": bridge["id"], "generation": bridge["generation"],
                "idempotencyKey": key, "createdAt": "2026-07-01T02:00:00+00:00",
                "event": "bridge.message.received",
                "data": {
                    "message_id": message["id"], "delivery_key": key,
                    "source": message["source"], "text": message["text"],
                },
            }))
            deliveries = []
            runtime = run(core.create_bridge_runtime({
                "session": run(core.load_session(session["id"])), "remoteSessionId": remote["id"],
                "reuseActive": True, "startPolling": False,
                "emit": lambda event, data: deliveries.append((event, data)),
            }))
            run(runtime["pollInbox"]())
            self.assertFalse(any(event == "bridge.message" and data["delivery_key"] == key for event, data in deliveries))
            repaired = run(load_bridge_delivery_ledger(bridge["id"]))
            self.assertGreaterEqual(repaired["cursor"], 1)
            self.assertEqual(repaired["entries"][0]["disposition"], "delivered")
            run(runtime["close"]())
        finally:
            self.restore_home(root, previous)

    def test_legacy_ambiguity_requires_explicit_resolution_and_preserves_sources(self):
        root, _home, workspace, previous = self.temp_context()
        try:
            session, remote = self._session_remote(workspace)
            for bridge_id in ("legacy-a", "legacy-b"):
                run(core.save_bridge({
                    "id": bridge_id, "sessionId": session["id"], "remoteSessionId": remote["id"],
                    "createdAt": "2026-07-01T00:00:00+00:00",
                    "updatedAt": "2026-07-01T00:00:00+00:00",
                    "status": "active", "cwd": str(workspace),
                }))
                run(replace_bridge_inbox(bridge_id, [{
                    "id": f"message-{bridge_id}", "bridgeId": bridge_id,
                    "createdAt": "2026-07-01T00:00:01+00:00",
                    "source": "remote", "text": bridge_id,
                }]))
            before = {
                bridge_id: Path(bridge_inbox_path(bridge_id)).read_bytes()
                for bridge_id in ("legacy-a", "legacy-b")
            }
            inspection = run(core.inspect_bridge_recovery(session["id"]))
            self.assertIn("multiple_active_without_authority", inspection["blockedReasons"])
            self.assertIn("delivery_unconfirmed", inspection["blockedReasons"])
            with self.assertRaises(core.BridgeRecoveryBlockedError):
                run(bridge_recovery.activate_bridge_generation({
                    "sessionId": session["id"], "cwd": str(workspace),
                }))
            with self.assertRaisesRegex(RuntimeError, "exactly one decision"):
                run(core.resolve_bridge_recovery({
                    "sessionId": session["id"],
                    "expectedRecoveryRevision": inspection["recoveryRevision"],
                    "recoveryDigest": inspection["recoveryDigest"],
                    "selectedLegacyBridgeId": "legacy-a", "deliveryDecisions": {},
                }))
            resolution = run(core.resolve_bridge_recovery({
                "sessionId": session["id"],
                "expectedRecoveryRevision": inspection["recoveryRevision"],
                "recoveryDigest": inspection["recoveryDigest"],
                "selectedLegacyBridgeId": "legacy-a",
                "deliveryDecisions": {
                    key: "replay_at_least_once"
                    for key in inspection["deliveryUnconfirmedKeys"]
                },
            }))
            destination = run(core.load_bridge(resolution["activeBridgeId"]))
            self.assertEqual((destination["schemaVersion"], destination["status"]), (2, "active"))
            self.assertEqual(len(run(core.load_bridge_inbox(destination["id"]))), 2)
            for bridge_id, raw in before.items():
                self.assertEqual(Path(bridge_inbox_path(bridge_id)).read_bytes(), raw)
        finally:
            self.restore_home(root, previous)

    def test_recovery_journal_resumes_and_rejects_path_escape(self):
        root, _home, workspace, previous = self.temp_context()
        try:
            session, _remote = self._session_remote(workspace)
            injected = {"value": False}

            def hook(phase, _journal):
                if phase == "session_pointer_written" and not injected["value"]:
                    injected["value"] = True
                    raise RuntimeError("simulated crash")

            set_hook = getattr(bridge_recovery, "_set_bridge_recovery_test_hook_for_tests")
            set_hook(hook)
            with self.assertRaisesRegex(RuntimeError, "simulated crash"):
                run(bridge_recovery.activate_bridge_generation({
                    "sessionId": session["id"], "cwd": str(workspace),
                }))
            set_hook(None)
            self.assertEqual(run(core.inspect_bridge_recovery(session["id"]))["blockedReasons"], [])
            current = run(core.load_session(session["id"]))
            self.assertEqual(run(core.load_bridge(current["metadata"]["bridgeId"]))["status"], "active")

            journal_path = Path(bridge_recovery_path(session["id"]))
            journal = json.loads(journal_path.read_text())
            journal["immutableSourceFiles"][0]["path"] = "/etc/passwd"
            journal_path.write_text(json.dumps(journal) + "\n")
            with self.assertRaisesRegex(RuntimeError, "recovery journal.+unavailable"):
                run(core.inspect_bridge_recovery(session["id"]))
        finally:
            getattr(bridge_recovery, "_set_bridge_recovery_test_hook_for_tests")(None)
            self.restore_home(root, previous)


if __name__ == "__main__":
    import unittest

    unittest.main()
