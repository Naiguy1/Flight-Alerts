"""Optional scraper isolated in a killable subprocess with bounded IPC output."""

from contextlib import redirect_stderr, redirect_stdout
import json
import multiprocessing
import os
import re
import time
from urllib.parse import urlencode

from fetcher.base import FlightFetcher, FlightOffer, ProviderError, positive_price


MAX_OUTPUT_BYTES = 262_144


def _scrape_worker(connection, query):
    """Load the optional dependency only in the child; send JSON primitives."""
    try:
        with open(os.devnull, "w", encoding="utf-8") as null_output:
            with redirect_stdout(null_output), redirect_stderr(null_output):
                import fast_flights.parser

                def _safe_parse_js(js: str):
                    import json
                    from fast_flights.model import (
                        Airline, Airport, Alliance, CarbonEmission,
                        Flights, JsMetadata, SimpleDatetime, SingleFlight,
                    )
                    from fast_flights.parser import ResultList, _parse_time
                    from fast_flights.exceptions import FlightsNotFound

                    data = js.split("data:", 1)[1].rsplit(",", 1)[0]
                    if data.endswith("errorHasStatus: true"):
                        raise FlightsNotFound("no flights found; received error")
                    payload = json.loads(data)
                    alliances, airlines = [], []
                    if len(payload) > 7 and payload[7] and len(payload[7]) > 1:
                        (alliances_data, airlines_data) = (payload[7][1][0], payload[7][1][1])
                        for code, name in alliances_data:
                            alliances.append(Alliance(code=code, name=name))
                        for code, name in airlines_data:
                            airlines.append(Airline(code=code, name=name))
                    meta = JsMetadata(alliances=alliances, airlines=airlines)
                    flights = ResultList()
                    if not payload or len(payload) < 4 or payload[3] is None or not payload[3] or payload[3][0] is None:
                        flights.metadata = meta
                        return flights
                    for k in payload[3][0]:
                        flight = k[0]
                        if not k[1] or not k[1][0] or len(k[1][0]) < 2:
                            continue
                        price = k[1][0][1]
                        typ = flight[0]
                        airlines_names = flight[1]
                        sg_flights = []
                        for single_flight in flight[2]:
                            from_airport = Airport(code=single_flight[3], name=single_flight[4])
                            to_airport = Airport(code=single_flight[6], name=single_flight[5])
                            departure_time = _parse_time(single_flight[8])
                            departure_date = tuple(single_flight[20])
                            departure = SimpleDatetime(date=departure_date, time=departure_time)
                            arrival_time = _parse_time(single_flight[10])
                            arrival_date = tuple(single_flight[21])
                            arrival = SimpleDatetime(date=arrival_date, time=arrival_time)
                            plane_type = single_flight[17]
                            duration = single_flight[11]
                            sg_flights.append(SingleFlight(
                                from_airport=from_airport, to_airport=to_airport,
                                departure=departure, arrival=arrival,
                                duration=duration, plane_type=plane_type,
                            ))
                        carbon = CarbonEmission(typical_on_route=flight[22][0], emission=flight[22][1]) if flight[22] else None
                        flights.append(Flights(type=typ, price=price, airlines=airlines_names, flights=sg_flights, carbon=carbon))
                    flights.metadata = meta
                    return flights

                fast_flights.parser.parse_js = _safe_parse_js
                from fast_flights import FlightQuery, Passengers, create_query, get_flights

                provider_query = create_query(
                    flights=[
                        FlightQuery(date=query["outbound_date"], from_airport=query["origin"], to_airport=query["destination"]),
                        FlightQuery(date=query["return_date"], from_airport=query["destination"], to_airport=query["origin"]),
                    ], seat="economy", trip="round-trip", passengers=Passengers(adults=1), currency="USD",
                )
                results = get_flights(provider_query)
                rows = getattr(results, "flights", None)
                if rows is None and isinstance(results, list):
                    rows = results
                if not isinstance(rows, list):
                    raise ValueError("Invalid scraper results")
                normalized = []
                for item in rows[:100]:
                    price = getattr(item, "price", None)
                    if price is None:
                        continue
                    raw_airlines = getattr(item, "airlines", None)
                    if not isinstance(raw_airlines, list):
                        raw_airlines = ["Google Flights Carrier"]
                    if any(not isinstance(airline, str) for airline in raw_airlines):
                        continue
                    stops = getattr(item, "stops", None)
                    if not isinstance(stops, int) or isinstance(stops, bool):
                        legs = getattr(item, "flights", None)
                        stops = max(0, len(legs) - 1) if isinstance(legs, list) else 0
                    normalized.append({"price": price, "airlines": raw_airlines[:20], "layovers": stops})
                payload = {"rows": normalized}
                encoded = json.dumps(payload, allow_nan=False).encode("utf-8")
                if len(encoded) > MAX_OUTPUT_BYTES:
                    encoded = b'{"error":"response_too_large"}'
    except ImportError:
        encoded = b'{"error":"scraper_unavailable"}'
    except Exception as exc:
        try:
            from fast_flights.exceptions import FlightsNotFound
            if isinstance(exc, FlightsNotFound):
                encoded = b'{"rows":[]}'
            else:
                encoded = b'{"error":"scraper_failed"}'
        except Exception:
            encoded = b'{"error":"scraper_failed"}'
    try:
        connection.send_bytes(encoded)
    finally:
        connection.close()


