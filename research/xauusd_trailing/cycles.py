from __future__ import annotations

from datetime import datetime, timedelta, timezone
from math import floor
from zoneinfo import ZoneInfo


def resolve_cycle_anchor(anchor: datetime | str, timezone_name: str) -> datetime:
    tz = ZoneInfo(timezone_name)
    parsed = datetime.fromisoformat(anchor) if isinstance(anchor, str) else anchor
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=tz)
    return parsed.astimezone(tz)


def cycle_window(
    at: datetime,
    timezone_name: str,
    cycle_hours: int,
    anchor: datetime | str,
) -> tuple[str, datetime, datetime]:
    if cycle_hours < 1:
        raise ValueError("cycle_hours must be positive")
    tz = ZoneInfo(timezone_name)
    local = at.astimezone(tz)
    anchor_local = resolve_cycle_anchor(anchor, timezone_name)
    interval = timedelta(hours=cycle_hours)
    anchor_utc = anchor_local.astimezone(timezone.utc)
    elapsed = (local.astimezone(timezone.utc) - anchor_utc).total_seconds()
    start_utc = anchor_utc + floor(elapsed / interval.total_seconds()) * interval
    start = start_utc.astimezone(tz)
    end = (start_utc + interval).astimezone(tz)
    cycle_id = f"{start.date().isoformat()}T{start.hour:02d}:{start.minute:02d}"
    return cycle_id, start, end
