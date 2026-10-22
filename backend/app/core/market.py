from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import exchange_calendars as xcals

EASTERN = ZoneInfo("America/New_York")


class MarketCalendar:
    def __init__(self) -> None:
        self.calendar = xcals.get_calendar("XNYS")

    def is_session(self, day: date) -> bool:
        return bool(self.calendar.is_session(day.isoformat()))

    def bounds(self, day: date) -> tuple[datetime, datetime]:
        session = day.isoformat()
        return (
            self.calendar.session_open(session).to_pydatetime(),
            self.calendar.session_close(session).to_pydatetime(),
        )

    def is_open(self, now: datetime) -> bool:
        day = now.astimezone(EASTERN).date()
        if not self.is_session(day):
            return False
        start, end = self.bounds(day)
        return start <= now < end

    def next_entry(self, now: datetime, *, hour: int = 10, minute: int = 30) -> datetime:
        day = now.astimezone(EASTERN).date()
        for offset in range(370):
            candidate = day + timedelta(days=offset)
            at = datetime.combine(candidate, datetime.min.time(), EASTERN).replace(
                hour=hour, minute=minute
            )
            if at > now and self.is_session(candidate):
                return at
        raise RuntimeError("No trading session found in the next year")

    def sessions_between(self, start: date, end: date) -> int:
        return len(self.calendar.sessions_in_range(start.isoformat(), end.isoformat()))
