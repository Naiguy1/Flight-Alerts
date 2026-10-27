from dataclasses import dataclass, field
from typing import List, Optional
from abc import ABC, abstractmethod


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

    @property
    def trip_duration_days(self) -> int:
        from datetime import datetime
        d1 = datetime.strptime(self.outbound_date, "%Y-%m-%d")
        d2 = datetime.strptime(self.return_date, "%Y-%m-%d")
        return (d2 - d1).days


class FlightFetcher(ABC):
    @abstractmethod
    def fetch_offers(
        self,
        origin: str,
        destination: str,
        destination_city: str,
        country_code: str,
        outbound_date: str,
        return_date: str,
    ) -> List[FlightOffer]:
        pass
