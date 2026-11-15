from __future__ import annotations

import json
import shutil
import subprocess
import time

from tests.support.tess_core_case import *
import tess_core.sandbox as sandbox_module
import tess_core.tools.processes as processes_module


class SandboxTests(TessCoreTestCase):
    def test_backend_selection_and_wrapper_shapes(self):
        config = {"sandbox": {"mode": "auto"}}
        darwin = core.resolve_sandbox_backend(config, platform="darwin", which=lambda name: "/usr/bin/sandbox-exec" if name == "sandbox-exec" else None)
        linux = core.resolve_sandbox_backend(config, platform="linux", which=lambda name: "/usr/bin/bwrap" if name == "bwrap" else None)
        missing = core.resolve_sandbox_backend(config, platform="linux", which=lambda _name: None)

        self.assertEqual(darwin.name, "seatbelt")
        self.assertEqual(linux.name, "bubblewrap")
        self.assertEqual(missing.enforcement, "none")

        seatbelt = darwin.wrap(["sh", "-lc", "echo ok"], {"writableRoots": ["/tmp/work"], "allowNetwork": False})
        self.assertEqual(seatbelt[:2], ["sandbox-exec", "-p"])
        self.assertIn('(subpath "/tmp/work")', seatbelt[2])
        self.assertNotIn("(allow network*)", seatbelt[2])

        bubblewrap = linux.wrap(["sh", "-lc", "echo ok"], {"writableRoots": ["/tmp/work"], "allowNetwork": False})
        self.assertEqual(bubblewrap[:4], ["bwrap", "--die-with-parent", "--ro-bind", "/"])
        self.assertIn("--unshare-net", bubblewrap)
        self.assertEqual(bubblewrap[-4:], ["--", "sh", "-lc", "echo ok"])

    def test_landlock_shim_is_gone_and_os_required_never_degrades(self):
        landlock = core.resolve_sandbox_backend(
            {"sandbox": {"mode": "auto", "backend": "landlock"}}, platform="linux", which=lambda _name: None
        )
        self.assertEqual(landlock.enforcement, "none")
        self.assertIn("removed", landlock.reason)

        with self.assertRaises(core.SandboxUnavailableError):
            core.prepare_sandboxed_argv(
                ["sh", "-lc", "echo never"],
                config={"sandbox": {"mode": "os-required", "backend": "landlock"}},
                cwd=os.getcwd(),
            )

        # Any enforcement below mandatory OS isolation refuses under
        # os-required; in auto mode it still wraps but downgrades loudly.
        class AdvisoryBackend(core.SandboxBackend):
            def __init__(self):
                super().__init__("advisory-test", "advisory", "advisory only")

            def wrap(self, argv, policy):
                return ["advisory-wrapper", *argv]

        with mock.patch.object(sandbox_module, "resolve_sandbox_backend", return_value=AdvisoryBackend()):
            with self.assertRaises(core.SandboxUnavailableError):
                core.prepare_sandboxed_argv(
                    ["sh", "-lc", "echo never"],
                    config={"sandbox": {"mode": "os-required"}},
                    cwd=os.getcwd(),
                )
            events = []
            advisory = core.prepare_sandboxed_argv(
                ["sh", "-lc", "echo ok"],
                config={"sandbox": {"mode": "auto"}},
                cwd=os.getcwd(),
                emit=lambda event, data: events.append((event, data)),
            )
        self.assertEqual(advisory["argv"][0], "advisory-wrapper")
        downgrade = next(data for event, data in events if event == "sandbox.downgraded")
        self.assertEqual(downgrade["actual"], "advisory")

    def test_resolve_config_defaults_sandbox_off_and_accepts_overrides(self):
        root, home, workspace, previous = self.temp_context()
        try:
            default_config = run(core.resolve_config({"provider": "openai", "runtimeHome": str(home)}, str(workspace)))
            self.assertEqual(default_config["sandbox"], {"mode": "off"})

            config = run(
                core.resolve_config(
                    {
                        "provider": "openai",
                        "runtimeHome": str(home),
                        "sandbox": {"mode": "auto", "backend": "none", "writableRoots": ["out"], "allowNetwork": True},
                    },
                    str(workspace),
                )
            )
            self.assertEqual(config["sandbox"]["mode"], "auto")
            self.assertEqual(config["sandbox"]["backend"], "none")
            self.assertEqual(config["sandbox"]["writableRoots"], [os.path.realpath(workspace / "out")])
            self.assertTrue(config["sandbox"]["allowNetwork"])
        finally:
            self.restore_home(root, previous)

    def test_bash_auto_mode_downgrades_loudly_when_backend_missing(self):
        root, home, workspace, previous = self.temp_context()
        try:
            config, session, bash, events = self._bash_fixture(home, workspace, {"mode": "auto", "backend": "none"})
            result = run(bash.handler({"command": "echo sandbox-ok"}, self._context(session, config, workspace, events)))
            payload = json.loads(result["content"])

            self.assertEqual(payload["exitCode"], 0)
            self.assertIn("sandbox-ok", payload["stdout"])
            downgrade = next(data for event, data in events if event == "sandbox.downgraded")
            self.assertEqual(downgrade["requested"], "auto")
            self.assertEqual(downgrade["actual"], "none")
        finally:
            self.restore_home(root, previous)

    def test_bash_os_required_fails_closed_without_backend(self):
        root, home, workspace, previous = self.temp_context()
        try:
            config, session, bash, events = self._bash_fixture(home, workspace, {"mode": "os-required", "backend": "none"})
            with self.assertRaises(core.SandboxUnavailableError):
                run(bash.handler({"command": "echo should-not-run"}, self._context(session, config, workspace, events)))
            self.assertFalse(any(event.startswith("sandbox.") for event, _ in events))
        finally:
            self.restore_home(root, previous)

    def test_seatbelt_blocks_write_outside_writable_root_when_available(self):
        if not shutil.which("sandbox-exec"):
            self.skipTest("sandbox-exec unavailable")
        root, home, workspace, previous = self.temp_context()
        outside = workspace.parent / "outside.txt"
        try:
            events = []
            launch = core.prepare_sandboxed_argv(
                ["/bin/sh", "-c", f"touch inside.txt; touch {outside}"],
                config={"sandbox": {"mode": "os-required", "backend": "seatbelt"}, "runtimeHome": str(home)},
                cwd=str(workspace),
                emit=lambda event, data: events.append((event, data)),
            )
            result = subprocess.run(launch["argv"], cwd=workspace, text=True, capture_output=True, check=False)
            if result.returncode == 71 and "sandbox_apply: Operation not permitted" in result.stderr:
                self.skipTest("nested sandbox-exec is denied by the outer test environment")

            self.assertNotEqual(result.returncode, 0)
            self.assertTrue((workspace / "inside.txt").exists())
            self.assertFalse(outside.exists())
            self.assertTrue(any(event == "sandbox.applied" for event, _ in events))
        finally:
            self.restore_home(root, previous)

    def test_local_background_bash_inherits_sandbox_boundary(self):
        root, home, workspace, previous = self.temp_context()
        try:
            config, session, bash, events = self._bash_fixture(home, workspace, {"mode": "auto", "backend": "none"})
            result = run(
                bash.handler(
                    {"command": "printf bg-ok", "run_in_background": True},
                    self._context(session, config, workspace, events),
                )
            )
            payload = json.loads(result["content"])
            task_id = payload["task_id"]
            meta = run(core.read_task_meta(task_id))
            run(processes_module._wait_for_tracked_background_runner(meta["runnerPid"]))

            self.assertIn("bg-ok", self._wait_for_output(payload["output_file"]))
            self.assertTrue(any(event == "sandbox.downgraded" for event, _ in events))
        finally:
            self.restore_home(root, previous)

    def test_supervisor_background_bash_emits_sandbox_events(self):
        root, home, workspace, previous = self.temp_context()
        try:
            events = []
            supervisor = run(core.create_task_supervisor({"cwd": str(workspace), "onEvent": lambda payload: events.append(payload)}))
            session = core.create_empty_session(cwd=str(workspace), provider="openai", model="gpt-5.4", systemPrompt="s", permissionMode="bypassPermissions")
            run(core.save_session(session))
            started = run(
                supervisor["startBackgroundCommand"](
                    {
                        "sessionId": session["id"],
                        "command": "printf supervisor-ok",
                        "cwd": str(workspace),
                        "sandbox": {"mode": "auto", "backend": "none"},
                        "runtimeHome": str(home),
                    }
                )
            )
            output = run(supervisor["read_task_output"](started["taskId"], 12000))

            self.assertTrue(any(payload["event"] == "sandbox.downgraded" for payload in events))
            self.assertIn("supervisor-ok", self._wait_for_output(output["outputFile"]))
        finally:
            self.restore_home(root, previous)

    def _bash_fixture(self, home, workspace, sandbox):
        config = run(core.resolve_config({"provider": "openai", "runtimeHome": str(home), "permissionMode": "bypassPermissions", "sandbox": sandbox}, str(workspace)))
        session = core.create_empty_session(cwd=str(workspace), provider=config["provider"], providerProfileName=config["providerProfileName"], model=config["model"], effort=config["effort"], systemPrompt=config["systemPrompt"], permissionMode="bypassPermissions")
        bash = next(tool for tool in core.create_builtin_tools() if tool.name == "Bash")
        return config, session, bash, []

    def _context(self, session, config, workspace, events):
        return {"session": session, "config": config, "interactive": False, "askQuestion": lambda *_: "", "outputJson": lambda event, data: events.append((event, data)), "cwd": str(workspace), "persist_session": lambda: None}

    def _wait_for_output(self, path: str) -> str:
        for _ in range(50):
            text = Path(path).read_text(encoding="utf-8", errors="replace") if Path(path).exists() else ""
            if text:
                return text
            time.sleep(0.05)
        return ""


if __name__ == "__main__":
    unittest.main()
