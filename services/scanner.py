"""Bounded, isolated provider queries and durable alert reservations."""
from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone

from analyzer.evaluator import DealEvaluator
from services.budgets import ProviderBudgets
from settings import validate_config

logger = logging.getLogger(__name__)
_PERMANENT_FOR_SCAN = {"budget_exhausted", "rate_limited", "authentication_failed", "permission_denied",
                       "authentication", "account_access", "quota_exhausted", "provider_disabled", "disabled", "provider_rejected"}


def route_baselines(destinations: list[dict]) -> dict[str, dict[str, float]]:
    return {item["code"]: item["baseline_avg_usd"] for item in destinations}


def evaluator_for(db, config: dict) -> DealEvaluator:
    filters = config["filters"]
    return DealEvaluator(
        db, discount_threshold_percent=filters["discount_threshold_percent"],
        min_historical_samples=filters["min_historical_samples_for_db_avg"],
        excluded_country_codes=filters["excluded_country_codes"],
        allowed_domestic_airports=filters["allowed_domestic_airports"],
        blocked_destinations=filters["blocked_destinations"],
        route_baselines=route_baselines(config["monitored_destinations"]),
        allowed_origins=config["scanner"]["origins"],
    )


def build_fetchers(runtime, config: dict, db, *, budgets=None, stop_event=None, deadline=None, heartbeat=None,
                   provider_names: set[str] | None = None):
    """One set per pool thread; sessions are never shared across query threads."""
    scanner = config["scanner"]
    budgets = budgets or ProviderBudgets(db)
    options = {
        "timeout_seconds": scanner["request_timeout_seconds"],
        # Rate-limit waits are inside transport's overall deadline. Allow a small
        # bounded queue at low configured rates; socket inactivity stays bounded.
        "deadline_seconds": min(600, max(scanner["request_timeout_seconds"] * 3,
                                        180 * scanner["concurrency"] / scanner["provider_requests_per_minute"]
                                        + scanner["request_timeout_seconds"] * 3)),
    }

    def budget(name):
        return lambda: budgets.consume(name, scanner["provider_requests_per_minute"], scanner["provider_daily_budget"],
                                       stop_event=stop_event, deadline=deadline, heartbeat=heartbeat)

    providers = []
    allowed = provider_names if provider_names is not None else {"serpapi", "kiwi", "scraper"}
    try:
        if runtime.serpapi_api_key and "serpapi" in allowed:
            from fetcher.google_flights import GoogleFlightsFetcher
            providers.append(GoogleFlightsFetcher(runtime.serpapi_api_key, budget_callback=budget("serpapi"), **options))
        if runtime.kiwi_api_key and "kiwi" in allowed:
            from fetcher.secondary_aggregator import KiwiTequilaFetcher
            providers.append(KiwiTequilaFetcher(runtime.kiwi_api_key, budget_callback=budget("kiwi"), **options))
        if runtime.enable_scraper and "scraper" in allowed:
            from fetcher.fast_flights_scraper import FastFlightsScraper
            providers.append(FastFlightsScraper(budget_callback=budget("scraper"), **options))
        return providers
    except BaseException:
        for provider in providers:
            provider.close()
        raise


@dataclass
class ScanResult:
    total_windows: int = 0
    windows: int = 0
    completed_windows: int = 0
    observations: int = 0
    alerts_reserved: int = 0
    provider_failures: int = 0
    provider_successes: int = 0
    deadline_reached: bool = False
    canceled: bool = False
    providers_disabled: bool = False
    quota_exhausted: bool = False

    @property
    def success(self) -> bool:
        return (self.total_windows > 0 and self.windows == self.total_windows
                and self.completed_windows == self.total_windows and self.provider_failures == 0
                and not self.deadline_reached and not self.canceled and not self.providers_disabled)

    @property
    def error_category(self) -> str:
        if self.canceled:
            return "scan_canceled"
        if self.deadline_reached:
            return "scan_deadline_exceeded"
        if self.providers_disabled:
            return "providers_not_configured"
        if self.quota_exhausted:
            return "provider_quota_exhausted"
        if self.total_windows == 0:
            return "no_active_search_windows"
        return "incomplete_provider_coverage"


