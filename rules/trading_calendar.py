from __future__ import annotations

from datetime import datetime, time
from functools import lru_cache
from zoneinfo import ZoneInfo

import exchange_calendars as xcals
import pandas as pd

ET = ZoneInfo("America/New_York")


@lru_cache(maxsize=1)
def exchange():
    return xcals.get_calendar("XNYS", start="2019-01-01", end="2027-12-31")


def is_session(day: str) -> bool:
    return bool(exchange().is_session(day))


def session_bounds(day: str):
    cal = exchange()
    return cal.session_open(day).to_pydatetime(), cal.session_close(day).to_pydatetime()


def completed_session(now: datetime) -> str:
    if now.tzinfo is None:
        raise ValueError("Timezone required")
    cal = exchange()
    day = now.astimezone(ET).date().isoformat()
    candidate = cal.date_to_session(day, direction="previous")
    if cal.session_close(candidate).to_pydatetime() > now:
        candidate = cal.previous_session(candidate)
    return candidate.date().isoformat()


def target_age(signal_date: str, now: datetime) -> int:
    if not is_session(signal_date):
        raise ValueError("Signal date is not an exchange session")
    last = completed_session(now)
    if signal_date > last:
        raise ValueError("Future or incomplete signal date")
    return len(exchange().sessions_in_range(signal_date, last)) - 1


def rebalance_signal(day: str, anchor: str = "2021-11-18", every: int = 5) -> bool:
    if not is_session(day) or day < anchor:
        return False
    return (len(exchange().sessions_in_range(anchor, day)) - 1) % every == 0


def trading_window(now: datetime, emergency: bool = False) -> bool:
    local = now.astimezone(ET)
    day = local.date().isoformat()
    if not is_session(day):
        return False
    opened, closed = session_bounds(day)
    if not opened <= now < closed:
        return False
    if emergency:
        return True
    if day == "2026-11-27":
        return False
    return time(9, 35) <= local.time().replace(tzinfo=None) <= time(15, 45)
