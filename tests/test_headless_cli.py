from __future__ import annotations

from tests.support.tess_core_case import *


class HeadlessCliTests(TessCoreTestCase):
    def _run_cli(self, args, *, home, workspace, responses=None, error=None):
        env = os.environ.copy()
        env["TESS_CORE_HOME"] = str(home)
        env["PYTHONPATH"] = str(Path.cwd()) if not env.get("PYTHONPATH") else f"{Path.cwd()}{os.pathsep}{env['PYTHONPATH']}"
        if responses is not None:
            env["TESS_CORE_HEADLESS_PROVIDER_RESPONSES"] = json.dumps(responses)
        if error is not None:
            env["TESS_CORE_HEADLESS_PROVIDER_ERROR"] = error
        return subprocess.run(
            [sys.executable, "-m", "tess_core", "run", *args],
            cwd=str(workspace),
            env=env,
            text=True,
            capture_output=True,
            timeout=20,
        )

    def _context(self):
        root, home, workspace, previous = self.temp_context()
        workspace.joinpath("README.md").write_text("hello from headless cli\n", encoding="utf-8")
        return root, home, workspace, previous

    def test_stream_json_reuses_runtime_event_envelope(self):
        root, home, workspace, previous = self._context()
        try:
            responses = [
                {
                    "model": "gpt-5.4",
                    "stopReason": "tool_use",
                    "usage": {"input_tokens": 10, "output_tokens": 3},
                    "content": [{"type": "tool_use", "id": "read_1", "name": "Read", "input": {"file_path": "README.md"}}],
                },
                {
                    "model": "gpt-5.4",
                    "stopReason": "end_turn",
                    "usage": {"input_tokens": 8, "output_tokens": 5},
                    "content": [{"type": "text", "text": "README says hello from headless cli."}],
                },
            ]
            proc = self._run_cli(
                [
                    "-p",
                    "Read README.md.",
                    "--output-format",
                    "stream-json",
                    "--runtime-home",
                    str(home),
                    "--cwd",
                    str(workspace),
                    "--provider",
                    "openai",
                    "--model",
                    "gpt-5.4",
                    "--permission-mode",
                    "bypassPermissions",
                    "--tools",
                    "Read",
                    "--allowed-tools",
                    "Read",
                ],
                home=home,
                workspace=workspace,
                responses=responses,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            events = [json.loads(line) for line in proc.stdout.splitlines()]
            self.assertEqual(events[0]["event"], "system.init")
            self.assertEqual(events[0]["payload"]["streamVersion"], 1)
            self.assertEqual(events[-1]["event"], "result")
            self.assertTrue(events[-1]["payload"]["success"])
            names = [event["event"] for event in events]
            for event in ("session.activated", "turn.started", "message.user", "tool.started", "tool.completed", "message.assistant", "turn.completed"):
                self.assertIn(event, names)
            self.assertEqual(events[-1]["payload"]["text"], "README says hello from headless cli.")
        finally:
            self.restore_home(root, previous)

    def test_json_output_is_single_final_object(self):
        root, home, workspace, previous = self._context()
        try:
            proc = self._run_cli(
                ["-p", "Say hi.", "--output-format", "json", "--runtime-home", str(home), "--cwd", str(workspace), "--provider", "openai", "--model", "gpt-5.4"],
                home=home,
                workspace=workspace,
                responses={"model": "gpt-5.4", "stopReason": "end_turn", "usage": {"input_tokens": 1, "output_tokens": 2}, "content": [{"type": "text", "text": "hi"}]},
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(len(proc.stdout.splitlines()), 1)
            payload = json.loads(proc.stdout)
            self.assertTrue(payload["success"])
            self.assertEqual(payload["text"], "hi")
        finally:
            self.restore_home(root, previous)

    def test_text_output_prints_assistant_text_only(self):
        root, home, workspace, previous = self._context()
        try:
            proc = self._run_cli(
                ["-p", "Say hi.", "--runtime-home", str(home), "--cwd", str(workspace), "--provider", "openai", "--model", "gpt-5.4"],
                home=home,
                workspace=workspace,
                responses={"model": "gpt-5.4", "stopReason": "end_turn", "usage": {}, "content": [{"type": "text", "text": "plain hi"}]},
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(proc.stdout, "plain hi\n")
        finally:
            self.restore_home(root, previous)

    def test_provider_error_streams_failed_result_and_nonzero_exit(self):
        root, home, workspace, previous = self._context()
        try:
            proc = self._run_cli(
                ["-p", "fail", "--output-format", "stream-json", "--runtime-home", str(home), "--cwd", str(workspace), "--provider", "openai", "--model", "gpt-5.4"],
                home=home,
                workspace=workspace,
                error="scripted boom",
            )
            self.assertEqual(proc.returncode, 1)
            events = [json.loads(line) for line in proc.stdout.splitlines()]
            self.assertEqual(events[-1]["event"], "result")
            self.assertFalse(events[-1]["payload"]["success"])
            self.assertIn("scripted boom", events[-1]["payload"]["error"])
        finally:
            self.restore_home(root, previous)

    def test_missing_credential_is_a_failed_run(self):
        root, home, workspace, previous = self._context()
        try:
            env = {k: v for k, v in os.environ.items() if not k.startswith(("ANTHROPIC_", "OPENAI_", "TESS_CORE_HEADLESS_"))}
            env["TESS_CORE_HOME"] = str(home)
            env["PYTHONPATH"] = str(Path.cwd())
            proc = subprocess.run(
                [sys.executable, "-m", "tess_core", "run", "-p", "hi", "--output-format", "json", "--runtime-home", str(home), "--cwd", str(workspace), "--provider", "anthropic"],
                cwd=str(workspace), env=env, text=True, capture_output=True, timeout=20,
            )
            self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
            final = json.loads(proc.stdout.splitlines()[-1])
            self.assertFalse(final["success"])
            self.assertEqual(final["stopReason"], "error")
            self.assertIn("No credential is configured", final["error"])
            self.assertNotIn("tess auth", final["error"])
        finally:
            self.restore_home(root, previous)

    def _registry_run(self, registry_text, extra_args, extra_env):
        root, home, workspace, previous = self._context()
        try:
            (workspace / "providers.yaml").write_text(registry_text, encoding="utf-8")
            env = {k: v for k, v in os.environ.items() if not k.startswith(("ANTHROPIC_", "OPENAI_", "TESS_"))}
            env.update({"TESS_CORE_HOME": str(home), "PYTHONPATH": str(Path.cwd()), **extra_env})
            env["TESS_CORE_HEADLESS_PROVIDER_RESPONSES"] = json.dumps(
                [{"model": "m", "stopReason": "end_turn", "usage": {"input_tokens": 1, "output_tokens": 1}, "content": [{"type": "text", "text": "ok"}]}]
            )
            return subprocess.run(
                [sys.executable, "-m", "tess_core", "run", "-p", "hi", "--output-format", "json", "--no-session-persistence", *extra_args],
                cwd=str(workspace), env=env, text=True, capture_output=True, timeout=20,
            )
        finally:
            self.restore_home(root, previous)

    _ONE_PROFILE = "schema_version: 1\nproviderProfiles:\n  mine:\n    kind: openai\n    apiMode: responses\n    defaultModel: m\n    auth:\n      kind: openai_api_key\n      envVar: MY_KEY\n"
    _SECOND_PROFILE = "  other:\n    kind: anthropic\n    defaultModel: m2\n    auth:\n      kind: anthropic_api_key\n      envVar: OTHER_KEY\n"

    def test_provider_registry_flag_uses_only_the_registry(self):
        # A relative path, one profile and no --provider: the profile is selected.
        proc = self._registry_run(self._ONE_PROFILE, ["--provider-registry", "providers.yaml"], {"MY_KEY": "k"})
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(json.loads(proc.stdout.splitlines()[-1])["text"], "ok")
        # Its credential is the declared variable and nothing else.
        proc = self._registry_run(self._ONE_PROFILE, ["--provider-registry", "providers.yaml"], {"OPENAI_API_KEY": "ambient"})
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("Missing credential for profile 'mine'", proc.stderr)
        # Built-in profiles do not exist beside a registry.
        proc = self._registry_run(self._ONE_PROFILE, ["--provider-registry", "providers.yaml", "--provider", "openai"], {"MY_KEY": "k"})
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)

    def test_provider_registry_flag_asks_which_of_several_profiles(self):
        both = self._ONE_PROFILE + self._SECOND_PROFILE
        proc = self._registry_run(both, ["--provider-registry", "providers.yaml"], {"MY_KEY": "k"})
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        self.assertIn("choose one with --provider: mine, other", proc.stderr)
        proc = self._registry_run(both, ["--provider-registry", "providers.yaml", "--provider", "mine"], {"MY_KEY": "k"})
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

    def test_provider_auth_path_flag_supplies_a_codex_sign_in(self):
        registry = "schema_version: 1\nproviderProfiles:\n  codex:\n    kind: openai\n    apiMode: codex\n    defaultModel: m\n    auth:\n      kind: openai_api_key_or_codex\n"
        proc = self._registry_run(registry, ["--provider-registry", "providers.yaml"], {})
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("name a Codex auth file", proc.stderr)
        with tempfile.TemporaryDirectory() as folder:
            auth = Path(folder) / "auth.json"
            auth.write_text(json.dumps({"tokens": {"access_token": "codex-token"}}), encoding="utf-8")
            proc = self._registry_run(registry, ["--provider-registry", "providers.yaml", "--provider-auth-path", str(auth)], {})
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            proc = self._registry_run(registry, ["--provider-auth-path", str(auth)], {})
            self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
            self.assertIn("--provider-auth-path needs --provider-registry", proc.stderr)

    def test_resumed_session_keeps_its_profile_without_repeating_the_flags(self):
        root, home, workspace, previous = self._context()
        try:
            (workspace / "providers.yaml").write_text(self._ONE_PROFILE, encoding="utf-8")
            env = {k: v for k, v in os.environ.items() if not k.startswith(("ANTHROPIC_", "OPENAI_", "TESS_"))}
            env.update({"TESS_CORE_HOME": str(home), "PYTHONPATH": str(Path.cwd()), "MY_KEY": "k"})
            env["TESS_CORE_HEADLESS_PROVIDER_RESPONSES"] = json.dumps(
                [{"model": "m", "stopReason": "end_turn", "usage": {"input_tokens": 1, "output_tokens": 1}, "content": [{"type": "text", "text": "ok"}]}]
            )

            def cli(*args):
                return subprocess.run(
                    [sys.executable, "-m", "tess_core", "run", *args], cwd=str(workspace), env=env, text=True, capture_output=True, timeout=20,
                )

            first = cli("-p", "one", "--provider-registry", "providers.yaml", "--output-format", "json")
            self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
            session_id = json.loads(first.stdout.splitlines()[-1])["sessionId"]

            # Neither --provider nor --provider-registry is repeated.
            resumed = cli("-p", "two", "--session", session_id, "--output-format", "stream-json")
            self.assertEqual(resumed.returncode, 0, resumed.stdout + resumed.stderr)
            init = next(json.loads(line)["data"] for line in resumed.stdout.splitlines() if '"system.init"' in line)
            self.assertEqual((init["session_id"], init["provider_profile"], init["model"]), (session_id, "mine", "m"))
        finally:
            self.restore_home(root, previous)

    def test_tool_flags_reject_permission_rules(self):
        root, home, workspace, previous = self._context()
        try:
            proc = self._run_cli(
                ["-p", "x", "--runtime-home", str(home), "--cwd", str(workspace), "--allowed-tools", "Bash(touch x)"],
                home=home,
                workspace=workspace,
                responses=[{"model": "gpt-5.4", "stopReason": "end_turn", "usage": {"input_tokens": 1, "output_tokens": 1}, "content": [{"type": "text", "text": "ok"}]}],
            )
            self.assertEqual(proc.returncode, 2)
            self.assertEqual(proc.stdout, "")
            self.assertIn("--allowed-tools takes tool names", proc.stderr)
            self.assertIn("permissions.allow", proc.stderr)
        finally:
            self.restore_home(root, previous)

    def test_default_permission_mode_denies_gated_tool_without_hanging(self):
        root, home, workspace, previous = self._context()
        try:
            responses = [
                {
                    "model": "gpt-5.4",
                    "stopReason": "tool_use",
                    "usage": {},
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "bash_1",
                            "name": "Bash",
                            "input": {"command": f"{shlex.quote(sys.executable)} -c {shlex.quote('print(123)')}"},
                        }
                    ],
                },
                {"model": "gpt-5.4", "stopReason": "end_turn", "usage": {}, "content": [{"type": "text", "text": "handled"}]},
            ]
            proc = self._run_cli(
                [
                    "-p",
                    "Run a command.",
                    "--output-format",
                    "stream-json",
                    "--runtime-home",
                    str(home),
                    "--cwd",
                    str(workspace),
                    "--provider",
                    "openai",
                    "--model",
                    "gpt-5.4",
                    "--tools",
                    "Bash",
                    "--allowed-tools",
                    "Bash",
                ],
                home=home,
                workspace=workspace,
                responses=responses,
            )
            self.assertEqual(proc.returncode, 2, proc.stderr)
            events = [json.loads(line) for line in proc.stdout.splitlines()]
            denied = [event for event in events if event["event"] == "permission.decided"]
            self.assertTrue(any(event["payload"]["decision"] == "deny" for event in denied))
            self.assertEqual(events[-1]["payload"]["stopReason"], "permission_denied")
        finally:
            self.restore_home(root, previous)

    def test_stream_redacts_large_media_payloads(self):
        from tess_core.headless import JsonStreamSink

        stream = io.StringIO()
        sink = JsonStreamSink(stream)
        sink.emit(
            "tool.completed",
            {"name": "Read", "output": {"type": "image", "base64": "A" * 4000, "filePath": "inbox/photo.png"}},
            default_session_id="session-1",
        )
        line = json.loads(stream.getvalue())
        rendered = json.dumps(line)
        self.assertNotIn("A" * 80, rendered)
        self.assertIn("<redacted-media-payload>", rendered)
        self.assertIn("inbox/photo.png", rendered)


if __name__ == "__main__":
    unittest.main()