class ScanService:
    def __init__(self, db, runtime, default_config: dict, *, fetcher_factory=None, heartbeat=None, budgets=None,
                 stop_event=None, clock=time.monotonic):
        self.db, self.runtime, self.default_config = db, runtime, default_config
        self.fetcher_factory = fetcher_factory
        self.heartbeat = heartbeat or (lambda: None)
        self.budgets = budgets or ProviderBudgets(db)
        self.stop_event = stop_event or threading.Event()
        self.clock = clock

    def run(self, *, job: dict | None = None, dry_run: bool = False) -> ScanResult:
        config = validate_config(self.db.get_config(self.default_config))
        scanner, filters = config["scanner"], config["filters"]
        evaluator = evaluator_for(self.db, config)
        result = ScanResult()
        today = datetime.now(timezone.utc).date()
        deadline = self.clock() + scanner["scan_deadline_seconds"]
        stopped = threading.Event()
        local = threading.local()
        created = []
        guard = threading.Lock()
        blocked_providers = set()
        quota_providers = set()
        blocked = {term.lower() for term in filters["blocked_destinations"]}
        queries = []
        for origin in scanner["origins"]:
            for destination in config["monitored_destinations"]:
                if destination["code"].lower() in blocked or destination["city"].lower() in blocked:
                    continue
                for offset in scanner["search_window"]["sample_departure_offsets_days"]:
                    outbound = today + timedelta(days=offset)
                    queries.append(dict(origin=origin, destination=destination["code"],
                                        destination_city=destination["city"], country_code=destination["country_code"],
                                        outbound_date=outbound.isoformat(),
                                        return_date=(outbound + timedelta(days=scanner["search_window"]["trip_duration_days"])).isoformat()))
        result.total_windows = len(queries)
        if not queries:
            return result
        # Rotate coverage between persisted scan jobs when a daily cap prevents
        # a whole scan. A route at the end cannot be permanently starved.
        rotation = ((job or {}).get("id", 1) - 1) * 97 % len(queries)
        queries = queries[rotation:] + queries[:rotation]

        def should_stop():
            return stopped.is_set() or self.stop_event.is_set() or self.clock() >= deadline

        def search(query):
            if not hasattr(local, "fetchers"):
                local.fetchers = (self.fetcher_factory(config) if self.fetcher_factory else
                                  build_fetchers(self.runtime, config, self.db, budgets=self.budgets,
                                                 stop_event=self.stop_event, deadline=deadline, heartbeat=self.heartbeat))
                with guard:
                    created.extend(local.fetchers)
            offers, successes, failures = [], 0, 0
            if not local.fetchers:
                return offers, successes, failures, True
            for provider in local.fetchers:
                if should_stop():
                    break
                name = getattr(provider, "name", "provider")
                with guard:
                    unavailable = name in blocked_providers
                if unavailable:
                    failures += 1
                    continue
                started = self.clock()
                try:
                    found = provider.fetch_offers(**query)
                    found = [offer for offer in found if all(getattr(offer, key) == query[key]
                             for key in ("origin", "destination", "outbound_date", "return_date"))]
                    offers.extend(found)
                    self.db.record_provider_result(name, True, latency_ms=round((self.clock() - started) * 1000), offers=len(found))
                    successes += 1
                except Exception as exc:
                    category = getattr(exc, "category", "provider_operation_failed")
                    if category in _PERMANENT_FOR_SCAN:
                        with guard:
                            blocked_providers.add(name)
                            if category in {"budget_exhausted", "quota_exhausted", "rate_limited"}:
                                quota_providers.add(name)
                    self.db.record_provider_result(name, False, latency_ms=round((self.clock() - started) * 1000), error=category)
                    logger.warning("provider_search_failed", extra={"provider": name, "category": category})
                    failures += 1
            return offers, successes, failures, False

        iterator, pending = iter(queries), set()
        last_renewal = self.clock()
        lease_seconds = scanner["scan_deadline_seconds"] + 300
        pool = ThreadPoolExecutor(max_workers=scanner["concurrency"], thread_name_prefix="flight-query")
        try:
            while True:
                if self.stop_event.is_set():
                    result.canceled = True
                    stopped.set()
                if self.clock() >= deadline:
                    result.deadline_reached = True
                    stopped.set()
                while not stopped.is_set() and len(pending) < scanner["concurrency"]:
                    try:
                        query = next(iterator)
                    except StopIteration:
                        break
                    pending.add(pool.submit(search, query))
                if not pending:
                    break
                finished, pending = wait(pending, timeout=0.5, return_when=FIRST_COMPLETED)
                self.heartbeat()
                if job and self.clock() - last_renewal >= 30:
                    if not self.db.renew_scan(job["id"], job["lease_token"], lease_seconds):
                        stopped.set()
                        raise RuntimeError("Scan lease ownership was lost")
                    last_renewal = self.clock()
                for future in finished:
                    if future.cancelled():
                        continue
                    offers, successes, failures, disabled = future.result()
                    result.windows += 1
                    result.completed_windows += int(successes > 0 and failures == 0)
                    result.provider_successes += successes
                    result.provider_failures += failures
                    result.providers_disabled = result.providers_disabled or disabled
                    if disabled:
                        stopped.set()
                    with guard:
                        names = {getattr(provider, "name", "provider") for provider in created}
                        all_blocked = bool(names) and names <= blocked_providers
                        result.quota_exhausted = result.quota_exhausted or bool(quota_providers)
                    if all_blocked:
                        stopped.set()
                    if not should_stop():
                        deals = evaluator.evaluate_offers(offers)
                        cheapest = min((offer for offer in offers if evaluator.is_eligible_route(offer)), key=lambda offer: offer.price_usd, default=None)
                        if not dry_run:
                            if cheapest is not None:
                                self.db.record_offers([cheapest])
                                result.observations += 1
                            live = validate_config(self.db.get_config(self.default_config))
                            live_evaluator = evaluator_for(self.db, live)
                            active = {item["code"] for item in live["monitored_destinations"]}
                            if not live["filters"]["alerts_paused"]:
                                for deal in deals:
                                    if deal.offer.destination not in active or not live_evaluator.is_eligible_route(deal.offer):
                                        continue
                                    if deal.discount_percent < live["filters"]["discount_threshold_percent"]:
                                        continue
                                    event = self.db.reserve_alert(deal.offer, deal.average_price_usd, deal.discount_percent,
                                        baseline_source=deal.baseline_source, window_hours=live["filters"]["deduplication_window_hours"],
                                        improvement_usd=live["filters"]["price_improvement_usd"],
                                        improvement_percent=live["filters"]["price_improvement_percent"])
                                    result.alerts_reserved += int(event is not None)
                    delay = scanner["request_delay_seconds"]
                    until = min(deadline, self.clock() + delay)
                    while delay and self.clock() < until and not should_stop():
                        self.heartbeat()
                        self.stop_event.wait(min(0.5, max(0.0, until - self.clock())))
                if stopped.is_set():
                    for future in pending:
                        future.cancel()
        finally:
            stopped.set()
            pool.shutdown(wait=True, cancel_futures=True)
            for provider in created:
                try:
                    provider.close()
                except Exception:
                    logger.warning("provider_close_failed")
        logger.info("scan_finished", extra={"offers": result.observations, "job_id": job["id"] if job else None})
        return result
