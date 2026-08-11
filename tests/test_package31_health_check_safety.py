from __future__ import annotations

from tests.support.agent_core_case import *
from agent_core._utils import now_iso

from agent_core.tasks import health_check_executor as executor
from agent_core.tasks.health_check_policy import (
    authorize_health_check_spec,
    health_check_spec_digest,
    normalize_health_check_spec,
)
from agent_core.tasks.health_check_record import (
    build_authorized_health_check_record,
    persist_health_check_record,
)
from agent_core.tasks.health_check_runtime import _commit_attempt
from agent_core.tasks.terminal_store import prepare_task_stop


class Package31HealthCheckSafetyTests(AgentCoreTestCase):
    def _session(self, home: Path, workspace: Path, mode: str = "bypassPermissions"):
        config = run(
            core.resolveConfig(
                {
                    "provider": "openai",
                    "runtimeHome": str(home),
                    "permissionMode": mode,
                    "sandbox": {"mode": "off", "network": True},
                },
                str(workspace),
            )
        )
        session = core.create_empty_session(
            cwd=str(workspace),
            provider=config["provider"],
            providerProfileName=config["providerProfileName"],
            model=config["model"],
            effort=config["effort"],
            systemPrompt=config["systemPrompt"],
            permissionMode=mode,
        )
        run(core.save_session(session))
        return config, session

    def _context(self, config, session, daemon=None):
        return {
            "session": session,
            "config": config,
            "interactive": False,
            "askQuestion": lambda *_: "",
            "outputJson": lambda *_: None,
            "cwd": session["cwd"],
            "persist_session": lambda: None,
            **({"daemon": daemon} if daemon else {}),
        }

    def _command_spec(self, workspace: Path):
        return run(
            normalize_health_check_spec(
                {
                    "title": "command health",
                    "mode": "command",
                    "cwd": str(workspace),
                    "command": "printf ready",
                },
                str(workspace),
            )
        )

    def _request(self, session, spec, policy):
        return {
            "sessionId": session["id"],
            "spec": spec,
            "specDigest": health_check_spec_digest(spec),
            "executionPolicy": policy,
        }

    def test_strict_normalization_and_bounded_defaults(self):
        root, home, workspace, previous = self.temp_context()
        try:
            command = self._command_spec(workspace)
            self.assertEqual(command["outputLimitBytes"], 128 * 1024)
            self.assertEqual(command["bodyLimitBytes"], 256 * 1024)
            self.assertEqual(command["expectedBodyPolicy"], "ignore")
            invalid = [
                {"title": "x", "mode": "command", "command": ""},
                {"title": "x", "mode": "other", "command": "true"},
                {"title": "x", "mode": "http", "url": "http://example.test"},
                {
                    "title": "x",
                    "mode": "http",
                    "url": "http://example.test",
                    "expectedBodyPolicy": "ignore",
                    "expectedStatusCodes": [],
                },
                {
                    "title": "x",
                    "mode": "command",
                    "command": "true",
                    "timeoutMs": 60_001,
                },
                {
                    "title": "x",
                    "mode": "command",
                    "command": "true",
                    "intervalMs": 1000.5,
                },
                {
                    "title": "x",
                    "mode": "command",
                    "command": "true",
                    "url": "http://example.test",
                },
            ]
            for row in invalid:
                with self.subTest(row=row), self.assertRaises(ValueError):
                    run(normalize_health_check_spec(row, str(workspace)))
        finally:
            self.restore_home(root, previous)

    def test_tool_authorizes_before_daemon(self):
        root, home, workspace, previous = self.temp_context()
        try:
            config, session = self._session(home, workspace)
            calls = []

            async def start(payload):
                calls.append(payload)
                return {"taskId": "health-authorized"}

            tool = next(
                item for item in core.createBuiltinTools() if item.name == "TaskHealthCheckStart"
            )
            context = self._context(config, session, {"startHealthCheck": start})
            result = run(
                tool.handler(
                    {
                        "title": "http health",
                        "mode": "http",
                        "url": "http://example.test/health",
                        "expected_status_codes": [200],
                        "expected_body_policy": "ignore",
                    },
                    context,
                )
            )
            self.assertEqual(json.loads(result["content"])["task_id"], "health-authorized")
            self.assertEqual(calls[0]["specDigest"], health_check_spec_digest(calls[0]["spec"]))
            self.assertEqual(calls[0]["executionPolicy"]["allowedOrigins"], ["http://example.test"])

        finally:
            self.restore_home(root, previous)

    def test_direct_gateway_rejects_unauthorized_start_before_persistence(self):
        root, home, workspace, previous = self.temp_context()
        try:
            _config, session = self._session(home, workspace)

            async def scenario():
                supervisor = await core.create_task_supervisor({"cwd": str(workspace)})
                try:
                    with self.assertRaises(RuntimeError) as raised:
                        await supervisor["startHealthCheck"](
                            {
                                "sessionId": session["id"],
                                "title": "legacy bypass",
                                "mode": "command",
                                "command": "true",
                            }
                        )
                    self.assertEqual(
                        getattr(raised.exception, "code", None),
                        "health_check.authorization_required",
                    )
                    self.assertEqual(await core.list_health_check_tasks(), [])
                finally:
                    await supervisor["close"]()

            run(scenario())
        finally:
            self.restore_home(root, previous)

    def test_command_policy_redacts_environment_and_executor_reuses_authority(self):
        root, home, workspace, previous = self.temp_context()
        original = executor.runInlineShellCommand
        secret = os.environ.get("HEALTH_CHECK_TEST_SECRET")
        try:
            os.environ["HEALTH_CHECK_TEST_SECRET"] = "never-persist"
            config, session = self._session(home, workspace)
            context = self._context(config, session)
            spec = self._command_spec(workspace)
            policy = run(authorize_health_check_spec(context, spec))
            self.assertNotIn("HEALTH_CHECK_TEST_SECRET", policy["environment"])
            captured = {}

            async def fake_run(**kwargs):
                captured.update(kwargs)
                return {
                    "stdout": "ready",
                    "stderr": "",
                    "exitCode": 0,
                    "timedOut": False,
                    "aborted": False,
                    "outputLimitExceeded": False,
                    "stdoutBytes": 5,
                    "stderrBytes": 0,
                }

            executor.runInlineShellCommand = fake_run
            record = build_authorized_health_check_record(
                request=self._request(session, spec, policy), taskId="health-command-policy"
            )
            result = run(executor.execute_health_check(record=record, signal=asyncio.Event()))
            self.assertTrue(result["okay"])
            self.assertFalse(result["lastTimedOut"])
            self.assertFalse(result["lastTerminated"])
            self.assertEqual(captured["maxSpoolBytes"], 128 * 1024)
            self.assertEqual(captured["env"], policy["environment"])
            self.assertEqual(captured["argv"][-3:], [policy["shellPath"], "-lc", spec["command"]])
        finally:
            executor.runInlineShellCommand = original
            if secret is None:
                os.environ.pop("HEALTH_CHECK_TEST_SECRET", None)
            else:
                os.environ["HEALTH_CHECK_TEST_SECRET"] = secret
            self.restore_home(root, previous)

    def test_http_bounds_and_redirect_authority_are_forwarded(self):
        root, home, workspace, previous = self.temp_context()
        original = executor.boundedWebFetch
        try:
            config, session = self._session(home, workspace)
            spec = run(
                normalize_health_check_spec(
                    {
                        "title": "http health",
                        "mode": "http",
                        "url": "http://example.test/health",
                        "timeoutMs": 1200,
                        "bodyLimitBytes": 4096,
                        "expectedStatusCodes": [200],
                        "expectedBodyPolicy": "contains",
                        "expectedBodySubstring": "ready",
                    },
                    str(workspace),
                )
            )
            policy = run(authorize_health_check_spec(self._context(config, session), spec))
            captured = {}

            async def fake_fetch(**kwargs):
                captured.update(kwargs)
                await kwargs["authorizeHop"]("http://example.test/next")
                with self.assertRaises(RuntimeError):
                    await kwargs["authorizeHop"]("http://other.test/next")
                return {
                    "outcome": "ok",
                    "statusCode": 200,
                    "body": "ready",
                    "compressedBytes": 5,
                    "decodedBytes": 5,
                }

            executor.boundedWebFetch = fake_fetch
            record = build_authorized_health_check_record(
                request=self._request(session, spec, policy), taskId="health-http-policy"
            )
            result = run(executor.execute_health_check(record=record, signal=asyncio.Event()))
            self.assertTrue(result["okay"])
            self.assertEqual(captured["compressedMaxBytes"], 4096)
            self.assertEqual(captured["decodedMaxBytes"], 4096)
            self.assertEqual(captured["totalTimeoutMs"], 1200)
            self.assertEqual(captured["maxRedirects"], 3)
        finally:
            executor.boundedWebFetch = original
            self.restore_home(root, previous)

    def test_stop_generation_drops_late_attempt_result(self):
        root, home, workspace, previous = self.temp_context()
        try:
            config, session = self._session(home, workspace)
            spec = self._command_spec(workspace)
            policy = run(authorize_health_check_spec(self._context(config, session), spec))
            task_id = "health-stop-generation"
            record = build_authorized_health_check_record(
                request=self._request(session, spec, policy), taskId=task_id
            )
            run(persist_health_check_record(record))
            prepared = run(prepare_task_stop(task_id, "SIGTERM"))
            self.assertTrue(prepared["result"]["changed"])
            stopped = run(core.load_health_check_task(task_id))
            self.assertEqual((stopped["status"], stopped["generation"]), ("stopped", 2))
            committed = run(
                _commit_attempt(
                    task_id,
                    1,
                    now_iso(),
                    {
                        "okay": True,
                        "outputExcerpt": "late success",
                        "outputTruncated": False,
                        "measuredBytes": 12,
                    },
                )
            )
            self.assertIsNone(committed)
            self.assertEqual(run(core.load_health_check_task(task_id))["status"], "stopped")
        finally:
            self.restore_home(root, previous)

if __name__ == "__main__":
    unittest.main()
