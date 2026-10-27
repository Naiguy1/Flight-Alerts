"""Validated domain objects and safe errors shared by provider adapters."""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
import math
import re
from typing import List, Optional
from urllib.parse import unquote, urlsplit, urlunsplit


class ProviderError(Exception):
    """Operational failure containing application-controlled identifiers only."""

    def __init__(self, category: str, retryable: bool = False,
                 retry_after_seconds: float | None = None,
                 status_code: int | None = None):
        self.category = category if re.fullmatch(r"[a-z_]{1,48}", category) else "provider_error"
        self.retryable = bool(retryable)
        self.retry_after_seconds = retry_after_seconds
        self.status_code = status_code
        super().__init__(self.category)


def validate_iso_date(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError("Invalid date")
    date.fromisoformat(value)
    return value


def validate_flight_iata(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("Invalid flight number")
    value = value.strip().upper()
    if not re.fullmatch(r"[A-Z0-9]{2}[0-9]{1,4}[A-Z]?", value):
        raise ValueError("Invalid flight number")
    return value


def validate_airport(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Z]{3}", value):
        raise ValueError("Invalid airport")
    return value


def _safe_text(value: str, maximum: int, *, required: bool = False) -> str:
    if not isinstance(value, str) or len(value) > maximum or (required and not value.strip()):
        raise ValueError("Invalid text field")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError("Invalid text field")
    return value


def positive_price(value: float) -> float:
    if isinstance(value, bool):
        raise ValueError("Invalid price")
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        raise ValueError("Invalid price") from None
    if not math.isfinite(number) or not 0 < number <= 1_000_000:
        raise ValueError("Invalid price")
    return number


def sanitize_booking_url(raw: str) -> str:
    """Allow only the booking destinations supported by the shipped adapters."""
    if raw is None or raw == "":
        return ""
    if not isinstance(raw, str) or len(raw) > 2048:
        raise ValueError("Invalid booking URL")
    if any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in raw):
        raise ValueError("Invalid booking URL")
    try:
        parsed = urlsplit(raw)
        host = (parsed.hostname or "").lower()
        port = parsed.port
    except ValueError:
        raise ValueError("Invalid booking URL") from None
    if (parsed.scheme.lower() != "https" or parsed.username is not None
            or parsed.password is not None or port not in (None, 443) or parsed.fragment
            or host not in {"google.com", "www.google.com", "kiwi.com", "www.kiwi.com"}):
        raise ValueError("Invalid booking URL")
    decoded_path = unquote(parsed.path)
    if "\\" in decoded_path or any(segment in {".", ".."} for segment in decoded_path.split("/")):
        raise ValueError("Invalid booking URL")
    if host in {"google.com", "www.google.com"} and not (
        parsed.path == "/travel/flights" or parsed.path.startswith("/travel/flights/")
    ):
        raise ValueError("Invalid booking URL")
    return urlunsplit(("https", host, parsed.path, parsed.query, ""))


@dataclass
class FlightOffer:
    origin: str
    destination: str
    destination_city: str
    country_code: str
    outbound_date: str
    return_date: str
    price_usd: float
    airlines: List[str] = field(default_factory=list)
    layovers: int = 0
    source: str = ""
    booking_url: str = ""
    provider_typical_price_usd: Optional[float] = None
    observed_at: str | None = None
    currency: str = "USD"
    metadata_scope: str = "outbound"

    def __post_init__(self):
        validate_airport(self.origin)
        validate_airport(self.destination)
        if self.origin == self.destination:
            raise ValueError("Origin and destination must differ")
        _safe_text(self.destination_city, 100, required=True)
        if not isinstance(self.country_code, str) or not re.fullmatch(r"[A-Z]{2}", self.country_code):
            raise ValueError("Invalid country")
        validate_iso_date(self.outbound_date)
        validate_iso_date(self.return_date)
        if not 1 <= self.trip_duration_days <= 366:
            raise ValueError("Invalid trip duration")
        self.price_usd = positive_price(self.price_usd)
        if self.provider_typical_price_usd is not None:
            self.provider_typical_price_usd = positive_price(self.provider_typical_price_usd)
        if self.currency != "USD":
            raise ValueError("Only USD pricing is supported")
        if self.metadata_scope not in {"outbound", "roundtrip"}:
            raise ValueError("Invalid metadata scope")
        if (not isinstance(self.airlines, list) or len(self.airlines) > 20
                or any(not _safe_text(item, 100, required=True) for item in self.airlines)):
            raise ValueError("Invalid airlines")
        if isinstance(self.layovers, bool) or not isinstance(self.layovers, int) or not 0 <= self.layovers <= 20:
            raise ValueError("Invalid layovers")
        _safe_text(self.source, 80)
        self.booking_url = sanitize_booking_url(self.booking_url)
        if self.observed_at is None:
            observed = datetime.now(timezone.utc)
        else:
            if not isinstance(self.observed_at, str) or len(self.observed_at) > 40:
                raise ValueError("Invalid observation time")
            try:
                observed = datetime.fromisoformat(self.observed_at.replace("Z", "+00:00"))
            except ValueError:
                raise ValueError("Invalid observation time") from None
            if observed.tzinfo is None:
                raise ValueError("Observation time must include timezone")
        self.observed_at = observed.astimezone(timezone.utc).isoformat()

    @property
    def trip_duration_days(self) -> int:
        return (date.fromisoformat(self.return_date) - date.fromisoformat(self.outbound_date)).days


class FlightFetcher(ABC):
    name = "provider"

    @abstractmethod
    def fetch_offers(self, origin: str, destination: str, destination_city: str,
                     country_code: str, outbound_date: str, return_date: str) -> List[FlightOffer]:
        raise NotImplementedError
