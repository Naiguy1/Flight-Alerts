"""Kiwi Tequila adapter with validated USD round-trip offers."""

from datetime import date

from fetcher.base import FlightFetcher, FlightOffer, ProviderError, sanitize_booking_url
from fetcher.transport import ProviderTransport


class KiwiTequilaFetcher(FlightFetcher):
    name = "kiwi"
    BASE_URL = "https://tequila-api.kiwi.com/v2/search"

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

    @staticmethod
    def _format_kiwi_date(iso_date):
        return date.fromisoformat(iso_date).strftime("%d/%m/%Y")

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
        data = self.transport.get_json(self.BASE_URL, headers={"apikey": self.api_key}, params={
            "fly_from": f"airport:{origin}", "fly_to": f"airport:{destination}",
            "date_from": self._format_kiwi_date(outbound_date),
            "date_to": self._format_kiwi_date(outbound_date),
            "return_from": self._format_kiwi_date(return_date),
            "return_to": self._format_kiwi_date(return_date),
            "curr": "USD", "adults": 1, "limit": 5, "sort": "price",
        })
        if not isinstance(data, dict):
            raise ProviderError("invalid_payload")
        if data.get("error") or data.get("status") in ("error", "ERROR"):
            raise ProviderError("provider_rejected")
        rows = data.get("data")
        if not isinstance(rows, list):
            raise ProviderError("invalid_payload")
        if len(rows) > 500:
            raise ProviderError("response_too_large")
        offers = []
        malformed = 0
        for row in rows:
            try:
                if not isinstance(row, dict):
                    raise ValueError("Invalid offer")
                for field, expected in (("flyFrom", origin), ("flyTo", destination)):
                    if field in row and row[field] != expected:
                        raise ValueError("Mismatched itinerary")
                segments = row.get("route", [])
                if not isinstance(segments, list) or len(segments) > 22 or any(not isinstance(segment, dict) for segment in segments):
                    raise ValueError("Invalid segments")
                country = row.get("countryTo") or {}
                if not isinstance(country, dict):
                    raise ValueError("Invalid country")
                url = sanitize_booking_url(row.get("deep_link")) or "https://www.kiwi.com"
                offers.append(FlightOffer(
                    **{**query, "country_code": country.get("code") or country_code,
                       "destination_city": row.get("cityTo") or destination_city},
                    price_usd=row.get("price"), airlines=row.get("airlines") or ["Multiple Airlines"],
                    layovers=max(0, len(segments) - 2), source="Kiwi Tequila", booking_url=url,
                    metadata_scope="roundtrip",
                ))
            except (ValueError, TypeError, KeyError, OverflowError):
                malformed += 1
        if malformed and not offers:
            raise ProviderError("invalid_offers")
        return offers
