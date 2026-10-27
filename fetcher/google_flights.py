import logging
import urllib.parse
from typing import List, Optional
import requests
from fetcher.base import FlightFetcher, FlightOffer

logger = logging.getLogger(__name__)


class GoogleFlightsFetcher(FlightFetcher):
    BASE_URL = "https://serpapi.com/search"

    def __init__(self, api_key: str, currency: str = "USD"):
        self.api_key = api_key
        self.currency = currency

    def _build_google_flights_url(
        self, origin: str, destination: str, outbound_date: str, return_date: str
    ) -> str:
        query = f"Flights to {destination} from {origin} on {outbound_date} through {return_date}"
        return f"https://www.google.com/travel/flights?q={urllib.parse.quote(query)}&curr={self.currency}"

    def _extract_typical_midpoint(self, price_insights: dict) -> Optional[float]:
        typical_range = price_insights.get("typical_price_range")
        if isinstance(typical_range, list) and len(typical_range) == 2:
            try:
                return (float(typical_range[0]) + float(typical_range[1])) / 2.0
            except (ValueError, TypeError):
                return None
        return None

    def fetch_offers(
        self,
        origin: str,
        destination: str,
        destination_city: str,
        country_code: str,
        outbound_date: str,
        return_date: str,
    ) -> List[FlightOffer]:
        if not self.api_key:
            return []

        params = {
            "engine": "google_flights",
            "departure_id": origin,
            "arrival_id": destination,
            "outbound_date": outbound_date,
            "return_date": return_date,
            "currency": self.currency,
            "hl": "en",
            "gl": "us",
            "type": "1",
            "api_key": self.api_key,
        }

        try:
            response = requests.get(self.BASE_URL, params=params, timeout=20)
            response.raise_for_status()
            data = response.json()
        except requests.RequestException as exc:
            logger.error("SerpApi error for %s->%s: %s", origin, destination, exc)
            return []

        price_insights = data.get("price_insights", {})
        typical_midpoint = self._extract_typical_midpoint(price_insights)
        google_url = data.get("search_metadata", {}).get(
            "google_flights_url",
            self._build_google_flights_url(origin, destination, outbound_date, return_date),
        )

        raw_flights = data.get("best_flights", []) + data.get("other_flights", [])
        offers: List[FlightOffer] = []

        for item in raw_flights:
            price = item.get("price")
            if price is None:
                continue

            legs = item.get("flights", [])
            airlines = sorted({leg.get("airline", "Unknown Airline") for leg in legs if leg.get("airline")})
            layovers_list = item.get("layovers", [])
            layover_count = len(layovers_list) if isinstance(layovers_list, list) else max(0, len(legs) - 1)

            offers.append(
                FlightOffer(
                    origin=origin,
                    destination=destination,
                    destination_city=destination_city,
                    country_code=country_code,
                    outbound_date=outbound_date,
                    return_date=return_date,
                    price_usd=float(price),
                    airlines=airlines or ["Multiple Airlines"],
                    layovers=layover_count,
                    source="Google Flights (SerpApi)",
                    booking_url=google_url,
                    provider_typical_price_usd=typical_midpoint,
                )
            )

        return offers
