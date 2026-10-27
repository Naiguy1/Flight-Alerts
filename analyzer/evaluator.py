"""Fare qualification without persistence, notification or cooldown side effects."""
from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
from typing import Dict, List, Optional, Set, Tuple

from fetcher.base import FlightOffer


@dataclass(frozen=True)
class QualifiedDeal:
    offer: FlightOffer
    average_price_usd: float
    discount_percent: float
    baseline_source: str


class DealEvaluator:
    def __init__(
        self,
        db,
        discount_threshold_percent: float = 50.0,
        deduplication_window_hours: int = 48,
        min_historical_samples: int = 15,
        excluded_country_codes: Optional[List[str]] = None,
        allowed_domestic_airports: Optional[List[str]] = None,
        blocked_destinations: Optional[List[str]] = None,
        route_baselines: Optional[Dict[str, Dict[str, float]]] = None,
        allowed_origins: Optional[List[str]] = None,
    ):
        self.db = db
        self.discount_threshold_percent = discount_threshold_percent
        self.deduplication_window_hours = deduplication_window_hours
        self.min_historical_samples = min_historical_samples
        self.excluded_countries: Set[str] = {
            c.upper() for c in (["US"] if excluded_country_codes is None else excluded_country_codes)
        }
        self.allowed_domestic_airports = {a.upper() for a in (allowed_domestic_airports or [])}
        self.blocked_terms = {str(item).strip().lower() for item in (blocked_destinations or [])}
        self.route_baselines = route_baselines or {}
        self.allowed_origins = {a.upper() for a in (allowed_origins or ["SLC", "LAS"])}
        self._stats_cache: dict[tuple[str, str], tuple[float | None, int]] = {}

    def is_eligible_route(self, offer: FlightOffer) -> bool:
        return (
            offer.origin in self.allowed_origins
            and offer.destination.lower() not in self.blocked_terms
            and offer.destination_city.strip().lower() not in self.blocked_terms
            and bool(offer.country_code)
            and (offer.country_code not in self.excluded_countries
                 or offer.destination in self.allowed_domestic_airports)
            and getattr(offer, "currency", "USD") == "USD"
            and isfinite(offer.price_usd)
            and offer.price_usd > 0
        )

    def resolve_average_price(self, offer: FlightOffer) -> Optional[Tuple[float, str]]:
        typical = offer.provider_typical_price_usd
        if typical is not None and isfinite(typical) and typical > 0:
            return typical, "Provider typical-price midpoint"
        key = (offer.origin, offer.destination)
        if key not in self._stats_cache:
            self._stats_cache[key] = self.db.get_historical_route_stats(*key)
        historical, count = self._stats_cache[key]
        if historical is not None and isfinite(historical) and historical > 0 and count >= self.min_historical_samples:
            return historical, f"Historical route average ({count} observations, 90 days)"
        configured = self.route_baselines.get(offer.destination, {}).get(offer.origin)
        if configured is not None and isfinite(configured) and configured > 0:
            return float(configured), "Operator-configured reference (not a market guarantee)"
        return None

    @staticmethod
    def calculate_discount_percent(average_price: float, current_price: float) -> float:
        if not isfinite(average_price) or average_price <= 0 or not isfinite(current_price) or current_price <= 0:
            raise ValueError("Prices must be finite and positive")
        return (average_price - current_price) / average_price * 100.0

    def evaluate_offers(self, offers: List[FlightOffer]) -> List[QualifiedDeal]:
        cheapest: dict[tuple, FlightOffer] = {}
        for offer in offers:
            if not self.is_eligible_route(offer):
                continue
            key = (offer.origin, offer.destination, offer.outbound_date, offer.return_date)
            if key not in cheapest or offer.price_usd < cheapest[key].price_usd:
                cheapest[key] = offer
        deals = []
        for offer in cheapest.values():
            baseline = self.resolve_average_price(offer)
            if baseline is None:
                continue
            average, source = baseline
            discount = self.calculate_discount_percent(average, offer.price_usd)
            if discount >= self.discount_threshold_percent:
                deals.append(QualifiedDeal(offer, average, discount, source))
        return sorted(deals, key=lambda deal: deal.offer.price_usd)
