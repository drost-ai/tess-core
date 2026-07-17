from __future__ import annotations

from tests.support.agent_core_case import *

from agent_core_harness.sandbox import ScenarioSandbox


class HarnessSandboxTests(AgentCoreTestCase):
    def test_sandbox_creates_paths_files_and_restores_environment(self):
        with tempfile.TemporaryDirectory() as tmp:
            previous = os.environ.get("AGENT_CORE_HOME")
            previous_git_ceiling = os.environ.get("GIT_CEILING_DIRECTORIES")
            sandbox = ScenarioSandbox("smoke.case", runs_dir=Path(tmp)).create()
            try:
                sandbox.apply_setup({"workspace": {"directories": ["src"], "files": {"src/app.py": "print('ok')\n"}}})
                self.assertTrue(sandbox.home.exists())
                self.assertTrue((sandbox.workspace / "src" / "app.py").exists())
                self.assertEqual(os.environ["AGENT_CORE_HOME"], str(sandbox.home))
                self.assertEqual(os.environ["GIT_CEILING_DIRECTORIES"], str(sandbox.run_dir))
                self.assertEqual(sandbox.paths_dict()["workspace"], str(sandbox.workspace))
                sandbox.cleanup_sandbox(passed=False)
                self.assertTrue(sandbox.run_dir.exists())
                self.assertEqual(os.environ.get("AGENT_CORE_HOME"), previous)
                self.assertEqual(os.environ.get("GIT_CEILING_DIRECTORIES"), previous_git_ceiling)
            finally:
                sandbox.cleanup_sandbox(passed=False)

    def test_empty_process_cleanup_is_safe_and_success_cleanup_removes_run_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            sandbox = ScenarioSandbox("smoke.case", runs_dir=Path(tmp), cleanup="success").create()
            run_dir = sandbox.run_dir
            sandbox.cleanup_sandbox(passed=True)
            self.assertFalse(run_dir.exists())
