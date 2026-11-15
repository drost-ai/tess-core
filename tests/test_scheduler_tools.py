"""The agent's scheduler tools: local times resolved and echoed, refusals
that say why, scope fixed to the calling session, and present only where a
scheduler timer runs."""
from __future__ import annotations

from datetime import datetime, timedelta

from tests.support.tess_core_case import *
from tests.support.session_monitors import Listener, ScriptedProvider, SilentListener, tool_reply

from tess_core import SessionOptions, start_runtime
from tess_core.config._knobs import reset_configured_knobs
from tess_core.runtime import host as host_module
from tess_core.scheduler import rules
from tess_core.scheduler.store import list_jobs, load_job
from tess_core.tools.schedule import NAMES, schedule_tools

LA = "America/Los_Angeles"


def tools():
    return {tool.name: tool for tool in schedule_tools()}


def context(session_id="s1", timezone=LA):
    return {"session": {"id": session_id}, "config": {"ownerTimezone": timezone} if timezone else {}}


def tomorrow_at(clock: str) -> str:
    local = datetime.now(rules.zone(LA)) + timedelta(days=1)
    return f"{local:%Y-%m-%d}T{clock}"


async def call(name, payload, ctx=None):
    result = await tools()[name].handler(payload, ctx or context())
    return json.loads(result["content"]), bool(result.get("isError"))


class SchedulerToolTests(TessCoreTestCase):
    def setUp(self):
        host_module._ACTIVE = None

    def tearDown(self):
        reset_configured_knobs()

    def test_create_echoes_the_resolved_local_time_and_list_update_cancel_work_on_own_jobs(self):
        root, home, workspace, previous = self.temp_context()
        try:
            async def scenario():
                # Codex-style zero-filled optional parameters mean "not given".
                created, failed = await call("ScheduleCreate", {"instructions": "Remind Migel to call Kevin.", "label": "Call Kevin", "at": tomorrow_at("11:00"),
                                                                "time": "", "days": [], "month_days": [], "every_minutes": 0, "timezone": ""})
                self.assertFalse(failed, created)
                job = created["scheduled"]
                self.assertTrue(job["nextFire"].endswith("11:00 AM PDT") or job["nextFire"].endswith("11:00 AM PST"), job)
                self.assertIn("in ", job["nextFireIn"])
                self.assertIn(job["nextFire"], created["confirm"])
                self.assertEqual(((await load_job(job["id"]))["sessionId"], job["timezone"]), ("s1", LA))

                listed, _ = await call("ScheduleList", {})
                self.assertEqual([item["id"] for item in listed["jobs"]], [job["id"]])
                other, _ = await call("ScheduleList", {}, context("s2"))
                self.assertEqual(other["jobs"], [])
                stolen, failed = await call("ScheduleCancel", {"job_id": job["id"]}, context("s2"))
                self.assertEqual((failed, stolen["error"]), (True, "scheduler.job_not_found"))

                moved, failed = await call("ScheduleUpdate", {"job_id": job["id"], "time": "07:30", "days": ["mon", "wed"], "state": "paused"})
                self.assertFalse(failed, moved)
                self.assertEqual((moved["updated"]["schedule"], moved["updated"]["state"]), (f"Mon, Wed at 07:30 ({LA})", "paused"))
                cancelled, _ = await call("ScheduleCancel", {"job_id": job["id"]})
                self.assertEqual(cancelled["cancelled"]["state"], "cancelled")
                self.assertEqual((await call("ScheduleList", {}))[0]["jobs"], [])

            run(scenario())
        finally:
            self.restore_home(root, previous)

    def test_refusals_say_why(self):
        root, home, workspace, previous = self.temp_context()
        try:
            async def scenario():
                cases = [
                    ({"instructions": "x", "at": "2026-03-08T02:30"}, None, "scheduler.nonexistent_time"),
                    ({"instructions": "x", "at": "2020-01-01T09:00"}, None, "scheduler.time_in_past"),
                    ({"instructions": "x", "at": tomorrow_at("09:00"), "time": "09:00"}, None, "scheduler.invalid_rule"),
                    ({"instructions": "x"}, None, "scheduler.invalid_rule"),
                    ({"instructions": "x", "time": "09:00", "timezone": "PST8PDT-ish"}, None, "scheduler.unknown_timezone"),
                    ({"instructions": "x", "time": "09:00"}, context(timezone=None), "scheduler.timezone_required"),
                ]
                for payload, ctx, code in cases:
                    answer, failed = await call("ScheduleCreate", payload, ctx)
                    self.assertEqual((failed, answer.get("error")), (True, code), payload)
                self.assertEqual(await list_jobs(), [])

            run(scenario())
        finally:
            self.restore_home(root, previous)

    def test_the_tools_exist_only_where_a_timer_runs(self):
        root, home, workspace, previous = self.temp_context()
        try:
            async def scenario():
                for listener, expected in ((Listener(), True), (SilentListener(), False)):
                    provider = ScriptedProvider(tool_reply("ScheduleCreate", {"instructions": "Check the build.", "time": "17:00"}), "Set for 5 PM.")
                    runtime = await start_runtime(str(home), cwd=str(workspace), listener=listener, daemon_enabled=False,
                                                  session_options=lambda sid, reason: SessionOptions(permission_mode="bypassPermissions", provider_override=provider.override, timezone=LA))
                    try:
                        session = await runtime.sessions.open()
                        await session.run_turn("check the build at 5pm every day")
                        offered = {tool["name"] for tool in provider.requests[0].get("tools") or []}
                        self.assertEqual(set(NAMES) <= offered, expected, offered)
                        if expected:
                            (job,) = await list_jobs()
                            self.assertEqual((job["sessionId"], job["timezone"], job["createdBy"], job["rule"]["time"]), (session.id, LA, "agent", "17:00"))
                    finally:
                        await runtime.stop()

            run(scenario())
        finally:
            self.restore_home(root, previous)


if __name__ == "__main__":
    unittest.main()
