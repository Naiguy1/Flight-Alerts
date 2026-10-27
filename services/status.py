"""Optional frequent status polling independent of long-running fare scans."""
from __future__ import annotations

import logging
import time
import threading
from datetime import datetime, timedelta, timezone

from settings import validate_config

logger = logging.getLogger(__name__)


class StatusService:
    def __init__(self, db, runtime, default_config, budgets, *, fetcher=None, stop_event=None, heartbeat=None):
        self.db, self.runtime, self.default_config, self.budgets = db, runtime, default_config, budgets
        self.fetcher = fetcher
        self.stop_event = stop_event or threading.Event()
        self.heartbeat = heartbeat or (lambda: None)

    def poll(self) -> int:
        if self.stop_event.is_set() or (self.fetcher is None and not self.runtime.aviationstack_api_key):
            return 0
        lease = self.db.acquire_lease("flight-status-poll", lease_seconds=300)
        if lease is None:
            return 0
        changed = 0
        provider = self.fetcher
        deadline = time.monotonic() + 240
        try:
            config = validate_config(self.db.get_config(self.default_config))
            scanner = config["scanner"]
            if provider is None:
                from fetcher.status import FlightStatusFetcher
                provider = FlightStatusFetcher(
                    self.runtime.aviationstack_api_key,
                    budget_callback=lambda: self.budgets.consume("aviationstack", scanner["provider_requests_per_minute"], scanner["provider_daily_budget"], stop_event=self.stop_event, deadline=deadline, heartbeat=self.heartbeat),
                    timeout_seconds=scanner["request_timeout_seconds"],
                    deadline_seconds=60,
                )
            today = datetime.now(timezone.utc).date()
            for watch in self.db.list_watched_flights():
                # Future schedules are a different API product; poll when the
                # flight's date arrives. Stop polling completed/past watches.
                departure = datetime.strptime(watch["flight_date"], "%Y-%m-%d").date()
                if departure > today or departure < today - timedelta(days=1):
                    continue
                self.heartbeat()
                if self.stop_event.is_set() or time.monotonic() >= deadline:
                    break
                if not self.db.renew_lease("flight-status-poll", lease, 300):
                    break
                started = time.monotonic()
                try:
                    status = provider.fetch_status(watch["flight_iata"], watch["flight_date"])
                    self.db.record_provider_result("aviationstack", True, latency_ms=round((time.monotonic() - started) * 1000), offers=int(bool(status)))
                    if status:
                        # State and recipient outbox events are committed together.
                        changed += int(self.db.update_flight_status(watch["id"], status))
                except Exception as exc:
                    category = getattr(exc, "category", type(exc).__name__)
                    self.db.record_provider_result("aviationstack", False, error=category)
                    logger.warning("status_poll_failed", extra={"provider": "aviationstack", "category": category})
                    if category in {"budget_exhausted", "authentication", "account_access", "quota_exhausted", "rate_limited"}:
                        break
            return changed
        finally:
            try:
                if self.fetcher is None and provider is not None:
                    provider.close()
            finally:
                self.db.release_lease("flight-status-poll", lease)
