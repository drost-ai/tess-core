"""The runtime's scheduler timer against a real runtime and a scripted
provider: a due turn job reopens its closed session and runs a ``scheduled``
turn; failures, interruptions and late fires reach the owner as notices;
host kinds share the timer; only a runtime that can publish fires. Every
test is one coroutine: a runtime belongs to the loop that started it."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from tests.support.tess_core_case import *
from tests.support.session_monitors import Listener, ScriptedProvider, SilentListener, eventually

from tess_core import SessionOptions, start_runtime
from tess_core.config._knobs import reset_configured_knobs
from tess_core.runtime import host as host_module
from tess_core.scheduler import operations, rules
from tess_core.scheduler.store import load_job, mutate_job

LA = "America/Los_Angeles"
MAX_LATE = "TESS_CORE_SCHEDULER_MAX_LATE_MS"


def soon(seconds: float = 0.3) -> str:
    return rules.iso(datetime.now(timezone.utc) + timedelta(seconds=seconds))


class NoticeListener(Listener):
    def __init__(self) -> None:
        super().__init__()
        self.notices: list[tuple[str, dict]] = []

    async def publish_schedule_notice(self, session_id, notice):
        self.notices.append((session_id, notice))
        return {"noticeId": notice["noticeId"], "seq": len(self.notices)}


class SchedulerTimerTests(TessCoreTestCase):
    def setUp(self):
        host_module._ACTIVE = None

    def tearDown(self):
        os.environ.pop(MAX_LATE, None)
        reset_configured_knobs()

    async def _runtime(self, home, workspace, provider, listener):
        def options(session_id, reason):
            return SessionOptions(permission_mode="bypassPermissions", provider_override=provider.override)

        return await start_runtime(str(home), cwd=str(workspace), listener=listener, daemon_enabled=False, session_options=options)

    async def _turn_job(self, session_id, **overrides):
        fields = {"kind": "turn", "rule": {"kind": "once", "atUtc": soon()}, "timezone": LA, "session_id": session_id,
                  "label": "Call Kevin", "instructions": "Remind Migel to call Kevin.", "created_by": "host"}
        return await operations.create_job(**{**fields, **overrides})

    def test_a_due_job_reopens_its_closed_session_and_runs_a_scheduled_turn(self):
        root, home, workspace, previous = self.temp_context()
        try:
            async def scenario():
                provider = ScriptedProvider("Hello.", "Migel, it's time to call Kevin.")
                listener = NoticeListener()
                runtime = await self._runtime(home, workspace, provider, listener)
                try:
                    session = await runtime.sessions.open()
                    await session.run_turn("hi")
                    await runtime.sessions.close(session.id)
                    job = await self._turn_job(session.id)
                    await eventually(lambda: len(provider.requests) == 2, label="the scheduled turn")
                    await eventually(lambda: runtime.scheduler.status()["firing"] == [], label="fire finished")
                    self.assertIn((session.id, "scheduled"), listener.opened)
                    start = listener.turn_starts(session.id)[-1]
                    self.assertEqual(start.get("origin"), "scheduled")
                    prompt = provider.transcript(1)
                    self.assertIn("<scheduled-wake>", prompt)
                    self.assertIn("Remind Migel to call Kevin.", prompt)
                    saved = await load_job(job["id"])
                    self.assertEqual((saved["state"], saved["lastFire"]["outcome"], saved["claim"]), ("done", "completed", None))
                    self.assertEqual(listener.notices, [])
                    self.assertIn("scheduler.job_changed", [event for event, _ in listener.runtime_events])
                    (firing,) = [data for event, data in listener.runtime_events if event == "scheduler.job_firing"]
                    self.assertEqual((firing["job_id"], firing["label"], firing["turn_id"]), (job["id"], "Call Kevin", start.get("turn_id")))
                finally:
                    await runtime.stop()

            run(scenario())
        finally:
            self.restore_home(root, previous)

    def test_a_failed_turn_leaves_a_notice_with_the_jobs_own_words(self):
        root, home, workspace, previous = self.temp_context()
        try:
            async def scenario():
                provider = ScriptedProvider("ok")
                send = provider.send

                async def failing(request):
                    if provider.answered >= 1:
                        raise RuntimeError("provider down")
                    return await send(request)

                provider.send = failing  # the second request (the scheduled turn) fails
                listener = NoticeListener()
                runtime = await self._runtime(home, workspace, provider, listener)
                try:
                    session = await runtime.sessions.open()
                    await session.run_turn("hi")
                    job = await self._turn_job(session.id)
                    await eventually(lambda: bool(listener.notices), label="the notice")
                    session_id, notice = listener.notices[0]
                    self.assertEqual((session_id, notice["reason"], notice["jobId"]), (session.id, "failed", job["id"]))
                    self.assertIn("Remind Migel to call Kevin.", notice["text"])
                    await eventually(lambda: runtime.scheduler.status()["firing"] == [], label="fire finished")
                    self.assertEqual((await load_job(job["id"]))["lastFire"]["outcome"], "failed")
                finally:
                    await runtime.stop()

            run(scenario())
        finally:
            self.restore_home(root, previous)

    def test_a_fire_left_started_by_a_dead_runtime_is_reported_never_repeated(self):
        root, home, workspace, previous = self.temp_context()
        try:
            async def scenario():
                provider = ScriptedProvider("ok")
                listener = NoticeListener()
                runtime = await self._runtime(home, workspace, provider, listener)
                try:
                    session = await runtime.sessions.open()
                    await session.run_turn("hi")
                    await runtime.scheduler.stop()  # nothing fires while the crash is staged
                    job = await self._turn_job(session.id, rule={"kind": "calendar", "time": "09:00"})
                    scheduled = job["nextFireAt"]
                    await mutate_job(job["id"], lambda current: {**current, "nextFireAt": "2026-01-01T17:00:00Z", "claim": {
                        "fireKey": f"{job['id']}@2026-01-01T17:00:00Z", "scheduledFor": "2026-01-01T17:00:00Z",
                        "instanceId": "dead-runtime", "pid": 2 ** 22 + 12345, "started": True}})
                    runtime.scheduler.start()
                    await eventually(lambda: bool(listener.notices), label="the interrupted notice")
                    self.assertEqual(listener.notices[0][1]["reason"], "interrupted")
                    await eventually(lambda: runtime.scheduler.status()["firing"] == [], label="fire finished")
                    saved = await load_job(job["id"])
                    self.assertEqual((saved["state"], saved["lastFire"]["outcome"], saved["nextFireAt"]), ("active", "interrupted", scheduled))
                    self.assertEqual(len(provider.requests), 1)  # the interrupted turn was not run again
                finally:
                    await runtime.stop()

            run(scenario())
        finally:
            self.restore_home(root, previous)

    def test_a_fire_later_than_the_configured_bound_is_skipped_visibly(self):
        root, home, workspace, previous = self.temp_context()
        try:
            os.environ[MAX_LATE] = "60000"

            async def scenario():
                provider = ScriptedProvider("ok")
                listener = NoticeListener()
                runtime = await self._runtime(home, workspace, provider, listener)
                try:
                    session = await runtime.sessions.open()
                    await session.run_turn("hi")
                    job = await self._turn_job(session.id, rule={"kind": "once", "atUtc": rules.iso(datetime.now(timezone.utc) - timedelta(hours=2))})
                    runtime.scheduler.wake()
                    await eventually(lambda: bool(listener.notices), label="the skipped notice")
                    self.assertEqual(listener.notices[0][1]["reason"], "skipped_late")
                    await eventually(lambda: runtime.scheduler.status()["firing"] == [], label="fire finished")
                    self.assertEqual(((await load_job(job["id"]))["state"], len(provider.requests)), ("done", 1))
                finally:
                    await runtime.stop()

            run(scenario())
        finally:
            self.restore_home(root, previous)

    def test_a_job_for_an_ended_session_is_cancelled(self):
        root, home, workspace, previous = self.temp_context()
        try:
            async def scenario():
                provider = ScriptedProvider("ok")
                listener = NoticeListener()
                runtime = await self._runtime(home, workspace, provider, listener)
                try:
                    session = await runtime.sessions.open()
                    await session.run_turn("hi")
                    await runtime.sessions.end(session.id)
                    job = await self._turn_job(session.id)
                    await eventually(lambda: runtime.scheduler.status()["recent"] != [], label="the fire")
                    saved = await load_job(job["id"])
                    self.assertEqual((saved["state"], saved["lastFire"]["outcome"], saved["lastFire"]["detail"]), ("cancelled", "refused", "session_ended"))
                finally:
                    await runtime.stop()

            run(scenario())
        finally:
            self.restore_home(root, previous)

    def test_host_kinds_share_the_timer_and_may_name_their_next_fire(self):
        root, home, workspace, previous = self.temp_context()
        try:
            async def scenario():
                listener = NoticeListener()
                runtime = await self._runtime(home, workspace, ScriptedProvider("ok"), listener)
                try:
                    calls: list[dict] = []
                    following = datetime.now(timezone.utc) + timedelta(hours=3)

                    async def extractor(job, fire):
                        calls.append(fire)
                        return {"outcome": "not_due", "nextFireAt": rules.iso(following)}

                    job = await runtime.scheduler.ensure("host.extractor", kind="memory.extract", rule={"kind": "interval", "everySeconds": 3600}, timezone=LA)
                    again = await runtime.scheduler.ensure("host.extractor", kind="memory.extract", rule={"kind": "interval", "everySeconds": 60}, timezone=LA)
                    self.assertEqual(again["revision"], job["revision"])  # a standing job is left as it is
                    await mutate_job(job["id"], lambda current: {**current, "nextFireAt": soon(0)})
                    runtime.scheduler.wake()
                    await asyncio.sleep(0.3)
                    self.assertEqual(calls, [])  # nobody handles its kind yet: it waits
                    runtime.scheduler.register("memory.extract", extractor)
                    await eventually(lambda: len(calls) == 1, label="the host job")
                    await eventually(lambda: runtime.scheduler.status()["firing"] == [], label="fire finished")
                    saved = await load_job(job["id"])
                    self.assertEqual((saved["lastFire"]["outcome"], saved["nextFireAt"]), ("not_due", rules.iso(following)))
                finally:
                    await runtime.stop()

            run(scenario())
        finally:
            self.restore_home(root, previous)

    def test_a_runtime_that_cannot_publish_runs_no_timer(self):
        root, home, workspace, previous = self.temp_context()
        try:
            async def scenario():
                runtime = await self._runtime(home, workspace, ScriptedProvider("ok"), SilentListener())
                try:
                    self.assertFalse(runtime.status()["scheduler"]["running"])
                    job = await operations.create_job(kind="turn", rule={"kind": "once", "atUtc": soon(0.05)}, timezone=LA,
                                                      session_id="s", instructions="x", created_by="host")
                    await asyncio.sleep(0.2)
                    self.assertEqual((await load_job(job["id"]))["lastFire"], None)
                finally:
                    await runtime.stop()

            run(scenario())
        finally:
            self.restore_home(root, previous)


if __name__ == "__main__":
    unittest.main()
