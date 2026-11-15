from __future__ import annotations

import copy

from tests.support.tess_core_case import *


class ContextCompactionIsolationTests(TessCoreTestCase):
    def test_prompt_context_accepts_only_session_owned_working_context(self):
        root, home, workspace, previous = self.temp_context()
        try:
            session = core.create_empty_session(
                cwd=str(workspace),
                provider="openai",
                model="gpt-5.4",
                effort="high",
                systemPrompt="s",
                permissionMode="default",
                sessionId="owned-compaction-context",
            )
            compactions_dir = Path(core.get_session_compactions_dir(session["id"]))
            compactions_dir.mkdir(parents=True)
            outside_path = home / "outside-working-context.json"

            def document(session_id):
                return {
                    "version": "v2",
                    "sessionId": session_id,
                    "session": {
                        "cwd": str(workspace),
                        "provider": "openai",
                        "model": "gpt-5.4",
                        "permissionMode": "default",
                    },
                    "project": {
                        "description": "Owned compaction authority",
                        "keyFiles": [],
                    },
                    "goals": {
                        "active": ["Preserve owned-context marker"],
                        "recentRequests": [],
                        "constraints": [],
                        "preferences": [],
                    },
                    "currentState": {
                        "completed": [],
                        "inProgress": [],
                        "blocked": [],
                    },
                    "decisions": [],
                    "discoveries": [],
                    "todos": {"open": [], "completed": []},
                    "activity": {
                        "commands": [],
                        "searches": [],
                        "web": [],
                        "failures": [],
                    },
                    "preservedTrace": [],
                    "nextSteps": [],
                    "criticalContext": [],
                }

            owned_path = compactions_dir / "working-context-owned.json"
            foreign_path = compactions_dir / "working-context-foreign.json"
            summary_path = compactions_dir / "summary.md"
            owned_path.write_text(json.dumps(document(session["id"])))
            foreign_path.write_text(json.dumps(document("foreign-session")))
            outside_path.write_text(json.dumps(document(session["id"])))
            summary_path.write_text("# Untrusted fallback\n\nsummary fallback marker\n")

            def prompt_for(working_context):
                candidate = copy.deepcopy(session)
                candidate["metadata"]["compactionState"] = {
                    "artifacts": {
                        "workingContext": str(working_context),
                        "summary": str(summary_path),
                    }
                }
                return run(core.build_compaction_system_prompt_context(candidate))

            owned = prompt_for(owned_path)
            self.assertIn("owned-context marker", owned)
            self.assertIsNone(
                prompt_for(compactions_dir / "missing-working-context.json")
            )
            self.assertIsNone(prompt_for(foreign_path))
            self.assertIsNone(prompt_for(outside_path))
        finally:
            self.restore_home(root, previous)


if __name__ == "__main__":
    unittest.main()
