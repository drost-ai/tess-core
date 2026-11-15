from __future__ import annotations

from tests.support.tess_core_case import *


class CompactionHardeningTests(TessCoreTestCase):
    def test_legacy_working_context_projects_only_newest_bounded_trace(self):
        traces = [
            {"sourceRange": {"generation": 1}, "content": "old-one"},
            {"sourceRange": {"generation": 2}, "content": "old-two"},
            {
                "sourceRange": {"generation": 3},
                "tokenEstimate": 30_000,
                "tokenBudget": 30_000,
                "truncated": False,
                "content": "x" * 120_000,
            },
        ]

        projected = core.coerce_preserved_trace(traces)

        self.assertEqual(len(projected), 1)
        self.assertEqual(projected[0]["sourceRange"]["generation"], 3)
        self.assertNotIn("old-one", projected[0]["content"])
        self.assertLessEqual(projected[0]["tokenEstimate"], 20_000)
        self.assertEqual(projected[0]["tokenBudget"], 20_000)
        self.assertTrue(projected[0]["truncated"])

    def test_working_context_reinjects_only_the_latest_bounded_trace(self):
        session = core.create_empty_session(
            cwd="/tmp/workspace",
            provider="openai",
            model="gpt-5.4",
            effort="high",
            systemPrompt="s",
            permissionMode="default",
        )
        previous = {
            "preservedTrace": [
                {"sourceRange": {"generation": 1}, "content": "old-one"},
                {"sourceRange": {"generation": 2}, "content": "old-two"},
            ]
        }
        working_fields = {
            "project": {"description": "test", "facts": None, "keyFiles": []},
            "goals": {"active": ["continue"], "recentRequests": [], "constraints": [], "preferences": []},
            "currentState": {"completed": [], "inProgress": [], "blocked": []},
            "decisions": [],
            "discoveries": [],
            "activity": {"commands": [], "searches": [], "web": [], "failures": []},
            "nextSteps": [],
            "criticalContext": [],
        }
        source_range = {
            "generation": 3,
            "start": 20,
            "end": 40,
            "messageCount": 20,
            "createdAt": "2026-07-20T17:01:07.533Z",
        }

        document = core.build_working_context_document_from_candidate(
            {
                "previous": previous,
                "session": session,
                "generation": 3,
                "createdAt": "2026-07-20T17:01:07.533Z",
                "sourceRange": source_range,
                "candidate": {
                    "qualityClassification": "usable",
                    "qualityReason": "bounded",
                    "workingContext": working_fields,
                    "model": {"provider": "openai", "model": "gpt-5.6-terra", "effort": "medium"},
                },
                "preservedTraceEvidence": {
                    "entryCount": 1,
                    "includedEntryCount": 1,
                    "omittedEntryCount": 0,
                    "tokenEstimate": 30_000,
                    "tokenBudget": 30_000,
                    "truncated": False,
                    "transcript": "x" * 120_000,
                },
            }
        )

        self.assertEqual(len(document["preservedTrace"]), 1)
        trace = document["preservedTrace"][0]
        self.assertEqual(trace["sourceRange"]["generation"], 3)
        self.assertNotIn("old-one", trace["content"])
        self.assertLessEqual(trace["tokenEstimate"], 20_000)
        self.assertEqual(trace["tokenBudget"], 20_000)
        self.assertTrue(trace["truncated"])

    def test_malformed_synthesis_uses_evidence_fallback_and_accounts_for_all_calls(self):
        root, _home, workspace, previous = self.temp_context()

        class MalformedSynthesisClient:
            def __init__(self):
                self.calls = []

            async def complete(self, args):
                self.calls.append(args["model"]["purpose"])
                if args["model"]["purpose"] == "synthesis":
                    return {
                        "text": '{"shouldCheckpoint":true,"quality":{"classification":"rich"',
                        "usage": {"input_tokens": 10, "output_tokens": 20},
                        "model": args["model"],
                    }
                return {
                    "text": json.dumps(
                        {
                            "verdict": "accept",
                            "confidence": "medium",
                            "reason": "Evidence-backed fallback preserves the source trace.",
                            "issues": [],
                            "notes": ["Synthesis JSON fallback was used."],
                        }
                    ),
                    "usage": {"input_tokens": 11, "output_tokens": 21},
                    "model": args["model"],
                }

        try:
            session = core.create_empty_session(
                cwd=str(workspace),
                provider="openai",
                model="gpt-5.4",
                effort="high",
                systemPrompt="s",
                permissionMode="default",
            )
            for index in range(34):
                role = "user" if index % 2 == 0 else "assistant"
                session["messages"].append({"role": role, "content": f"{role} message {index}"})
            client = MalformedSynthesisClient()

            result = run(
                core.run_compaction(
                    {
                        "session": session,
                        "config": {"durableMemory": {"enabled": False}},
                        "trigger": "auto_hard_limit",
                        "force": True,
                        "modelClient": client,
                    }
                )
            )

            self.assertEqual(result["status"], "completed")
            self.assertEqual(client.calls, ["synthesis", "synthesis", "synthesis", "verification"])
            self.assertEqual(session["metadata"]["usage"]["inputTokens"], 41)
            self.assertEqual(session["metadata"]["usage"]["outputTokens"], 81)
            working = json.loads(Path(result["artifacts"]["workingContext"]).read_text())
            self.assertEqual(len(working["preservedTrace"]), 1)
            self.assertIn("Preserved trace is authoritative", "\n".join(working["criticalContext"]))
            self.assertIn("malformed JSON", "\n".join(working["activity"]["failures"]))
            state = json.loads(Path(result["artifacts"]["state"]).read_text())
            self.assertIn("shouldCheckpoint", state["synthesis"]["rawResponsePreview"])
        finally:
            self.restore_home(root, previous)

    def test_auto_compaction_skips_incremental_rewrite_that_cannot_relieve_threshold(self):
        root, _home, workspace, previous = self.temp_context()

        class UnexpectedClient:
            async def complete(self, _args):
                raise AssertionError("A non-beneficial incremental compaction must not call a model.")

        try:
            session = core.create_empty_session(
                cwd=str(workspace),
                provider="openai",
                model="gpt-5.4",
                effort="high",
                systemPrompt="s",
                permissionMode="default",
            )
            session["messages"] = [
                {"role": "user" if index % 2 == 0 else "assistant", "content": f"message {index}"}
                for index in range(50)
            ]
            session["metadata"]["compactionState"] = {
                "latestCompactionId": "checkpoint-1",
                "latestCompactionGeneration": 1,
                "latestCompactionAt": "2026-07-20T17:01:07.533Z",
                "compactedThroughMessageIndex": 20,
                "archiveMessageCount": 40,
                "rawTokenEstimate": 190_000,
                "compactedContextTokenEstimate": 100_000,
                "workingContextTokenEstimate": 130_000,
                "artifacts": {},
            }

            result = run(
                core.run_compaction(
                    {
                        "session": session,
                        "config": {},
                        "trigger": "auto_hard_limit",
                        "currentInputTokens": 226_000,
                        "compactionThresholdTokens": 200_000,
                        "modelClient": UnexpectedClient(),
                    }
                )
            )

            self.assertEqual(result["status"], "skipped")
            self.assertEqual(result["reason"], "insufficient_incremental_relief")
            self.assertGreater(result["candidateCutoff"], result["previousCutoff"])
            self.assertLess(result["targetEstimatedRelief"], 26_000)
        finally:
            self.restore_home(root, previous)
