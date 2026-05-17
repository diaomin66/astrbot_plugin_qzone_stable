"""Cron-style scheduling helpers for plugin background jobs."""

from __future__ import annotations

import random
from collections.abc import Callable
from datetime import datetime, timedelta


def cron_delay_seconds(
    cron: str,
    offset_seconds: int,
    *,
    now: datetime | None = None,
    randint: Callable[[int, int], int] = random.randint,
) -> float:
    current = now or datetime.now()
    target = cron_next_after(cron, current)
    if target is None:
        return 0.0
    offset = int(offset_seconds or 0)
    if offset > 0:
        target += timedelta(seconds=randint(-offset, offset))
        if target <= current:
            target = current + timedelta(seconds=1)
    return max(1.0, (target - current).total_seconds())


def cron_next_after(cron: str, now: datetime) -> datetime | None:
    parts = str(cron or "").split()
    if len(parts) == 2:
        fields = [parts[0], parts[1], "*", "*", "*"]
    elif len(parts) >= 5:
        fields = parts[:5]
    else:
        return None
    candidate = (now + timedelta(minutes=1)).replace(second=0, microsecond=0)
    for _ in range(366 * 24 * 60 + 1):
        if cron_fields_match(fields, candidate):
            return candidate
        candidate += timedelta(minutes=1)
    return None


def cron_fields_match(fields: list[str], candidate: datetime) -> bool:
    minute, hour, day, month, weekday = fields
    if not cron_field_matches(minute, candidate.minute, 0, 59):
        return False
    if not cron_field_matches(hour, candidate.hour, 0, 23):
        return False
    if not cron_field_matches(month, candidate.month, 1, 12):
        return False

    day_any = cron_field_is_any(day)
    weekday_any = cron_field_is_any(weekday)
    day_match = cron_field_matches(day, candidate.day, 1, 31)
    cron_weekday = (candidate.weekday() + 1) % 7
    weekday_match = cron_field_matches(weekday, cron_weekday, 0, 7, weekday=True)
    if not day_any and not weekday_any:
        return day_match or weekday_match
    return day_match and weekday_match


def cron_field_is_any(field: str) -> bool:
    return str(field or "").strip() in {"*", "?"}


def cron_field_matches(field: str, value: int, minimum: int, maximum: int, *, weekday: bool = False) -> bool:
    text = str(field or "").strip()
    if text in {"*", "?"}:
        return True

    def normalize(raw: str) -> int | None:
        try:
            number = int(raw)
        except ValueError:
            return None
        if weekday and number == 7:
            number = 0
        if minimum <= number <= maximum:
            return number
        return None

    for item in text.split(","):
        item = item.strip()
        if not item:
            return False
        base = item
        step = 1
        if "/" in item:
            base, step_text = item.split("/", 1)
            try:
                step = int(step_text)
            except ValueError:
                return False
            if step <= 0:
                return False
        if base in {"*", "?"}:
            if (value - minimum) % step == 0:
                return True
            continue
        if "-" in base:
            start_text, end_text = base.split("-", 1)
            start = normalize(start_text)
            end = normalize(end_text)
            if start is None or end is None or start > end:
                return False
            if start <= value <= end and (value - start) % step == 0:
                return True
            continue
        number = normalize(base)
        if number is None:
            return False
        if value == number:
            return True
    return False