def _run_subprocess_once(query, timeout_seconds):
    context = multiprocessing.get_context("spawn")
    receiver, sender = context.Pipe(duplex=False)
    process = context.Process(target=_scrape_worker, args=(sender, query), daemon=True)
    try:
        process.start()
        sender.close()
        if not receiver.poll(timeout_seconds):
            raise ProviderError("timeout", retryable=True)
        try:
            encoded = receiver.recv_bytes(maxlength=MAX_OUTPUT_BYTES)
            result = json.loads(encoded.decode("utf-8"))
        except (EOFError, OSError, ValueError, UnicodeError):
            raise ProviderError("scraper_failed") from None
        if not isinstance(result, dict):
            raise ProviderError("invalid_payload")
        if result.get("error"):
            category = result["error"]
            if category not in {"response_too_large", "scraper_unavailable", "scraper_failed"}:
                category = "scraper_failed"
            raise ProviderError(category)
        rows = result.get("rows")
        if not isinstance(rows, list) or len(rows) > 100:
            raise ProviderError("invalid_payload")
        return rows
    except (OSError, RuntimeError):
        raise ProviderError("scraper_failed") from None
    finally:
        receiver.close()
        sender.close()
        if process.is_alive():
            process.terminate()
            process.join(timeout=1)
            if process.is_alive():
                process.kill()
        if process.pid is not None:
            process.join(timeout=1)
            process.close()


def _run_subprocess(query, timeout_seconds):
    last_err = None
    for attempt in range(2):
        try:
            return _run_subprocess_once(query, timeout_seconds)
        except ProviderError as err:
            if err.category in {"timeout", "scraper_failed"} and attempt == 0:
                last_err = err
                time.sleep(0.5)
                continue
            raise
    if last_err:
        raise last_err


class FastFlightsScraper(FlightFetcher):
    name = "scraper"

    def __init__(self, currency="USD", *, timeout_seconds=20, deadline_seconds=None,
                 budget_callback=None, worker_runner=None):
        if currency != "USD":
            raise ValueError("Only USD pricing is supported")
        if not 0 < timeout_seconds <= 120:
            raise ValueError("Invalid scraper timeout")
        self.currency = currency
        self.timeout_seconds = min(timeout_seconds, deadline_seconds or timeout_seconds)
        self.budget_callback = budget_callback
        self.worker_runner = worker_runner or _run_subprocess

    def close(self) -> None:
        """Each search owns its subprocess; no persistent session needs closing."""

    @staticmethod
    def _parse_price(price_raw):
        # Avoid stripping arbitrary punctuation and turning locale-formatted
        # fares or negative amounts into apparently cheap positive USD values.
        if isinstance(price_raw, str):
            if not re.fullmatch(r"\$?(?:[1-9]\d{0,2}(?:,\d{3})+|\d+)(?:\.\d{1,2})?", price_raw):
                raise ValueError("Invalid USD price")
            price_raw = price_raw.removeprefix("$").replace(",", "")
        return positive_price(price_raw)

    def fetch_offers(self, origin, destination, destination_city, country_code,
                     outbound_date, return_date):
        query = dict(origin=origin, destination=destination, destination_city=destination_city,
                     country_code=country_code, outbound_date=outbound_date, return_date=return_date)
        try:
            FlightOffer(**query, price_usd=1)
        except ValueError:
            raise ProviderError("invalid_query") from None
        if self.budget_callback is not None:
            try:
                permitted = self.budget_callback()
            except Exception:
                raise ProviderError("budget_unavailable", retryable=True) from None
            if not permitted:
                raise ProviderError("budget_exhausted", retryable=True, retry_after_seconds=60)
        rows = self.worker_runner(query, self.timeout_seconds)
        if not isinstance(rows, list) or len(rows) > 100:
            raise ProviderError("invalid_payload")
        url = "https://www.google.com/travel/flights?" + urlencode({
            "q": f"Flights to {destination} from {origin} on {outbound_date} through {return_date}",
            "curr": "USD",
        })
        offers = []
        malformed = 0
        for row in rows:
            try:
                if not isinstance(row, dict):
                    raise ValueError("Invalid offer")
                offers.append(FlightOffer(
                    **query, price_usd=self._parse_price(row.get("price")),
                    airlines=row.get("airlines") or ["Google Flights Carrier"],
                    layovers=row.get("layovers", 0), source="Google Flights (Direct)",
                    booking_url=url, metadata_scope="outbound",
                ))
            except (ValueError, TypeError, KeyError, OverflowError):
                malformed += 1
        if malformed and not offers:
            raise ProviderError("invalid_offers")
        return offers
