"""Optional Aviationstack real-time/historical flight status adapter.

This deliberately uses HTTPS only and the documented ``/v1/flights`` endpoint.
``flight_date`` historical queries require account access and are limited by the
provider to the previous three calendar months. Future schedule endpoints are a
different product; future watches must wait until their date becomes eligible.
Account access, quotas, data coverage and update frequency remain operator checks.

References (checked 2026-10-27):
https://docs.apilayer.com/aviationstack/docs/quickstart-guide
https://aviationstack.com/faq
https://aviationstack.com/pricing
"""

from __future__ import annotations

import calendar
from datetime import date, datetime, timezone
import math
import re
from typing import Any

from fetcher.base import ProviderError, validate_flight_iata, validate_iso_date


STATUSES = frozenset({"scheduled", "active", "landed", "cancelled", "incident", "diverted"})
GATE_RE = re.compile(r"[A-Za-z0-9 ._/-]{1,32}\Z")


def _historical_cutoff(today: date) -> date:
    month_index = today.year * 12 + today.month - 1 - 3
    year, zero_month = divmod(month_index, 12)
    month = zero_month + 1
    return date(year, month, min(today.day, calendar.monthrange(year, month)[1]))


def _gate(value: Any) -> str | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str) or not GATE_RE.fullmatch(value):
        raise ValueError("invalid gate")
    return value


def _delay(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("invalid delay")
    if not math.isfinite(value) or not 0 <= value <= 10080 or int(value) != value:
        raise ValueError("invalid delay")
    return int(value)


def _timestamp(value: Any) -> str | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str) or len(value) > 64:
        raise ValueError("invalid timestamp")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timezone required")
    return parsed.astimezone(timezone.utc).isoformat()


class FlightStatusFetcher:
    """Fetch one unambiguous dated IATA flight; never silently pick a leg."""

    name = "aviationstack"
    BASE_URL = "https://api.aviationstack.com/v1/flights"

    def __init__(self, api_key: str = "", *, transport=None, **transport_options):
        self._api_key = api_key
        self.transport = transport
        self._transport_options = transport_options

    @property
    def enabled(self) -> bool:
        return bool(self._api_key)

    def _get_transport(self):
        if self.transport is None:
            from fetcher.transport import ProviderTransport

            self.transport = ProviderTransport(self.name, **self._transport_options)
        return self.transport

    def close(self) -> None:
        if self.transport is not None and hasattr(self.transport, "close"):
            self.transport.close()

    @staticmethod
    def _semantic_error(payload: dict) -> None:
        if "error" not in payload:
            return
        error = payload.get("error")
        error_type = error.get("type", "") if isinstance(error, dict) else ""
        if not isinstance(error_type, str):
            raise ProviderError("provider_error")
        if error_type in {"invalid_access_key", "missing_access_key", "inactive_user"}:
            raise ProviderError("authentication")
        if error_type in {"function_access_restricted", "https_access_restricted", "access_restricted"}:
            raise ProviderError("account_access")
        if error_type == "usage_limit_reached":
            raise ProviderError("quota_exhausted")
        if error_type == "rate_limit_reached":
            raise ProviderError("rate_limited", retryable=True, retry_after_seconds=60)
        raise ProviderError("provider_error")

    def fetch_status(self, flight_iata: str, flight_date: str) -> dict:
        flight_iata = validate_flight_iata(flight_iata)
        flight_date = validate_iso_date(flight_date)
        requested_date = date.fromisoformat(flight_date)
        now = datetime.now(timezone.utc)
        today = now.date()
        if requested_date > today:
            raise ProviderError("future_date_unsupported")
        if requested_date < _historical_cutoff(today):
            raise ProviderError("historical_date_unsupported")
        if not self.enabled:
            raise ProviderError("disabled")

        payload = self._get_transport().get_json(
            self.BASE_URL,
            params={
                "access_key": self._api_key,
                "flight_iata": flight_iata,
                "flight_date": flight_date,
                # More than one result must be visible so multi-leg/reused flight
                # numbers cannot silently select an arbitrary itinerary.
                "limit": 100,
            },
        )
        if not isinstance(payload, dict):
            raise ProviderError("invalid_payload")
        self._semantic_error(payload)
        rows = payload.get("data")
        if not isinstance(rows, list) or len(rows) > 100:
            raise ProviderError("invalid_payload")
        pagination = payload.get("pagination")
        if pagination is not None:
            if not isinstance(pagination, dict):
                raise ProviderError("invalid_payload")
            total = pagination.get("total", len(rows))
            if isinstance(total, bool) or not isinstance(total, int) or total < 0:
                raise ProviderError("invalid_payload")
            if total > len(rows):
                raise ProviderError("truncated_payload")

        observed_at = datetime.now(timezone.utc).isoformat()
        matches: list[tuple[tuple, dict]] = []
        invalid_match = False
        for row in rows:
            if not isinstance(row, dict):
                continue
            flight = row.get("flight")
            if not isinstance(flight, dict):
                continue
            if flight.get("iata") != flight_iata or row.get("flight_date") != flight_date:
                continue
            try:
                status = row.get("flight_status")
                if status not in STATUSES:
                    raise ValueError("invalid status")
                departure = row.get("departure")
                arrival = row.get("arrival")
                if not isinstance(departure, dict) or not isinstance(arrival, dict):
                    raise ValueError("invalid itinerary")
                delays = [_delay(departure.get("delay")), _delay(arrival.get("delay"))]
                known_delays = [value for value in delays if value is not None]
                departure_gate = _gate(departure.get("gate"))
                arrival_gate = _gate(arrival.get("gate"))
                live = row.get("live")
                if live is not None and not isinstance(live, dict):
                    raise ValueError("invalid live record")
                provider_updated = _timestamp(live.get("updated")) if live else None
                scheduled = _timestamp(departure.get("scheduled"))
                if scheduled is None:
                    raise ValueError("scheduled departure required to disambiguate flight")
                departure_iata = departure.get("iata")
                arrival_iata = arrival.get("iata")
                if not isinstance(departure_iata, str) or not re.fullmatch(r"[A-Z]{3}", departure_iata):
                    raise ValueError("invalid departure")
                if not isinstance(arrival_iata, str) or not re.fullmatch(r"[A-Z]{3}", arrival_iata):
                    raise ValueError("invalid arrival")
                normalized = {
                    "flight_iata": flight_iata,
                    "flight_date": flight_date,
                    "status": status,
                    "delay_minutes": max(known_delays) if known_delays else None,
                    "departure_gate": departure_gate,
                    "arrival_gate": arrival_gate,
                    "updated_at": provider_updated or observed_at,
                    "observed_at": observed_at,
                    "timestamp_source": "provider" if provider_updated else "observation",
                    "source": self.name,
                }
                identity = (
                    departure_iata, arrival_iata, scheduled, status,
                    normalized["delay_minutes"], departure_gate, arrival_gate,
                )
                matches.append((identity, normalized))
            except (ValueError, TypeError, OverflowError):
                invalid_match = True

        if invalid_match:
            # A second malformed row may represent another leg of the same
            # flight number; selecting a valid row would hide that ambiguity.
            raise ProviderError("invalid_payload")
        if not matches:
            return {}
        if len({identity for identity, _ in matches}) != 1:
            raise ProviderError("ambiguous_flight")
        return matches[0][1]
