"""The driver lock: one driver per car at a time.

Whoever sends a driving command to a free car becomes its driver. The driver keeps the
car until they release it or have been idle for ``IDLE_S`` seconds; a command that is
still running never counts as idle. Everything happens on the event loop without an
``await`` between checking and taking the lock, so two callers can't both win.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass

IDLE_S = 120.0


@dataclass
class Driver:
    user_id: int
    login: str
    since: float
    last_active: float
    in_flight: int = 0

    def idle_for(self, now: float) -> float:
        return 0.0 if self.in_flight else max(0.0, now - self.last_active)


class DriverLock:
    def __init__(self, *, idle_s: float = IDLE_S, clock: Callable[[], float] = time.monotonic) -> None:
        self.idle_s = idle_s
        self._clock = clock
        self._drivers: dict[str, Driver] = {}

    def now(self) -> float:
        return self._clock()

    def current(self, car_id: str) -> Driver | None:
        """The car's driver, or None when it's free (including when the driver went idle)."""
        driver = self._drivers.get(car_id)
        if driver is None:
            return None
        if driver.idle_for(self._clock()) >= self.idle_s:
            del self._drivers[car_id]
            return None
        return driver

    def holder_other_than(self, car_id: str, user_id: int) -> Driver | None:
        driver = self.current(car_id)
        return driver if driver is not None and driver.user_id != user_id else None

    def acquire(self, car_id: str, user_id: int, login: str) -> Driver | None:
        """Take or keep the lock. Returns None on success, or the other driver who holds it."""
        other = self.holder_other_than(car_id, user_id)
        if other is not None:
            return other
        now = self._clock()
        driver = self._drivers.get(car_id)
        if driver is None:
            self._drivers[car_id] = Driver(user_id=user_id, login=login, since=now, last_active=now)
        else:
            driver.last_active = now
            driver.login = login
        return None

    def touch(self, car_id: str, user_id: int) -> None:
        driver = self._drivers.get(car_id)
        if driver is not None and driver.user_id == user_id:
            driver.last_active = self._clock()

    @contextmanager
    def driving(self, car_id: str, user_id: int) -> Iterator[None]:
        """Marks a command as running, so the driver isn't idle while it runs."""
        driver = self._drivers.get(car_id)
        if driver is None or driver.user_id != user_id:
            yield
            return
        driver.in_flight += 1
        try:
            yield
        finally:
            driver.in_flight -= 1
            driver.last_active = self._clock()

    def release(self, car_id: str, user_id: int | None = None) -> Driver | None:
        """Free the car. With ``user_id``, only if that user is driving. Returns who was released."""
        driver = self.current(car_id)
        if driver is None or (user_id is not None and driver.user_id != user_id):
            return None
        del self._drivers[car_id]
        return driver

    def release_user(self, user_id: int) -> None:
        """Free every car this user drives (used when an account is suspended)."""
        for car_id in [c for c, d in self._drivers.items() if d.user_id == user_id]:
            del self._drivers[car_id]
