"""Time source for the bridge. Tests use FakeClock, so nothing in the suite really sleeps."""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime: ...
    def monotonic(self) -> float: ...
    def sleep(self, seconds: float) -> None: ...


class RealClock:
    def now(self) -> datetime:
        return datetime.now().astimezone()

    def monotonic(self) -> float:
        return time.monotonic()

    def sleep(self, seconds: float) -> None:
        if seconds > 0:
            time.sleep(seconds)


IST = timezone(timedelta(hours=5, minutes=30))


class FakeClock:
    def __init__(self, start: datetime | None = None) -> None:
        self._now = start or datetime(2026, 10, 6, 12, 0, 0, tzinfo=IST)
        self._mono = 1000.0
        self.slept: list[float] = []

    def now(self) -> datetime:
        return self._now

    def monotonic(self) -> float:
        return self._mono

    def sleep(self, seconds: float) -> None:
        seconds = max(0.0, float(seconds))
        self.slept.append(seconds)
        self.advance(seconds)

    def advance(self, seconds: float) -> None:
        self._now += timedelta(seconds=seconds)
        self._mono += seconds


def iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")
