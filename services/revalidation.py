"""Recheck old queued fares once per originating provider/query, not recipient."""
from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json

from fetcher.base import FlightOffer
from services.scanner import build_fetchers, evaluator_for
from settings import validate_config


class RevalidationError(Exception):
    def __init__(self, category="fare_revalidation_failed"):
        self.category = category
        super().__init__(category)


class FareRevalidator:
    SOURCES = {"Google Flights (SerpApi)": "serpapi", "Kiwi Tequila": "kiwi", "Google Flights (Direct)": "scraper"}

    def __init__(self, db, runtime, default_config, budgets, *, heartbeat=None, stop_event=None,
                 fetcher_factory=None, clock=None):
        self.db, self.runtime, self.default_config, self.budgets = db, runtime, default_config, budgets
        self.heartbeat, self.stop_event = heartbeat or (lambda: None), stop_event
        self.fetcher_factory = fetcher_factory
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def __call__(self, payload: dict) -> bool:
        snapshot = FlightOffer(**payload["offer"])
        provider_name = self.SOURCES.get(snapshot.source)
        if provider_name is None:
            return False
        config = validate_config(self.db.get_config(self.default_config))
        active = {item["code"] for item in config["monitored_destinations"]}
        evaluator = evaluator_for(self.db, config)
        if snapshot.destination not in active or not evaluator.is_eligible_route(snapshot):
            return False
        now = self.clock()
        observed = datetime.fromisoformat(snapshot.observed_at.replace("Z", "+00:00"))
        age = (now - observed).total_seconds()
        candidates = [snapshot]
        if age > 300 or age < -30:
            query = {name: getattr(snapshot, name) for name in (
                "origin", "destination", "destination_city", "country_code", "outbound_date", "return_date")}
            cache_key = hashlib.sha256(json.dumps([provider_name, query], sort_keys=True).encode()).hexdigest()
            cached = self.db.get_provider_cache(cache_key, ttl_seconds=300)
            if cached is None:
                lease_name = "fare-recheck-" + cache_key[:32]
                lease = self.db.acquire_lease(lease_name, lease_seconds=180)
                if lease is None:
                    raise RevalidationError("fare_revalidation_in_progress")
                providers = []
                try:
                    # Another recipient may have populated it just before this lease.
                    cached = self.db.get_provider_cache(cache_key, ttl_seconds=300)
                    if cached is None:
                        self.heartbeat()
                        if self.fetcher_factory:
                            providers = self.fetcher_factory(config, {provider_name})
                        else:
                            # Only one query/provider; do not use a full scan's pacing deadline.
                            config["scanner"]["concurrency"] = 1
                            providers = build_fetchers(self.runtime, config, self.db, budgets=self.budgets,
                                stop_event=self.stop_event, heartbeat=self.heartbeat, provider_names={provider_name})
                        if len(providers) != 1:
                            raise RevalidationError("fare_provider_not_configured")
                        found = providers[0].fetch_offers(**query)
                        cached = [asdict(offer) for offer in found if all(getattr(offer, key) == query[key]
                                  for key in ("origin", "destination", "outbound_date", "return_date"))]
                        self.db.set_provider_cache(cache_key, cached)
                        self.heartbeat()
                except Exception as exc:
                    if isinstance(exc, RevalidationError):
                        raise
                    raise RevalidationError() from None
                finally:
                    for provider in providers:
                        provider.close()
                    self.db.release_lease(lease_name, lease)
            candidates = [FlightOffer(**offer) for offer in cached]
        valid = []
        for candidate in candidates:
            checked = datetime.fromisoformat(candidate.observed_at.replace("Z", "+00:00"))
            if (-30 <= (now - checked).total_seconds() <= 300
                    and candidate.price_usd <= snapshot.price_usd and candidate.source == snapshot.source):
                valid.append(candidate)
        qualified = evaluator.evaluate_offers(valid)
        if not qualified:
            return False
        best = qualified[0]
        payload.update(offer=asdict(best.offer), average_price_usd=best.average_price_usd,
                       discount_percent=best.discount_percent, baseline_source=best.baseline_source)
        return True
