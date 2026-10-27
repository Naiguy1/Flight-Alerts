"""SerpApi Google Flights adapter; round-trip metadata is outbound-only."""

from urllib.parse import urlencode

from fetcher.base import FlightFetcher, FlightOffer, ProviderError, positive_price, sanitize_booking_url
from fetcher.transport import ProviderTransport


class GoogleFlightsFetcher(FlightFetcher):
    name = "serpapi"
    BASE_URL = "https://serpapi.com/search"

    def __init__(self, api_key: str, currency: str = "USD", *, transport=None,
                 budget_callback=None, timeout_seconds=20, deadline_seconds=60):
        if currency != "USD":
            raise ValueError("Only USD pricing is supported")
        self.api_key = api_key
        self.currency = currency
        self.transport = transport if transport is not None else ProviderTransport(
            self.name, budget_callback=budget_callback, timeout_seconds=timeout_seconds,
            deadline_seconds=deadline_seconds,
        )

    def close(self):
        self.transport.close()

    def _build_google_flights_url(self, origin, destination, outbound_date, return_date):
        query = f"Flights to {destination} from {origin} on {outbound_date} through {return_date}"
        return "https://www.google.com/travel/flights?" + urlencode({"q": query, "curr": self.currency})

    @staticmethod
    def _extract_typical_midpoint(price_insights):
        typical = price_insights.get("typical_price_range")
        if not isinstance(typical, list) or len(typical) != 2:
            return None
        try:
            low, high = (positive_price(value) for value in typical)
            if high < low:
                return None
            return positive_price((low + high) / 2)
        except ValueError:
            return None

    def fetch_offers(self, origin, destination, destination_city, country_code,
                     outbound_date, return_date):
        if not self.api_key:
            raise ProviderError("provider_disabled")
        query = dict(origin=origin, destination=destination, destination_city=destination_city,
                     country_code=country_code, outbound_date=outbound_date, return_date=return_date)
        try:
            FlightOffer(**query, price_usd=1)
        except ValueError:
            raise ProviderError("invalid_query") from None
        data = self.transport.get_json(self.BASE_URL, params={
            "engine": "google_flights", "departure_id": origin, "arrival_id": destination,
            "outbound_date": outbound_date, "return_date": return_date, "currency": "USD",
            "hl": "en", "gl": "us", "type": "1", "api_key": self.api_key,
        })
        if not isinstance(data, dict):
            raise ProviderError("invalid_payload")
        if data.get("error"):
            raise ProviderError("provider_rejected")
        parameters = data.get("search_parameters")
        if parameters is not None:
            if not isinstance(parameters, dict):
                raise ProviderError("invalid_payload")
            for name, expected in {
                "departure_id": origin, "arrival_id": destination,
                "outbound_date": outbound_date, "return_date": return_date,
                "currency": "USD", "type": "1",
            }.items():
                if name in parameters and str(parameters[name]) != expected:
                    raise ProviderError("mismatched_query")
        metadata = data.get("search_metadata") or {}
        if not isinstance(metadata, dict):
            raise ProviderError("invalid_payload")
        if metadata.get("status") == "Error":
            raise ProviderError("provider_rejected")
        if "best_flights" not in data and "other_flights" not in data:
            raise ProviderError("invalid_payload")
        best, other = data.get("best_flights", []), data.get("other_flights", [])
        if not isinstance(best, list) or not isinstance(other, list):
            raise ProviderError("invalid_payload")
        if len(best) + len(other) > 500:
            raise ProviderError("response_too_large")
        insights = data.get("price_insights") or {}
        if not isinstance(insights, dict):
            raise ProviderError("invalid_payload")
        typical = self._extract_typical_midpoint(insights)
        fallback_url = self._build_google_flights_url(origin, destination, outbound_date, return_date)
        try:
            booking_url = sanitize_booking_url(metadata.get("google_flights_url")) or fallback_url
        except ValueError:
            booking_url = fallback_url
        offers = []
        malformed = 0
        for row in best + other:
            try:
                if not isinstance(row, dict):
                    raise ValueError("Invalid offer")
                legs = row.get("flights", [])
                if not isinstance(legs, list) or len(legs) > 20 or any(not isinstance(leg, dict) for leg in legs):
                    raise ValueError("Invalid legs")
                for leg, name, expected in (
                    (legs[0] if legs else {}, "departure_airport", origin),
                    (legs[-1] if legs else {}, "arrival_airport", destination),
                ):
                    airport = leg.get(name)
                    if airport is not None:
                        if not isinstance(airport, dict) or ("id" in airport and airport["id"] != expected):
                            raise ValueError("Mismatched itinerary")
                airlines = sorted({leg["airline"] for leg in legs
                                   if isinstance(leg.get("airline"), str) and leg["airline"]})
                layovers = row.get("layovers")
                count = len(layovers) if isinstance(layovers, list) else max(0, len(legs) - 1)
                offers.append(FlightOffer(
                    **query, price_usd=row.get("price"), airlines=airlines or ["Multiple Airlines"],
                    layovers=count, source="Google Flights (SerpApi)", booking_url=booking_url,
                    provider_typical_price_usd=typical, metadata_scope="outbound",
                ))
            except (ValueError, TypeError, KeyError, OverflowError):
                malformed += 1
        if malformed and not offers:
            raise ProviderError("invalid_offers")
        return offers
