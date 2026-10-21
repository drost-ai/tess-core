"""Scheduler rules and job records: local wall times in IANA zones, resolved
to UTC across daylight-saving changes; jobs built, edited and advanced."""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from agent_core.scheduler import jobs, rules
from agent_core.scheduler.rules import RuleError

LA = "America/Los_Angeles"
UTC = timezone.utc


def fires(rule, after, count, tz=LA):
    out, cursor = [], after
    for _ in range(count):
        cursor = rules.next_fire(rule, tz, cursor, previous=cursor)
        out.append(rules.local_text(cursor, tz))
    return out


class RuleTests(unittest.TestCase):
    def test_hourly_normalization_and_local_hour_boundaries(self):
        rule = rules.normalize({"kind": "hourly"})
        self.assertEqual(rule, {"kind": "hourly", "minute": 0})
        self.assertEqual(rules.describe(rules.normalize({"kind": "hourly", "minute": 5}), LA), f"hourly at :05 ({LA})")
        for minute in (-1, 60, True, "0"):
            with self.assertRaises(RuleError):
                rules.normalize({"kind": "hourly", "minute": minute})
        now = datetime(2026, 10, 16, 22, 40, tzinfo=UTC)
        self.assertEqual(rules.first_fire(rule, LA, now), datetime(2026, 10, 16, 23, tzinfo=UTC))
        self.assertEqual(rules.next_fire(rule, LA, now.replace(minute=0)), now.replace(hour=23, minute=0))
        self.assertEqual(rules.first_fire(rule, "Asia/Kathmandu", now), now.replace(minute=15) + timedelta(hours=1))

    def test_hourly_dst_skips_gap_and_fires_both_repeated_hours(self):
        rule = rules.normalize({"kind": "hourly"})
        self.assertEqual(fires(rule, datetime(2026, 3, 8, 9, 30, tzinfo=UTC), 2),
                         ["Sun 8 Mar 2026, 3:00 AM PDT", "Sun 8 Mar 2026, 4:00 AM PDT"])
        self.assertEqual(fires(rule, datetime(2026, 11, 1, 7, 30, tzinfo=UTC), 3),
                         ["Sun 1 Nov 2026, 1:00 AM PDT", "Sun 1 Nov 2026, 1:00 AM PST", "Sun 1 Nov 2026, 2:00 AM PST"])

    def test_interval_job_migrates_and_completion_or_failure_stays_on_hour(self):
        now = datetime(2026, 10, 16, 22, 40, tzinfo=UTC)
        job = jobs.build_job(job_id='tess.personal.extractor', kind='personal.extractor',
                             rule={'kind': 'interval', 'everySeconds': 900}, timezone=LA, now=now)
        job = jobs.edit_job(job, {'rule': {'kind': 'hourly', 'minute': 0}}, now)
        self.assertEqual(rules.parse(job['nextFireAt']), datetime(2026, 10, 16, 23, tzinfo=UTC))
        for outcome in ('completed', 'failed', 'busy', 'not_due'):
            advanced = jobs.advance(job, fire={'outcome': outcome, 'scheduledFor': job['nextFireAt']},
                                    now=datetime(2026, 10, 16, 23, 30, tzinfo=UTC))
            self.assertEqual(rules.parse(advanced['nextFireAt']), datetime(2026, 10, 17, 0, tzinfo=UTC))

    def test_a_daily_time_keeps_its_wall_clock_across_both_dst_changes(self):
        spring = rules.normalize({"kind": "calendar", "time": "02:30"})
        self.assertEqual(
            fires(spring, datetime(2026, 3, 7, 12, tzinfo=UTC), 3),
            # 02:30 does not exist on 8 March: it fires where the zone maps it.
            ["Sun 8 Mar 2026, 3:30 AM PDT", "Mon 9 Mar 2026, 2:30 AM PDT", "Tue 10 Mar 2026, 2:30 AM PDT"],
        )
        autumn = rules.normalize({"kind": "calendar", "time": "01:30"})
        self.assertEqual(
            fires(autumn, datetime(2026, 10, 31, 12, tzinfo=UTC), 3),
            # 01:30 happens twice on 1 November: the first one, once.
            ["Sun 1 Nov 2026, 1:30 AM PDT", "Mon 2 Nov 2026, 1:30 AM PST", "Tue 3 Nov 2026, 1:30 AM PST"],
        )

    def test_a_one_time_job_in_a_gap_or_an_overlap_is_refused(self):
        for at, code in (("2026-03-08T02:30", "scheduler.nonexistent_time"), ("2026-11-01T01:30", "scheduler.ambiguous_time")):
            with self.assertRaises(RuleError) as caught:
                rules.once_instant(rules.normalize({"kind": "once", "at": at}), LA, strict=True)
            self.assertEqual(caught.exception.code, code)
        instant = rules.once_instant(rules.normalize({"kind": "once", "at": "2026-10-08T11:00"}), LA, strict=True)
        self.assertEqual((rules.iso(instant), rules.local_text(instant, LA)), ("2026-10-08T18:00:00Z", "Thu 8 Oct 2026, 11:00 AM PDT"))

    def test_weekdays_month_days_and_intervals(self):
        weekdays = rules.normalize({"kind": "calendar", "time": "9:00", "days": ["Mon", "tue", "WED", "thu", "fri"]})
        self.assertEqual(rules.describe(weekdays, LA), f"weekdays at 09:00 ({LA})")
        self.assertEqual(fires(weekdays, datetime(2026, 10, 9, 17, tzinfo=UTC), 2), ["Mon 12 Oct 2026, 9:00 AM PDT", "Tue 13 Oct 2026, 9:00 AM PDT"])
        monthly = rules.normalize({"kind": "calendar", "time": "08:00", "monthDays": [31]})
        self.assertEqual(fires(monthly, datetime(2026, 10, 1, tzinfo=UTC), 2), ["Sat 31 Oct 2026, 8:00 AM PDT", "Thu 31 Dec 2026, 8:00 AM PST"])
        every = rules.normalize({"kind": "interval", "everySeconds": 1800})
        anchor = datetime(2026, 10, 7, 12, tzinfo=UTC)
        # Missed periods collapse: the next fire is the first one after now.
        self.assertEqual(rules.next_fire(every, LA, anchor + timedelta(hours=5, minutes=10), previous=anchor), anchor + timedelta(hours=5, minutes=30))

    def test_bad_input_names_what_is_wrong(self):
        cases = [
            ({"kind": "weekly"}, "scheduler.invalid_rule"),
            ({"kind": "once", "at": "tomorrow"}, "scheduler.invalid_time"),
            ({"kind": "once", "at": "2026-10-08T11:00-07:00"}, "scheduler.invalid_time"),
            ({"kind": "calendar", "time": "25:00"}, "scheduler.invalid_time"),
            ({"kind": "calendar", "time": "09:00", "days": ["mon"], "monthDays": [1]}, "scheduler.invalid_rule"),
            ({"kind": "interval", "everySeconds": 0}, "scheduler.invalid_rule"),
        ]
        for rule, code in cases:
            with self.assertRaises(RuleError, msg=rule) as caught:
                rules.normalize(rule)
            self.assertEqual(caught.exception.code, code)
        with self.assertRaises(RuleError) as caught:
            rules.zone("Pacific Time")
        self.assertEqual(caught.exception.code, "scheduler.unknown_timezone")


