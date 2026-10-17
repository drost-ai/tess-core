"""When a scheduled job fires: rules in a local time zone, resolved to UTC.

A rule is what a person means ("11:00 tomorrow", "weekdays at 09:00",
"every 30 minutes"), kept with its IANA zone; the next fire is computed from
it in UTC, so a daylight-saving change moves the UTC instant and never the
wall-clock time. Four kinds:

- ``once``: ``{"kind": "once", "at": "2026-10-08T11:00"}`` (local wall time),
  or ``{"kind": "once", "atUtc": "...Z"}`` for a host that already has an
  instant.
- ``calendar``: ``{"kind": "calendar", "time": "09:00", "days": ["mon", ...]}``
  or with ``"monthDays": [1, 15]`` instead of ``days``; neither means daily.
- ``hourly``: ``{"kind": "hourly", "minute": 0}``, every local hour.
- ``interval``: ``{"kind": "interval", "everySeconds": 1800}``.

A recurring rule's wall time that does not exist on one day (the spring
gap) fires at the same instant the zone maps it to, after the gap; one that
exists twice (the autumn overlap) fires at the first. A one-shot time in a
gap or an overlap is refused: a person has to say which they meant.

Pure functions; times are aware ``datetime`` objects in UTC.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

UTC = timezone.utc
DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
KINDS = ("once", "calendar", "interval", "hourly")
# How far ahead a calendar rule is searched for its next date (a monthDays
# rule naming only the 31st still finds one within a year).
_SEARCH_DAYS = 400


class RuleError(ValueError):
    """A rule, time or zone that cannot be scheduled; ``code`` says why."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def zone(name: Any) -> ZoneInfo:
    if not isinstance(name, str) or not name.strip():
        raise RuleError("scheduler.timezone_required", "A time zone (IANA name such as America/Los_Angeles) is required.")
    try:
        return ZoneInfo(name.strip())
    except (ZoneInfoNotFoundError, ValueError):
        raise RuleError("scheduler.unknown_timezone", f"Unknown time zone: {name!r}; use an IANA name such as America/Los_Angeles.") from None


def normalize(rule: Any) -> dict[str, Any]:
    """A validated copy of ``rule`` with only the fields its kind uses."""
    if not isinstance(rule, dict) or rule.get("kind") not in KINDS:
        raise RuleError("scheduler.invalid_rule", f"A rule needs a kind: one of {', '.join(KINDS)}.")
    kind = rule["kind"]
    if kind == "once":
        if rule.get("atUtc"):
            return {"kind": "once", "atUtc": _iso(_parse_utc(rule["atUtc"]))}
        return {"kind": "once", "at": _wall(rule.get("at")).isoformat(timespec="minutes")}
    if kind == "hourly":
        minute = rule.get("minute", 0)
        if type(minute) is not int or not 0 <= minute <= 59:
            raise RuleError("scheduler.invalid_rule", "An hourly rule needs minute from 0 to 59.")
        return {"kind": "hourly", "minute": minute}
    if kind == "interval":
        every = rule.get("everySeconds")
        if not isinstance(every, (int, float)) or isinstance(every, bool) or every < 1:
            raise RuleError("scheduler.invalid_rule", "An interval rule needs everySeconds of at least 1.")
        return {"kind": "interval", "everySeconds": int(every)}
    at = _clock(rule.get("time"))
    normalized: dict[str, Any] = {"kind": "calendar", "time": at.strftime("%H:%M")}
    days, month_days = rule.get("days") or [], rule.get("monthDays") or []
    if days and month_days:
        raise RuleError("scheduler.invalid_rule", "A calendar rule takes days or monthDays, not both.")
    if days:
        wanted = {str(day).strip().lower()[:3] for day in days}
        if not wanted <= set(DAYS):
            raise RuleError("scheduler.invalid_rule", f"Days must be among {', '.join(DAYS)}.")
        normalized["days"] = [day for day in DAYS if day in wanted]
    if month_days:
        if not all(isinstance(day, int) and not isinstance(day, bool) and 1 <= day <= 31 for day in month_days):
            raise RuleError("scheduler.invalid_rule", "monthDays must be days of the month, 1 to 31.")
        normalized["monthDays"] = sorted(set(month_days))
    return normalized


def recurring(rule: dict[str, Any]) -> bool:
    return rule["kind"] != "once"


def first_fire(rule: dict[str, Any], tz: str, now: datetime) -> datetime | None:
    """The first fire of a new rule: for a one-shot, its time even if past
    (refused by the caller); otherwise the next occurrence after ``now``."""
    if rule["kind"] == "once":
        return once_instant(rule, tz, strict=True)
    if rule["kind"] == "interval":
        return now + timedelta(seconds=rule["everySeconds"])
    return next_fire(rule, tz, now)


def next_fire(rule: dict[str, Any], tz: str, after: datetime, *, previous: datetime | None = None) -> datetime | None:
    """The first occurrence strictly after ``after``; None when the rule has
    none left (a one-shot). Missed occurrences collapse: ``after`` is now."""
    if rule["kind"] == "once":
        return None
    if rule["kind"] == "interval":
        step = timedelta(seconds=rule["everySeconds"])
        anchor = previous or after
        if anchor > after:
            return anchor
        periods = int((after - anchor) / step) + 1
        return anchor + periods * step
    zone_info = zone(tz)
    if rule["kind"] == "hourly":
        # Walk real instants, not local arithmetic: spring gaps are skipped
        # and both occurrences of a repeated autumn hour fire. This also
        # respects zones whose UTC offset is not a whole number of hours.
        candidate = after.astimezone(UTC).replace(second=0, microsecond=0) + timedelta(minutes=1)
        for _ in range(24 * 60):
            if candidate.astimezone(zone_info).minute == rule.get("minute", 0):
                return candidate
            candidate += timedelta(minutes=1)
        return None
    at = _clock(rule["time"])
    local_after = after.astimezone(zone_info)
    for offset in range(_SEARCH_DAYS):
        day = local_after.date() + timedelta(days=offset)
        if not _matches(rule, day):
            continue
        candidate = _resolve(datetime.combine(day, at), zone_info)
        if candidate > after:
            return candidate
    return None


