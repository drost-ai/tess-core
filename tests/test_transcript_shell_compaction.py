from __future__ import annotations

from tests.support.agent_core_case import *


class TranscriptShellCompactionTests(AgentCoreTestCase):
    def test_transcript_snapshot_matches_reference_shape(self):
        read_payload_a = {
            "type": "text",
            "file": {"filePath": "/tmp/a.py", "content": "print('a')", "numLines": 1, "startLine": 1, "totalLines": 1},
        }
        read_payload_b = {
            "type": "text",
            "file": {"filePath": "/tmp/b.py", "content": "print('b')", "numLines": 1, "startLine": 1, "totalLines": 1},
        }
        session = {
            "name": "Fallback Name",
            "metadata": {"assistantName": "Tess"},
            "messages": [
                {"role": "user", "content": "read both files"},
                {
                    "role": "assistant",
                    "content": [
                        {"type": "text", "text": "\nI'll read them.\n"},
                        {"type": "tool_use", "id": "status-1", "name": "SetStatus", "input": {"status": "reading"}},
                        {"type": "tool_use", "id": "read-1", "name": "Read", "input": {"file_path": "/tmp/a.py"}},
                    ],
                },
                {
                    "role": "user",
                    "content": [
                        {"type": "tool_result", "tool_use_id": "status-1", "content": "ignored"},
                        {"type": "tool_result", "tool_use_id": "read-1", "content": json.dumps(read_payload_a)},
                    ],
                },
                {
                    "role": "assistant",
                    "content": [{"type": "tool_use", "id": "read-2", "name": "Read", "input": {"file_path": "/tmp/b.py"}}],
                },
                {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "read-2", "content": read_payload_b}]},
            ],
        }

        entries = core.buildTranscriptSnapshot(session)
        self.assertEqual(entries[0], {"tone": "user", "title": "You", "body": "read both files", "timestamp": "saved"})
        self.assertEqual(entries[1], {"tone": "assistant", "title": "Tess", "body": "I'll read them.", "timestamp": "saved"})
        self.assertEqual(len([entry for entry in entries if entry.get("tone") == "tool"]), 1)
        tool_entry = entries[2]
        self.assertEqual(tool_entry["title"], "Read")
        self.assertEqual(tool_entry["payload"]["toolName"], "Read")
        self.assertEqual(tool_entry["payload"]["label"], "Read")
        self.assertEqual(tool_entry["payload"]["outputs"], [read_payload_a, read_payload_b])
        self.assertNotIn("ignored", tool_entry["body"])

    def test_transcript_adjacent_text_blocks_keep_paragraph_boundary(self):
        session = {
            "name": "Assistant",
            "messages": [
                {
                    "role": "assistant",
                    "content": [
                        {"type": "text", "text": "First update."},
                        {"type": "text", "text": "Second update."},
                    ],
                }
            ],
        }

        entries = core.buildTranscriptSnapshot(session)
        self.assertEqual(entries[0]["body"], "First update.\n\nSecond update.")

    def test_transcript_plan_compaction_and_bug_command_match_reference(self):
        plan_text = "Implement shell parity.\nRun the focused tests."
        session = {
            "id": "session-transcript",
            "cwd": "/tmp/work",
            "metadata": {"planMode": {"state": "pending", "plan": plan_text}},
            "messages": [
                {"role": "user", "content": "make a plan"},
                {
                    "role": "assistant",
                    "content": [
                        {"type": "text", "text": "Implement shell parity. Run the focused tests."},
                        {"type": "tool_use", "id": "plan-tool", "name": "ExitPlanMode", "input": {}},
                    ],
                },
                {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "plan-tool", "content": "pending"}]},
            ],
        }

        entries = core.buildTranscriptSnapshot(session)
        self.assertEqual([entry["title"] for entry in entries], ["You", "Plan"])
        self.assertEqual(entries[1]["body"], plan_text)
        self.assertTrue(all(entry.get("payload", {}).get("toolName") != "ExitPlanMode" for entry in entries))

        infos = []
        result = run(core.handleSlashCommand({"command": "/bug", "session": session, "config": {}, "hooks": {"info": lambda title, message: infos.append((title, message))}}))
        self.assertEqual(result["title"], "Feedback")
        self.assertIn("https://github.com/drost-ai/tess-core/issues", result["message"])
        self.assertEqual(infos[-1][0], "Feedback")

    def test_shell_compact_event_payload_matches_reference(self):
        session = {
            "id": "session-compact",
            "cwd": "/tmp/work",
            "metadata": {
                "compactionState": {
                    "workingContextTokenEstimate": 1234,
                    "workingContextTokenMeasurement": {"source": "fixture"},
                }
            },
        }
        config = {"provider": "openai"}
        events = []
        counters = {"persist": 0, "refresh": 0}

        def fake_compaction(payload):
            self.assertEqual(payload["trigger"], "manual")
            return {
                "status": "completed",
                "generation": 4,
                "trigger": "manual",
                "compactedThroughMessageIndex": 8,
                "compactedMessageCount": 9,
                "recentMessageCount": 3,
                "rawTokenEstimate": 5000,
                "workingContextTokenEstimate": 1500,
                "workingContextTokenMeasurement": {"source": "post-compact"},
                "qualityClassification": "rich",
                "compactionModel": {"provider": "openai", "model": "gpt-5.4"},
                "verificationModel": {"provider": "anthropic", "model": "claude"},
                "verification": {"verdict": "accept", "confidence": "high"},
                "artifacts": {
                    "summary": "/tmp/summary.md",
                    "state": "/tmp/state.json",
                    "workingContext": "/tmp/context.md",
                    "checkpoint": "/tmp/checkpoint.json",
                },
            }

        result = run(
            core.handleSlashCommand(
                {
                    "command": "/compact",
                    "session": session,
                    "config": config,
                    "hooks": {
                        "runCompaction": fake_compaction,
                        "emitRuntimeEvent": lambda event, data: events.append((event, data)),
                        "persist": lambda: counters.__setitem__("persist", counters["persist"] + 1),
                        "refresh": lambda: counters.__setitem__("refresh", counters["refresh"] + 1),
                    },
                }
            )
        )

        self.assertIn("Archive preserved.", result["message"])
        self.assertEqual(events[0][0], "compaction.started")
        self.assertEqual(events[0][1]["current_token_source"], "fixture")
        self.assertEqual(events[-1][0], "compaction.completed")
        completed = events[-1][1]
        self.assertEqual(completed["compacted_message_count"], 9)
        self.assertEqual(completed["working_context_token_source"], "post-compact")
        self.assertEqual(completed["compaction_provider"], "openai")
        self.assertEqual(completed["verification_verdict"], "accept")
        self.assertEqual(completed["checkpoint_path"], "/tmp/checkpoint.json")
        self.assertEqual(counters, {"persist": 1, "refresh": 1})
