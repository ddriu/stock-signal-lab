"""Resolve the closing session of the existing daily job and its recovery run."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo


def daily_digest_session_date(*, now: datetime | None = None) -> date | None:
    """Return the target session for 23:00–06:00 in Europe/Madrid.

    After midnight a delayed/recovery run belongs to the preceding evening,
    including Friday's report on Saturday morning. Sunday evening and Monday
    morning are not new sessions. Exchange calendars, actual quote dates and
    completed-session checks in the runner remain authoritative on holidays.

    Returning the date (not only a boolean) prevents a delayed run from asking
    for prices of the new day or mixing in an already-open Asian session.
    Starts at/after 06:00 are rejected rather than sending an old digest during
    the next trading day. The immutable per-session claim prevents duplicates.
    """

    clock = now or datetime.now(ZoneInfo("Europe/Madrid"))
    if clock.tzinfo is None or clock.utcoffset() is None:
        return None
    local = clock.astimezone(ZoneInfo("Europe/Madrid"))
    if local.hour == 23:
        session = local.date()
    elif local.hour < 6:
        session = local.date() - timedelta(days=1)
    else:
        return None
    return session if session.weekday() < 5 else None


def daily_digest_window(*, event_name: str = "", now: datetime | None = None) -> bool:
    """Preserve explicit manual reviews; scheduled runs need a target session."""

    return event_name != "schedule" or daily_digest_session_date(now=now) is not None