def once_instant(rule: dict[str, Any], tz: str, *, strict: bool) -> datetime:
    if rule.get("atUtc"):
        return _parse_utc(rule["atUtc"])
    wall = _wall(rule.get("at"))
    zone_info = zone(tz)
    if strict:
        check_wall_time(wall, zone_info)
    return _resolve(wall, zone_info)


def check_wall_time(wall: datetime, zone_info: ZoneInfo) -> None:
    """Refuse a local time that does not exist or exists twice."""
    first = wall.replace(tzinfo=zone_info, fold=0)
    second = wall.replace(tzinfo=zone_info, fold=1)
    if first.utcoffset() != second.utcoffset():
        round_trip = first.astimezone(UTC).astimezone(zone_info).replace(tzinfo=None)
        if round_trip != wall:
            raise RuleError("scheduler.nonexistent_time", f"{wall:%Y-%m-%d %H:%M} does not exist in {zone_info.key} (the clocks skip it); choose another time.")
        raise RuleError("scheduler.ambiguous_time", f"{wall:%Y-%m-%d %H:%M} happens twice in {zone_info.key} (the clocks fall back); choose another time.")


def describe(rule: dict[str, Any], tz: str) -> str:
    """The rule in words, for an echo or a list."""
    if rule["kind"] == "once":
        return "once"
    if rule["kind"] == "interval":
        return f"every {_span(rule['everySeconds'])}"
    when = rule["time"]
    if rule.get("monthDays"):
        return f"on day {', '.join(str(day) for day in rule['monthDays'])} of each month at {when} ({tz})"
    days = rule.get("days")
    if not days or len(days) == 7:
        return f"daily at {when} ({tz})"
    if days == list(DAYS[:5]):
        return f"weekdays at {when} ({tz})"
    return f"{', '.join(day.capitalize() for day in days)} at {when} ({tz})"


def local_text(instant: datetime, tz: str) -> str:
    """``Thu 8 Oct 2026, 11:00 AM PDT``."""
    local = instant.astimezone(zone(tz))
    hour = local.strftime("%I").lstrip("0") or "12"
    return f"{local:%a} {local.day} {local:%b %Y}, {hour}:{local:%M %p %Z}"


def until_text(instant: datetime, now: datetime) -> str:
    seconds = (instant - now).total_seconds()
    return f"in {_span(seconds)}" if seconds >= 0 else f"{_span(-seconds)} ago"


def span(seconds: float) -> str:
    """``45 min``, ``3 h 10 min``, ``2 d 4 h``."""
    return _span(seconds)


def iso(instant: datetime) -> str:
    return _iso(instant)


def parse(value: str) -> datetime:
    return _parse_utc(value)


# -- internals -----------------------------------------------------------


def _matches(rule: dict[str, Any], day: date) -> bool:
    if rule.get("monthDays"):
        return day.day in rule["monthDays"]
    days = rule.get("days")
    return not days or DAYS[day.weekday()] in days


def _resolve(wall: datetime, zone_info: ZoneInfo) -> datetime:
    # fold=0: an overlap takes its first occurrence; a gap maps past itself.
    return wall.replace(tzinfo=zone_info, fold=0).astimezone(UTC)


def _wall(value: Any) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise RuleError("scheduler.invalid_time", "A one-time job needs a local date and time, like 2026-10-08T11:00.")
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError:
        raise RuleError("scheduler.invalid_time", f"Not a local date and time: {value!r}; use YYYY-MM-DDTHH:MM.") from None
    if parsed.tzinfo is not None:
        raise RuleError("scheduler.invalid_time", "Give the local wall-clock time without an offset; the time zone is separate.")
    return parsed.replace(second=0, microsecond=0)


def _clock(value: Any) -> time:
    text = str(value).strip()
    if len(text) >= 4 and text[1] == ":":
        text = "0" + text  # 9:00 means 09:00
    try:
        return time.fromisoformat(text).replace(second=0, microsecond=0)
    except ValueError:
        raise RuleError("scheduler.invalid_time", f"Not a time of day: {value!r}; use HH:MM (24-hour).") from None


def _parse_utc(value: Any) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        raise RuleError("scheduler.invalid_time", f"Not an ISO instant: {value!r}.") from None
    if parsed.tzinfo is None:
        raise RuleError("scheduler.invalid_time", "An instant needs an offset or Z.")
    return parsed.astimezone(UTC)


def _iso(instant: datetime) -> str:
    return instant.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _span(seconds: float) -> str:
    minutes = int(round(seconds / 60))
    if minutes < 1:
        return f"{int(seconds)} s"
    if minutes < 60:
        return f"{minutes} min"
    hours, minutes = divmod(minutes, 60)
    if hours < 48:
        return f"{hours} h" + (f" {minutes} min" if minutes else "")
    days, hours = divmod(hours, 24)
    return f"{days} d" + (f" {hours} h" if hours else "")
