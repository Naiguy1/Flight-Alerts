import logging
from dataclasses import dataclass
from typing import Dict, List, Optional, Set, Tuple
from db.crud import DatabaseManager
from fetcher.base import FlightOffer

logger = logging.getLogger(__name__)

ALLOWED_ORIGINS = {"SLC", "LAS"}


@dataclass
class QualifiedDeal:
    offer: FlightOffer
    average_price_usd: float
    discount_percent: float
    baseline_source: str


class DealEvaluator:
    def __init__(
        self,
        db: DatabaseManager,
        discount_threshold_percent: float = 50.0,
        deduplication_window_hours: int = 48,
        min_historical_samples: int = 15,
        excluded_country_codes: Optional[List[str]] = None,
        allowed_domestic_airports: Optional[List[str]] = None,
        blocked_destinations: Optional[List[str]] = None,
        route_baselines: Optional[Dict[str, Dict[str, float]]] = None,
    ):
        self.db = db
        self.discount_threshold_percent = discount_threshold_percent
        self.deduplication_window_hours = deduplication_window_hours
        self.min_historical_samples = min_historical_samples
        self.excluded_countries: Set[str] = {
            c.upper() for c in (excluded_country_codes or ["US"])
        }
        self.allowed_domestic_airports: Set[str] = {
            a.strip().upper() for a in (allowed_domestic_airports or [])
        }
        self.blocked_terms: Set[str] = {
            str(item).strip().lower() for item in (blocked_destinations or [])
        }
        self.route_baselines = route_baselines or {}

    def is_eligible_route(self, offer: FlightOffer) -> bool:
        origin_upper = offer.origin.upper()
        dest_upper = offer.destination.upper()
        city_lower = offer.destination_city.strip().lower()
        country_upper = offer.country_code.upper()

        if origin_upper not in ALLOWED_ORIGINS:
            return False

        if dest_upper.lower() in self.blocked_terms or city_lower in self.blocked_terms:
            logger.info("Suppressed blocked destination %s->%s (%s)", origin_upper, dest_upper, offer.destination_city)
            return False

        if not country_upper:
            return False

        if country_upper in self.excluded_countries and dest_upper not in self.allowed_domestic_airports:
            return False

        return True

    def resolve_average_price(self, offer: FlightOffer) -> Optional[Tuple[float, str]]:
        configured_avg = self.route_baselines.get(offer.destination.upper(), {}).get(offer.origin.upper())
        db_avg, sample_count = self.db.get_historical_route_stats(offer.origin, offer.destination)

        if offer.provider_typical_price_usd and offer.provider_typical_price_usd > 0:
            return offer.provider_typical_price_usd, "Google Flights Typical Price"

        # Use the higher of the configured baseline or rolling DB average so cheap fares
        # never artificially depress the baseline below what you set in the dashboard
        if db_avg is not None and sample_count >= self.min_historical_samples:
            if configured_avg and configured_avg > db_avg:
                return float(configured_avg), "Configured Route Baseline"
            return db_avg, f"Historical DB Average ({sample_count} samples)"

        if configured_avg and configured_avg > 0:
            return float(configured_avg), "Configured Route Baseline"

        if db_avg is not None and db_avg > 0:
            return db_avg, f"Initial DB Average ({sample_count} samples)"

        return None

    @staticmethod
    def calculate_discount_percent(average_price: float, current_price: float) -> float:
        if average_price <= 0:
            return 0.0
        return ((average_price - current_price) / average_price) * 100.0

    def evaluate_offers(self, offers: List[FlightOffer]) -> List[QualifiedDeal]:
        eligible = [o for o in offers if self.is_eligible_route(o)]
        if not eligible:
            return []

        cheapest_offer = min(eligible, key=lambda x: x.price_usd)
        baseline_info = self.resolve_average_price(cheapest_offer)
        self.db.record_offer(cheapest_offer)

        if baseline_info is None:
            return []

        average_price, baseline_source = baseline_info
        discount_pct = self.calculate_discount_percent(average_price, cheapest_offer.price_usd)

        if discount_pct < self.discount_threshold_percent:
            return []

        if self.db.is_duplicate_alert(
            origin=cheapest_offer.origin,
            destination=cheapest_offer.destination,
            outbound_date=cheapest_offer.outbound_date,
            return_date=cheapest_offer.return_date,
            current_price=cheapest_offer.price_usd,
            window_hours=self.deduplication_window_hours,
        ):
            return []

        return [
            QualifiedDeal(
                offer=cheapest_offer,
                average_price_usd=average_price,
                discount_percent=discount_pct,
                baseline_source=baseline_source,
            )
        ]