class JobTests(unittest.TestCase):
    NOW = datetime(2026, 10, 7, 17, 0, tzinfo=UTC)  # 10:00 PDT

    def build(self, **overrides):
        fields = {"job_id": "sch_test", "kind": "turn", "rule": {"kind": "once", "at": "2026-10-08T11:00"}, "timezone": LA,
                  "now": self.NOW, "session_id": "s1", "instructions": "Remind Migel to call Kevin.", "created_by": "agent"}
        return jobs.build_job(**{**fields, **overrides})

    def test_a_reminder_is_built_with_its_next_fire_and_viewed_in_its_zone(self):
        job = self.build()
        self.assertEqual((job["state"], job["nextFireAt"], job["label"]), ("active", "2026-10-08T18:00:00Z", "Remind Migel to call Kevin."))
        seen = jobs.view(job, self.NOW)
        self.assertEqual((seen["nextFire"], seen["nextFireIn"], seen["schedule"]), ("Thu 8 Oct 2026, 11:00 AM PDT", "in 25 h", "once"))

    def test_the_agent_cannot_schedule_the_past_and_a_turn_needs_its_session_and_words(self):
        for overrides, code in (
            ({"rule": {"kind": "once", "at": "2026-10-07T09:00"}}, "scheduler.time_in_past"),
            ({"session_id": None}, "scheduler.session_required"),
            ({"instructions": "  "}, "scheduler.instructions_required"),
        ):
            with self.assertRaises(RuleError) as caught:
                self.build(**overrides)
            self.assertEqual(caught.exception.code, code)

    def test_advancing_finishes_a_one_time_job_and_moves_a_recurring_one_past_now(self):
        fire = {"fireKey": "k", "scheduledFor": "2026-10-08T18:00:00Z", "outcome": "completed"}
        finished = jobs.advance(self.build(), fire=fire, now=self.NOW + timedelta(days=1, hours=1))
        self.assertEqual((finished["state"], finished["nextFireAt"], finished["fireCount"]), ("done", None, 1))
        daily = self.build(rule={"kind": "calendar", "time": "09:00"})
        # Three days late: one fire for all of them, and the next is tomorrow.
        moved = jobs.advance(daily, fire={**fire, "scheduledFor": "2026-10-08T16:00:00Z"}, now=datetime(2026, 10, 11, 18, tzinfo=UTC))
        self.assertEqual((moved["state"], moved["nextFireAt"]), ("active", "2026-10-12T16:00:00Z"))

    def test_editing_pausing_and_resuming(self):
        job = self.build(rule={"kind": "calendar", "time": "09:00"})
        moved = jobs.edit_job(job, {"rule": {"kind": "calendar", "time": "18:30"}, "label": "Evening check"}, self.NOW)
        self.assertEqual((moved["nextFireAt"], moved["label"]), ("2026-10-08T01:30:00Z", "Evening check"))
        paused = jobs.edit_job(moved, {"state": "paused"}, self.NOW)
        self.assertFalse(jobs.due(paused, self.NOW + timedelta(days=2)))
        # Resumed after its time passed: not made up, continues from now.
        resumed = jobs.edit_job(paused, {"state": "active"}, self.NOW + timedelta(days=2))
        self.assertEqual(resumed["nextFireAt"], "2026-10-10T01:30:00Z")
        cancelled = jobs.edit_job(resumed, {"state": "cancelled"}, self.NOW)
        with self.assertRaises(RuleError):
            jobs.edit_job(cancelled, {"state": "active"}, self.NOW)


if __name__ == "__main__":
    unittest.main()
