from __future__ import annotations

import json
import os
import subprocess
import unittest

import agent_core as core
from tests.support.agent_core_case import AgentCoreTestCase, run


def _init_repo(workspace: str) -> None:
    subprocess.run(["git", "init", "-q"], cwd=workspace, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "init"], cwd=workspace, check=True)


class SubagentWorktreeIsolationTests(AgentCoreTestCase):
    def test_worktree_child_passes_startup_authority_enforcement(self):
        """Bug regression: the isolation worktree is created as a SIBLING of
        the repo, outside every parent authority root, and the child's derived
        authority never included it - so the worker died at startup with
        child_authority_escalation after littering a worktree and branch."""
        root, home, workspace, previous = self.temp_context()
        try:
            _init_repo(str(workspace))
            config = run(core.resolveConfig({"provider": "openai", "runtimeHome": str(home), "permissionMode": "bypassPermissions"}, str(workspace)))
            parent = core.create_empty_session(cwd=str(workspace), provider=config["provider"], providerProfileName=config["providerProfileName"], model=config["model"], effort=config["effort"], systemPrompt=config["systemPrompt"], permissionMode="bypassPermissions")
            run(core.save_session(parent))

            async def fake_start(task_args):
                return {"taskId": "wt-task", "outputFile": str(workspace / "out.log")}

            result = run(
                next(tool for tool in core.createBuiltinTools() if tool.name == "Agent").handler(
                    {"description": "Worktree child", "prompt": "go", "isolation": "worktree"},
                    {"session": parent, "config": config, "interactive": False, "askQuestion": lambda *_: "", "outputJson": lambda *_: None, "cwd": str(workspace), "persistSession": lambda: None, "taskSupervisor": {"startSubagentTask": fake_start}},
                )
            )
            payload = json.loads(result["content"])
            worktree_path = payload["worktree_path"]
            self.assertTrue(os.path.isdir(worktree_path))
            self.assertNotEqual(
                os.path.realpath(worktree_path), os.path.realpath(str(workspace))
            )

            child_session = run(core.load_session(payload["session_id"]))
            self.assertEqual(
                os.path.realpath(child_session["cwd"]), os.path.realpath(worktree_path)
            )
            authority = child_session["metadata"]["effectiveAgentAuthority"]
            granted = os.path.realpath(worktree_path)
            self.assertIn(granted, authority["readableRoots"])
            self.assertIn(granted, authority["writableRoots"])

            # The exact check the worker runs at startup - this raised
            # child_authority_escalation before the fix.
            child_config = dict(config)
            enforced = run(
                core.enforceSessionAgentAuthority(
                    session=child_session, config=child_config
                )
            )
            self.assertIsNotNone(enforced)
            self.assertIn(granted, child_config["allowedRoots"])
        finally:
            self.restore_home(root, previous)


if __name__ == "__main__":
    unittest.main()
