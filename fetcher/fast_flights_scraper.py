import logging
import re
import urllib.parse
from typing import List
from fast_flights import FlightQuery, Passengers, create_query, get_flights
from fetcher.base import FlightFetcher, FlightOffer

logger = logging.getLogger(__name__)


class FastFlightsScraper(FlightFetcher):
    def __init__(self, currency: str = "USD"):
        self.currency = currency

    @staticmethod
    def _parse_price(price_raw) -> float:
        if isinstance(price_raw, (int, float)):
            return float(price_raw)
        cleaned = re.sub(r"[^\d.]", "", str(price_raw or ""))
        return float(cleaned) if cleaned else 0.0

    def fetch_offers(
        self,
        origin: str,
        destination: str,
        destination_city: str,
        country_code: str,
        outbound_date: str,
        return_date: str,
    ) -> List[FlightOffer]:
        try:
            query = create_query(
                flights=[
                    FlightQuery(date=outbound_date, from_airport=origin, to_airport=destination),
                    FlightQuery(date=return_date, from_airport=destination, to_airport=origin),
                ],
                seat="economy",
                trip="round-trip",
                passengers=Passengers(adults=1),
                currency=self.currency,
            )
            results = get_flights(query)
        except Exception as exc:
            logger.warning("fast-flights scrape failed for %s->%s: %s", origin, destination, exc)
            return []

        q_str = urllib.parse.quote(
            f"Flights to {destination} from {origin} on {outbound_date} through {return_date}"
        )
        booking_url = f"https://www.google.com/travel/flights?q={q_str}&curr={self.currency}"

        offers: List[FlightOffer] = []
        flights_list = getattr(results, "flights", None) or (results if isinstance(results, list) else [])

        for item in flights_list:
            price_val = self._parse_price(getattr(item, "price", 0))
            if price_val <= 0:
                continue

            raw_airlines = getattr(item, "airlines", None) or getattr(item, "name", None)
            if isinstance(raw_airlines, list) and raw_airlines:
                airlines = [str(a) for a in raw_airlines]
            elif raw_airlines:
                airlines = [str(raw_airlines)]
            else:
                airlines = ["Google Flights Carrier"]

            stops = getattr(item, "stops", None)
            if isinstance(stops, int):
                layovers = stops
            else:
                sub_flights = getattr(item, "flights", None)
                layovers = max(0, len(sub_flights) - 1) if isinstance(sub_flights, list) else 0

            offers.append(
                FlightOffer(
                    origin=origin,
                    destination=destination,
                    destination_city=destination_city,
                    country_code=country_code,
                    outbound_date=outbound_date,
                    return_date=return_date,
                    price_usd=price_val,
                    airlines=airlines,
                    layovers=layovers,
                    source="Google Flights (Direct)",
                    booking_url=booking_url,
                )
            )

        return offers
