"""Local-time gate for the existing daily email job, never a second scheduler."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo


def daily_digest_window(*, event_name: str = "", now: datetime | None = None) -> bool:
    """Allow one local 23:00 hour; tolerate delayed GitHub scheduled starts.

    The workflow uses its supported Europe/Madrid timezone rather than a fixed
    UTC offset. A scheduled weekend is never a new daily report. Actual quote
    dates, not this weekday gate, decide
    whether any tracked exchange completed a new session. Explicit manual
    dispatches remain available and use the same persistent delivery claim.
    """

    if event_name != "schedule":
        return True
    clock = now or datetime.now(ZoneInfo("Europe/Madrid"))
    if clock.tzinfo is None or clock.utcoffset() is None:
        return False
    local = clock.astimezone(ZoneInfo("Europe/Madrid"))
    return local.weekday() < 5 and local.hour == 23
