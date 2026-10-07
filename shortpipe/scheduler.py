"""Upload slot planning: N evenly spaced slots per day inside a local time window."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo


def _parse_hhmm(value: str) -> time:
    hour, minute = value.split(":")
    return time(int(hour), int(minute))


def daily_slots(day: date, per_day: int, start: str, end: str, tz: str) -> list[datetime]:
    """Return `per_day` UTC datetimes evenly spread over [start, end] local time."""
    zone = ZoneInfo(tz)
    begin = datetime.combine(day, _parse_hhmm(start), zone)
    finish = datetime.combine(day, _parse_hhmm(end), zone)
    if finish <= begin:
        raise ValueError("schedule.window_end must be after window_start")
    step = (finish - begin) / per_day
    # Place each slot in the middle of its segment.
    return [(begin + step * i + step / 2).astimezone(timezone.utc) for i in range(per_day)]


def to_iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def free_slots(now: datetime, taken: set[str], per_day: int, start: str, end: str,
               tz: str, days_ahead: int) -> list[str]:
    today = now.astimezone(ZoneInfo(tz)).date()
    slots = []
    for offset in range(days_ahead + 1):
        for slot in daily_slots(today + timedelta(days=offset), per_day, start, end, tz):
            iso = to_iso(slot)
            if slot > now and iso not in taken:
                slots.append(iso)
    return slots
