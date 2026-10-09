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


def parse_window(window: str, tz: str) -> tuple[time, time, ZoneInfo]:
    """"18:00-22:00" -> (18:00, 22:00, zone). Raises ValueError when malformed."""
    try:
        start_s, end_s = window.split("-")
        start, end = _parse_hhmm(start_s.strip()), _parse_hhmm(end_s.strip())
        zone = ZoneInfo(tz)
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"bad publish window {window!r} / time zone {tz!r}: {exc}") from exc
    if end <= start:
        raise ValueError("publish window end must be after its start (same day)")
    return start, end, zone


def next_in_window(t: datetime, window: str, tz: str) -> datetime:
    """The earliest moment >= t that falls inside the daily local window."""
    start, end, zone = parse_window(window, tz)
    local = t.astimezone(zone)
    day = local.date()
    for _ in range(3):
        begin = datetime.combine(day, start, zone)
        finish = datetime.combine(day, end, zone)
        if local < begin:
            return begin.astimezone(timezone.utc)
        if local <= finish:
            return local.astimezone(timezone.utc)
        day += timedelta(days=1)
        local = datetime.combine(day, time(0, 0), zone)
    raise ValueError("could not find a publish slot")
