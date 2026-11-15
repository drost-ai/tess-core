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
