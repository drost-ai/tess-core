from __future__ import annotations

from tests.support.agent_core_case import *

from agent_core.tasks import health_check_executor as executor
from agent_core.tasks import health_check_revalidation as revalidation
from agent_core.tasks.health_check_migration import (
    loadHealthCheckRevalidationJournal,
    migrateLegacyHealthCheck,
)
from agent_core.tasks.health_check_policy import (
    authorizeHealthCheckSpec,
    healthCheckSpecDigest,
    normalizeHealthCheckSpec,
)
from agent_core.tasks.health_check_record import (
    buildAuthorizedHealthCheckRecord,
    persistHealthCheckRecord,
)
from agent_core.tasks.health_check_runtime import _commit_attempt
from agent_core.tasks.terminal_store import prepareTaskStop


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
        session = core.createEmptySession(
            cwd=str(workspace),
            provider=config["provider"],
            providerProfileName=config["providerProfileName"],
            model=config["model"],
            effort=config["effort"],
            systemPrompt=config["systemPrompt"],
            permissionMode=mode,
        )
        run(core.saveSession(session))
        return config, session

    def _context(self, config, session, daemon=None):
        return {
            "session": session,
            "config": config,
            "interactive": False,
            "askQuestion": lambda *_: "",
            "outputJson": lambda *_: None,
            "cwd": session["cwd"],
            "persistSession": lambda: None,
            **({"daemon": daemon} if daemon else {}),
        }

    def _command_spec(self, workspace: Path):
        return run(
            normalizeHealthCheckSpec(
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
            "specDigest": healthCheckSpecDigest(spec),
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
                    run(normalizeHealthCheckSpec(row, str(workspace)))
        finally:
            self.restore_home(root, previous)

    def test_tool_authorizes_before_daemon_and_plan_mode_never_persists(self):
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
            self.assertEqual(calls[0]["specDigest"], healthCheckSpecDigest(calls[0]["spec"]))
            self.assertEqual(calls[0]["executionPolicy"]["allowedOrigins"], ["http://example.test"])

            plan_config, plan_session = self._session(home, workspace, "plan")
            plan_calls = []
            with self.assertRaises(PermissionError):
                run(
                    tool.handler(
                        {
                            "title": "blocked",
                            "mode": "http",
                            "url": "http://example.test/",
                            "expected_status_codes": [200],
                            "expected_body_policy": "ignore",
                        },
                        self._context(
                            plan_config,
                            plan_session,
                            {"startHealthCheck": lambda payload: plan_calls.append(payload)},
                        ),
                    )
                )
            self.assertEqual(plan_calls, [])
        finally:
            self.restore_home(root, previous)

    def test_direct_gateway_rejects_unauthorized_start_before_persistence(self):
        root, home, workspace, previous = self.temp_context()
        try:
            _config, session = self._session(home, workspace)

            async def scenario():
                supervisor = await core.createTaskSupervisor({"cwd": str(workspace)})
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
                    self.assertEqual(await core.listHealthCheckTasks(), [])
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
            policy = run(authorizeHealthCheckSpec(context, spec))
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
            record = buildAuthorizedHealthCheckRecord(
                request=self._request(session, spec, policy), taskId="health-command-policy"
            )
            result = run(executor.executeHealthCheck(record=record, signal=asyncio.Event()))
            self.assertTrue(result["okay"])
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
                normalizeHealthCheckSpec(
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
            policy = run(authorizeHealthCheckSpec(self._context(config, session), spec))
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
            record = buildAuthorizedHealthCheckRecord(
                request=self._request(session, spec, policy), taskId="health-http-policy"
            )
            result = run(executor.executeHealthCheck(record=record, signal=asyncio.Event()))
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
            policy = run(authorizeHealthCheckSpec(self._context(config, session), spec))
            task_id = "health-stop-generation"
            record = buildAuthorizedHealthCheckRecord(
                request=self._request(session, spec, policy), taskId=task_id
            )
            run(persistHealthCheckRecord(record))
            prepared = run(prepareTaskStop(task_id, "SIGTERM"))
            self.assertTrue(prepared["result"]["changed"])
            stopped = run(core.loadHealthCheckTask(task_id))
            self.assertEqual((stopped["status"], stopped["generation"]), ("stopped", 2))
            committed = run(
                _commit_attempt(
                    task_id,
                    1,
                    core.now_iso(),
                    {
                        "okay": True,
                        "outputExcerpt": "late success",
                        "outputTruncated": False,
                        "measuredBytes": 12,
                    },
                )
            )
            self.assertIsNone(committed)
            self.assertEqual(run(core.loadHealthCheckTask(task_id))["status"], "stopped")
        finally:
            self.restore_home(root, previous)

    def test_legacy_migration_is_inert_exact_and_revalidation_is_idempotent(self):
        root, home, workspace, previous = self.temp_context()
        try:
            config, session = self._session(home, workspace)
            task_id = "health-legacy-active"
            legacy = {
                "taskId": task_id,
                "sessionId": session["id"],
                "title": "legacy health",
                "mode": "command",
                "command": "printf legacy",
                "cwd": str(workspace),
                "intervalMs": 1000,
                "timeoutMs": 1000,
                "failureThreshold": 2,
                "backoffMultiplier": 2,
                "maxIntervalMs": 4000,
                "status": "active",
                "createdAt": core.now_iso(),
                "updatedAt": core.now_iso(),
            }
            run(core.saveHealthCheckTask(legacy))
            source_path = Path(core.healthCheckMetaPath(task_id))
            source_bytes = source_path.read_bytes()
            suspended = run(migrateLegacyHealthCheck(task_id))
            self.assertEqual((suspended["status"], suspended["generation"]), ("suspended_unverified", 0))
            journal = run(loadHealthCheckRevalidationJournal(task_id))
            self.assertEqual(base64.b64decode(journal["rawLegacyBytesBase64"]), source_bytes)
            self.assertNotIn("executionPolicy", suspended)

            context = self._context(config, session)
            grant = run(revalidation.authorizeHealthCheckRevalidation(context, task_id))
            values = {
                "taskId": task_id,
                "expectedSuspensionRevision": suspended["suspensionRevision"],
                "expectedSpecDigest": suspended["specDigest"],
                "approvalGrant": grant,
            }

            async def concurrent():
                return await asyncio.gather(
                    revalidation.revalidateHealthCheck(values),
                    revalidation.revalidateHealthCheck(values),
                )

            first, second = run(concurrent())
            self.assertEqual(first, second)
            replacement = run(core.loadHealthCheckTask(first["replacementTaskId"]))
            self.assertEqual((replacement["status"], replacement["generation"]), ("active", 1))
            self.assertEqual(replacement["supersedes"], task_id)
            self.assertEqual(run(core.loadHealthCheckTask(task_id))["status"], "suspended_unverified")

            terminal_id = "health-legacy-stopped"
            terminal = {**legacy, "taskId": terminal_id, "status": "stopped"}
            run(core.saveHealthCheckTask(terminal))
            terminal_path = Path(core.healthCheckMetaPath(terminal_id))
            before = terminal_path.read_bytes()
            run(migrateLegacyHealthCheck(terminal_id))
            self.assertEqual(terminal_path.read_bytes(), before)
        finally:
            getattr(revalidation, "__setHealthCheckGrantClockForTests")()
            getattr(revalidation, "__setHealthCheckRevalidationTestHookForTests")()
            self.restore_home(root, previous)


if __name__ == "__main__":
    unittest.main()
