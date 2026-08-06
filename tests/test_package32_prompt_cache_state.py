from __future__ import annotations

import copy
import os
from pathlib import Path

from tests.support.agent_core_case import *

import agent_core.providers.prompt_cache_state as prompt_cache_state_module
from agent_core.providers.prompt_cache_state import (
    commitPromptCacheObservation,
    createPromptCacheDispatchOrder,
    markPromptCacheEvictionIntent,
    promptCacheStatePath,
    readPromptCacheState,
)
from agent_core.providers.anthropic.payloads import planAnthropicCacheBreakpoints
from agent_core.providers.prompt_cache_diagnostics import (
    MAX_PROMPT_CACHE_STATE_BYTES,
    buildPromptCacheRequestSnapshot,
)
from agent_core.runtime.paths import (
    configureActiveRuntimePaths,
    resetActiveRuntimePaths,
    resolveRuntimePaths,
)


class PromptCacheStateTests(AgentCoreTestCase):
    def setUp(self):
        self.root, self.home, _, self.previous = self.temp_context()
        self.paths = resolveRuntimePaths(candidate=str(self.home))
        self.session_id = "session-state-test"

    def tearDown(self):
        self.restore_home(self.root, self.previous)

    def _candidate(self, *, order=None, text="stable"):
        policy = {
            "enabled": True,
            "mode": "explicit",
            "ttl": "1h",
            "scope": "ephemeral",
            "compatibility": "official",
        }
        request = {
            "model": "claude-opus-4-8",
            "system": "system",
            "messages": [{"role": "user", "content": text}, {"role": "user", "content": "volatile"}],
            "tools": [],
            "promptCache": policy,
        }
        plan = planAnthropicCacheBreakpoints(request)
        normalized_messages = [
            {"role": item["role"], "content": [{"type": "text", "text": item["content"]}]}
            for item in request["messages"]
        ]
        snapshot = buildPromptCacheRequestSnapshot(
            {
                "providerProfileName": "anthropic",
                "routeClass": "official",
                "model": request["model"],
                "betaTokens": ["cache-diagnosis-2026-04-07"],
                "normalizedTools": [],
                "normalizedSystem": [{"type": "text", "text": "system"}],
                "normalizedMessages": normalized_messages,
                "policy": policy,
                "plan": plan,
                "preparation": "normal",
            }
        )
        return {
            "sessionId": self.session_id,
            "paths": self.paths,
            "dispatchOrder": order or createPromptCacheDispatchOrder(
                {"runtimeGeneration": 2, "messageCount": 2, "reactiveAttempt": 0}
            ),
            "plan": plan,
            "snapshot": snapshot,
            "ttl": "1h",
            "scope": "ephemeral",
            "responseId": "msg_diagnostic",
            "usage": {
                "cache_read_input_tokens": 10,
                "cache_creation_input_tokens": 0,
                "cache_creation_5m_input_tokens": 0,
                "cache_creation_1h_input_tokens": 0,
            },
            "providerDiagnosis": "no_divergence",
        }

    def test_private_atomic_commit_and_eviction_idempotency(self):
        committed = run(commitPromptCacheObservation(self._candidate()))
        self.assertEqual(committed["status"], "committed")
        path = Path(promptCacheStatePath(self.session_id, self.paths))
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        loaded = run(readPromptCacheState({"sessionId": self.session_id, "paths": self.paths}))
        self.assertEqual(loaded["status"], "valid")
        self.assertEqual(loaded["state"]["response"]["id"], "msg_diagnostic")
        first = run(markPromptCacheEvictionIntent({
            "sessionId": self.session_id, "paths": self.paths, "finalizationKey": "final-key"
        }))
        second = run(markPromptCacheEvictionIntent({
            "sessionId": self.session_id, "paths": self.paths, "finalizationKey": "final-key"
        }))
        self.assertEqual((first["status"], second["status"]), ("recorded", "already_recorded"))
        self.assertEqual(first["state"]["revision"], committed["state"]["revision"] + 1)

    def test_worst_case_sidecar_is_bounded_and_contains_no_request_content(self):
        sentinel = "PRIVATE_PROMPT_SENTINEL"
        candidate = self._candidate(text=sentinel)
        snapshot = candidate["snapshot"]
        snapshot["boundedMessageHashes"] = ["a" * 64] * 64
        snapshot["boundedBlockHashes"] = ["b" * 64] * 128
        snapshot["boundedToolHashes"] = ["c" * 64] * 128
        snapshot["boundedBetaHashes"] = ["d" * 64] * 32
        candidate["responseId"] = None
        self.assertEqual(run(commitPromptCacheObservation(candidate))["status"], "committed")
        payload = Path(promptCacheStatePath(self.session_id, self.paths)).read_bytes()
        self.assertLessEqual(len(payload), MAX_PROMPT_CACHE_STATE_BYTES)
        self.assertNotIn(sentinel.encode("utf-8"), payload)

    def test_newest_dispatch_wins_across_concurrent_commits(self):
        old = createPromptCacheDispatchOrder(
            {"runtimeGeneration": 3, "messageCount": 2, "reactiveAttempt": 0, "nowMs": 100}
        )
        new = createPromptCacheDispatchOrder(
            {"runtimeGeneration": 3, "messageCount": 3, "reactiveAttempt": 0, "nowMs": 90}
        )

        async def compete():
            return await asyncio.gather(
                commitPromptCacheObservation(self._candidate(order=new, text="new")),
                commitPromptCacheObservation(self._candidate(order=old, text="old")),
            )

        results = run(compete())
        loaded = run(readPromptCacheState({"sessionId": self.session_id, "paths": self.paths}))
        self.assertEqual(loaded["state"]["dispatchOrder"], new)
        self.assertIn("stale", {item["status"] for item in results})

    def test_newest_dispatch_wins_across_processes(self):
        work = Path(self.root.name) / "workers"
        work.mkdir()
        go = work / "go"
        candidates = []
        for name, message_count, text in (("old", 2, "old"), ("new", 3, "new")):
            order = {
                "runtimeGeneration": 5,
                "messageCount": message_count,
                "reactiveAttempt": 0,
                "startedAtMs": 100,
                "localSequence": 1,
                "nonce": name,
            }
            candidate = self._candidate(order=order, text=text)
            candidate.pop("paths")
            path = work / f"{name}.json"
            path.write_text(json.dumps(candidate), encoding="utf-8")
            candidates.append((name, path))
        worker = Path(__file__).parent / "fixtures" / "prompt_cache_state_worker.py"
        processes = []
        for name, candidate in candidates:
            processes.append(
                subprocess.Popen(
                    [sys.executable, str(worker), str(candidate), str(self.home), str(work / f"{name}.ready"), str(go), str(work / f"{name}.result")],
                    cwd=Path(__file__).parents[1],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
            )
        deadline = time.monotonic() + 10
        while not all((work / f"{name}.ready").exists() for name, _ in candidates):
            self.assertLess(time.monotonic(), deadline, "workers did not reach barrier")
            time.sleep(0.01)
        go.write_text("go\n", encoding="utf-8")
        for process in processes:
            stdout, stderr = process.communicate(timeout=10)
            self.assertEqual(process.returncode, 0, stdout + stderr)
        loaded = run(readPromptCacheState({"sessionId": self.session_id, "paths": self.paths}))
        self.assertEqual(loaded["state"]["dispatchOrder"]["messageCount"], 3)
        statuses = {json.loads((work / f"{name}.result").read_text())["status"] for name, _ in candidates}
        self.assertTrue(statuses.issubset({"committed", "stale"}))

    def test_child_process_completion_orders_both_preserve_newest(self):
        worker = Path(__file__).parent / "fixtures" / "prompt_cache_state_worker.py"
        work = Path(self.root.name) / "ordered-workers"
        work.mkdir()
        for scenario, release_order in (("old-first", ("old", "new")), ("new-first", ("new", "old"))):
            session_id = f"{self.session_id}-{scenario}"
            processes = {}
            for name, message_count in (("old", 2), ("new", 3)):
                candidate = self._candidate(
                    order={
                        "runtimeGeneration": 5, "messageCount": message_count,
                        "reactiveAttempt": 0, "startedAtMs": 100,
                        "localSequence": 1, "nonce": name,
                    },
                    text=name,
                )
                candidate["sessionId"] = session_id
                candidate.pop("paths")
                candidate_path = work / f"{scenario}-{name}.json"
                candidate_path.write_text(json.dumps(candidate), encoding="utf-8")
                processes[name] = subprocess.Popen(
                    [
                        sys.executable, str(worker), str(candidate_path), str(self.home),
                        str(work / f"{scenario}-{name}.ready"),
                        str(work / f"{scenario}-{name}.go"),
                        str(work / f"{scenario}-{name}.result"),
                    ],
                    cwd=Path(__file__).parents[1], stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE, text=True,
                )
            deadline = time.monotonic() + 10
            while not all((work / f"{scenario}-{name}.ready").exists() for name in processes):
                self.assertLess(time.monotonic(), deadline, "ordered workers did not reach barrier")
                time.sleep(0.01)
            statuses = {}
            for name in release_order:
                (work / f"{scenario}-{name}.go").write_text("go\n", encoding="utf-8")
                stdout, stderr = processes[name].communicate(timeout=10)
                self.assertEqual(processes[name].returncode, 0, stdout + stderr)
                statuses[name] = json.loads(
                    (work / f"{scenario}-{name}.result").read_text(encoding="utf-8")
                )["status"]
            loaded = run(readPromptCacheState({"sessionId": session_id, "paths": self.paths}))
            self.assertEqual(loaded["state"]["dispatchOrder"]["messageCount"], 3)
            self.assertEqual(statuses["new"], "committed")
            self.assertEqual(statuses["old"], "committed" if scenario == "old-first" else "stale")

    def test_malformed_state_is_quarantined_but_future_schema_is_immutable(self):
        path = Path(promptCacheStatePath(self.session_id, self.paths))
        path.parent.mkdir(parents=True)
        path.write_text("{broken", encoding="utf-8")
        result = run(commitPromptCacheObservation(self._candidate()))
        self.assertEqual(result["status"], "committed")
        self.assertEqual(len(list(path.parent.glob("prompt-cache-state.json.invalid-*"))), 1)

        future = b'{"schemaVersion":2,"future":"preserve exactly"}\n'
        path.write_bytes(future)
        refused = run(commitPromptCacheObservation(self._candidate()))
        self.assertEqual(refused, {"status": "unavailable", "reason": "future_schema"})
        self.assertEqual(path.read_bytes(), future)

    def test_oversized_state_is_quarantined_and_session_mismatch_is_immutable(self):
        path = Path(promptCacheStatePath(self.session_id, self.paths))
        path.parent.mkdir(parents=True)
        path.write_bytes(b"x" * (128 * 1024 + 1))
        recovered = run(commitPromptCacheObservation(self._candidate()))
        self.assertEqual(recovered["status"], "committed")
        self.assertEqual(len(list(path.parent.glob("prompt-cache-state.json.invalid-*"))), 1)
        mismatched = json.loads(path.read_text(encoding="utf-8"))
        mismatched["sessionId"] = "different-session"
        exact = (json.dumps(mismatched, separators=(",", ":")) + "\n").encode("utf-8")
        path.write_bytes(exact)
        refused = run(commitPromptCacheObservation(self._candidate()))
        self.assertEqual(refused, {"status": "unavailable", "reason": "session_mismatch"})
        self.assertEqual(path.read_bytes(), exact)

    def test_exact_bound_is_readable_and_one_byte_over_is_oversized(self):
        run(commitPromptCacheObservation(self._candidate()))
        path = Path(promptCacheStatePath(self.session_id, self.paths))
        compact = path.read_bytes().rstrip()
        exact = compact + (b" " * (MAX_PROMPT_CACHE_STATE_BYTES - len(compact) - 1)) + b"\n"
        self.assertEqual(len(exact), MAX_PROMPT_CACHE_STATE_BYTES)
        path.write_bytes(exact)
        self.assertEqual(
            run(readPromptCacheState({"sessionId": self.session_id, "paths": self.paths}))["status"],
            "valid",
        )
        path.write_bytes(exact + b" ")
        self.assertEqual(
            run(readPromptCacheState({"sessionId": self.session_id, "paths": self.paths})),
            {"status": "unavailable", "reason": "oversized"},
        )

    def test_inode_switch_during_bounded_read_is_rejected(self):
        run(commitPromptCacheObservation(self._candidate()))
        path = Path(promptCacheStatePath(self.session_id, self.paths))
        replacement = path.with_name("replacement.json")
        replacement.write_bytes(path.read_bytes())
        original_read = os.read
        switched = False

        def read_and_switch(descriptor, size):
            nonlocal switched
            chunk = original_read(descriptor, size)
            if not switched:
                switched = True
                os.replace(replacement, path)
            return chunk

        with mock.patch.object(prompt_cache_state_module.os, "read", read_and_switch):
            result = run(readPromptCacheState({"sessionId": self.session_id, "paths": self.paths}))
        self.assertEqual(result, {"status": "unavailable", "reason": "changed_during_read"})

    def test_invalid_v1_is_quarantined_and_resume_fork_are_isolated(self):
        committed = run(commitPromptCacheObservation(self._candidate()))
        path = Path(promptCacheStatePath(self.session_id, self.paths))
        invalid = copy.deepcopy(committed["state"])
        invalid["request"]["modelHash"] = "not-a-hash"
        path.write_text(json.dumps(invalid), encoding="utf-8")
        self.assertEqual(
            run(readPromptCacheState({"sessionId": self.session_id, "paths": self.paths}))["reason"],
            "invalid_schema",
        )
        repaired = run(commitPromptCacheObservation(self._candidate()))
        self.assertEqual(repaired["status"], "committed")
        self.assertEqual(
            run(readPromptCacheState({"sessionId": self.session_id, "paths": self.paths}))["status"],
            "valid",
        )
        self.assertEqual(
            run(readPromptCacheState({"sessionId": f"{self.session_id}-fork", "paths": self.paths})),
            {"status": "missing", "reason": "missing"},
        )

    def test_dispatch_factory_and_captured_runtime_home_are_stable(self):
        left = createPromptCacheDispatchOrder(
            {"runtimeGeneration": 3, "messageCount": 4, "reactiveAttempt": 0, "nowMs": 100}
        )
        right = createPromptCacheDispatchOrder(
            {"runtimeGeneration": 3, "messageCount": 4, "reactiveAttempt": 0, "nowMs": 100}
        )
        self.assertGreater(right["localSequence"], left["localSequence"])
        self.assertNotEqual(right["nonce"], left["nonce"])

        other_home = Path(self.root.name) / "other-home"
        other_home.mkdir()
        other_paths = resolveRuntimePaths(candidate=str(other_home))
        configureActiveRuntimePaths(str(other_home))
        try:
            self.assertEqual(run(commitPromptCacheObservation(self._candidate()))["status"], "committed")
        finally:
            resetActiveRuntimePaths()
        self.assertEqual(
            run(readPromptCacheState({"sessionId": self.session_id, "paths": self.paths}))["status"],
            "valid",
        )
        self.assertEqual(
            run(readPromptCacheState({"sessionId": self.session_id, "paths": other_paths}))["status"],
            "missing",
        )

    def test_invalid_identifier_and_directory_target_degrade_safely(self):
        self.assertEqual(
            run(readPromptCacheState({"sessionId": "../escape", "paths": self.paths})),
            {"status": "unavailable", "reason": "unsafe_path"},
        )
        path = Path(promptCacheStatePath(self.session_id, self.paths))
        path.mkdir(parents=True)
        result = run(commitPromptCacheObservation(self._candidate()))
        self.assertEqual(result["status"], "unavailable")
        self.assertIn(result["reason"], {"unsafe_path", "state_write_failed"})

    @unittest.skipUnless(hasattr(os, "symlink"), "symlink support required")
    def test_symlink_state_is_refused_without_touching_target(self):
        path = Path(promptCacheStatePath(self.session_id, self.paths))
        path.parent.mkdir(parents=True)
        target = Path(self.root.name) / "outside.json"
        target.write_text('{"outside":true}\n', encoding="utf-8")
        path.symlink_to(target)
        loaded = run(readPromptCacheState({"sessionId": self.session_id, "paths": self.paths}))
        refused = run(commitPromptCacheObservation(self._candidate()))
        self.assertEqual(loaded, {"status": "unavailable", "reason": "unsafe_path"})
        self.assertEqual(refused, {"status": "unavailable", "reason": "unsafe_path"})
        self.assertEqual(target.read_text(encoding="utf-8"), '{"outside":true}\n')


if __name__ == "__main__":
    unittest.main()
