import logging
from datetime import datetime
from typing import List
import requests
from fetcher.base import FlightFetcher, FlightOffer

logger = logging.getLogger(__name__)


class KiwiTequilaFetcher(FlightFetcher):
    BASE_URL = "https://tequila-api.kiwi.com/v2/search"

    def __init__(self, api_key: str, currency: str = "USD"):
        self.api_key = api_key
        self.currency = currency

    @staticmethod
    def _format_kiwi_date(iso_date: str) -> str:
        dt = datetime.strptime(iso_date, "%Y-%m-%d")
        return dt.strftime("%d/%m/%Y")

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

        headers = {"apikey": self.api_key}
        params = {
            "fly_from": f"airport:{origin}",
            "fly_to": f"airport:{destination}",
            "date_from": self._format_kiwi_date(outbound_date),
            "date_to": self._format_kiwi_date(outbound_date),
            "return_from": self._format_kiwi_date(return_date),
            "return_to": self._format_kiwi_date(return_date),
            "curr": self.currency,
            "adults": 1,
            "limit": 5,
            "sort": "price",
        }

        try:
            response = requests.get(self.BASE_URL, headers=headers, params=params, timeout=20)
            response.raise_for_status()
            payload = response.json()
        except requests.RequestException as exc:
            logger.error("Kiwi Tequila API error for %s->%s: %s", origin, destination, exc)
            return []

        offers: List[FlightOffer] = []
        for row in payload.get("data", []):
            price = row.get("price")
            if price is None:
                continue

            airlines = row.get("airlines", ["Multiple Airlines"])
            route_segments = row.get("route", [])
            total_layovers = max(0, len(route_segments) - 2)
            deep_link = row.get("deep_link", "https://www.kiwi.com")
            detected_country = row.get("countryTo", {}).get("code", country_code) or country_code
            detected_city = row.get("cityTo", destination_city) or destination_city

            offers.append(
                FlightOffer(
                    origin=origin,
                    destination=destination,
                    destination_city=detected_city,
                    country_code=detected_country,
                    outbound_date=outbound_date,
                    return_date=return_date,
                    price_usd=float(price),
                    airlines=airlines,
                    layovers=total_layovers,
                    source="Kiwi Tequila",
                    booking_url=deep_link,
                )
            )

        return offers
