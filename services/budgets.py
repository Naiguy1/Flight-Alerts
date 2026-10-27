"""Paced requests with authoritative cross-process, rolling-window quotas."""
from __future__ import annotations

import threading
import time


class ProviderBudgets:
    def __init__(self, db, *, clock=time.monotonic, sleep=time.sleep):
        self.db, self.clock, self.sleep = db, clock, sleep
        self._lock = threading.Lock()
        self._next: dict[str, float] = {}

    def consume(self, provider: str, per_minute: int, daily_budget: int, *, stop_event=None,
                deadline: float | None = None, heartbeat=None) -> bool:
        if not 1 <= int(per_minute) <= 100000 or not 1 <= int(daily_budget) <= 1000000:
            raise ValueError("Invalid quota")
        with self._lock:
            now = self.clock()
            due = max(now, self._next.get(provider, now))
            # A small margin avoids floating-point/clock-boundary rolling-window denials.
            self._next[provider] = due + 60.0 / per_minute + 0.01
        while due > self.clock():
            if stop_event is not None and stop_event.is_set():
                return False
            if deadline is not None and self.clock() >= deadline:
                return False
            if heartbeat:
                heartbeat()
            delay = min(0.5, due - self.clock())
            if deadline is not None:
                delay = min(delay, max(0.0, deadline - self.clock()))
            if stop_event is None:
                self.sleep(delay)
            elif stop_event.wait(delay):
                return False
        if (stop_event is not None and stop_event.is_set()) or (deadline is not None and self.clock() >= deadline):
            return False
        return self.db.provider_budget(provider, per_minute, daily_budget)
