"""Provider tests use recorded-shaped payloads and never make network calls."""

from datetime import datetime, timezone
import json
import multiprocessing
import sys
import time
import unittest
from unittest.mock import Mock, patch

# Review runtime has no installable network packages. Production code imports
# requests normally; only this local test harness may use pip's bundled copy.
try:
    import requests
except ImportError:
    from pip._vendor import requests
    sys.modules["requests"] = requests

from fetcher.base import FlightOffer, ProviderError, sanitize_booking_url
from fetcher.fast_flights_scraper import FastFlightsScraper, _run_subprocess
from fetcher.google_flights import GoogleFlightsFetcher
from fetcher.secondary_aggregator import KiwiTequilaFetcher
from fetcher.transport import ProviderTransport, retry_after_seconds


QUERY = dict(origin="SLC", destination="LHR", destination_city="London", country_code="GB",
             outbound_date="2027-01-01", return_date="2027-01-08")
ENDPOINT = "https://serpapi.com/search"


class Response:
    def __init__(self, payload=None, *, status=200, headers=None, body=None):
        self.status_code = status
        self.headers = headers or {}
        self.body = body if body is not None else json.dumps(payload if payload is not None else {}).encode()
        self.closed = False

    def iter_content(self, chunk_size):
        for offset in range(0, len(self.body), chunk_size):
            yield self.body[offset:offset + chunk_size]

    def close(self):
        self.closed = True


class Clock:
    value = 0.0

    def __call__(self):
        return self.value

    def sleep(self, seconds):
        self.value += seconds


def hanging_worker(connection, query):
    time.sleep(5)


class DomainTests(unittest.TestCase):
    def test_bad_prices_rejected(self):
        for value in (-1, 0, float("nan"), float("inf"), True, "bad", 1_000_001):
            with self.subTest(value=value), self.assertRaises(ValueError):
                FlightOffer(**QUERY, price_usd=value)

    def test_baseline_currency_dates_and_country_validated(self):
        for changes in ({"provider_typical_price_usd": float("nan")}, {"currency": "EUR"},
                        {"country_code": "<script>"}, {"origin": "slc"},
                        {"return_date": "2026-12-31"}, {"outbound_date": "2027-02-30"},
                        {"layovers": True}, {"layovers": -1}, {"airlines": "bad"},
                        {"destination_city": "London\nInjected"}, {"observed_at": "2026-01-01T00:00:00"}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                FlightOffer(**{**QUERY, "price_usd": 250, **changes})

    def test_observation_time_is_explicit_utc(self):
        offer = FlightOffer(**QUERY, price_usd=250, observed_at="2026-10-26T18:00:00-06:00")
        self.assertEqual(offer.observed_at, "2026-10-27T00:00:00+00:00")
        self.assertEqual(offer.trip_duration_days, 7)

    def test_booking_urls_reject_script_external_userinfo_and_open_redirect_path(self):
        for raw in ("javascript:alert(1)", "http://www.kiwi.com", "https://evil.test/",
                    "https://google.com@evil.test/travel/flights", "https://www.google.com:444/travel/flights",
                    "https://www.google.com/url?q=https://evil.test", "https://www.google.com/travel/flights#x",
                    "https://www.google.com/travel/flights/%2e%2e/%2e%2e/url", "https://www.kiwi.com/\nfoo"):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                sanitize_booking_url(raw)
        self.assertEqual(sanitize_booking_url(""), "")
        self.assertEqual(sanitize_booking_url("https://www.kiwi.com/deep?x=1"), "https://www.kiwi.com/deep?x=1")


class TransportTests(unittest.TestCase):
    def make_transport(self, responses, **options):
        session = Mock()
        session.get.side_effect = responses
        clock = Clock()
        transport = ProviderTransport("serpapi", session=session, sleep=clock.sleep,
                                      clock=clock, random_fn=lambda: 0, **options)
        return transport, session, clock

    def test_transient_retry_consumes_budget_per_attempt_and_disables_redirects(self):
        first, second = Response(status=503), Response({"ok": True})
        budget = Mock(return_value=True)
        transport, session, clock = self.make_transport([first, second], budget_callback=budget)
        self.assertEqual(transport.get_json(ENDPOINT, params={"api_key": "synthetic"}), {"ok": True})
        self.assertEqual(budget.call_count, 2)
        self.assertEqual(clock.value, 0.5)
        self.assertTrue(first.closed and second.closed)
        self.assertFalse(session.get.call_args.kwargs["allow_redirects"])
        self.assertNotIn("verify", session.get.call_args.kwargs)

    def test_budget_denial_makes_no_http_request(self):
        transport, session, _ = self.make_transport([], budget_callback=lambda: False)
        with self.assertRaises(ProviderError) as caught:
            transport.get_json(ENDPOINT)
        self.assertEqual(caught.exception.category, "budget_exhausted")
        session.get.assert_not_called()

    def test_budget_denial_prevents_retry_request(self):
        budget = Mock(side_effect=[True, False])
        transport, session, _ = self.make_transport([Response(status=503)], budget_callback=budget)
        with self.assertRaises(ProviderError) as caught:
            transport.get_json(ENDPOINT)
        self.assertEqual(caught.exception.category, "budget_exhausted")
        self.assertEqual(session.get.call_count, 1)

    def test_auth_and_redirect_never_retry(self):
        for status, category in ((401, "authentication_failed"), (403, "permission_denied"), (302, "redirect_rejected")):
            transport, session, _ = self.make_transport([Response(status=status)])
            with self.subTest(status=status), self.assertRaises(ProviderError) as caught:
                transport.get_json(ENDPOINT)
            self.assertEqual(caught.exception.category, category)
            self.assertEqual(session.get.call_count, 1)

    def test_retry_after_honored_and_long_wait_returned_to_scheduler(self):
        transport, _, clock = self.make_transport([Response(status=429, headers={"Retry-After": "2"}), Response({})])
        transport.get_json(ENDPOINT)
        self.assertEqual(clock.value, 2)
        transport, session, _ = self.make_transport([Response(status=429, headers={"Retry-After": "120"})])
        with self.assertRaises(ProviderError) as caught:
            transport.get_json(ENDPOINT)
        self.assertEqual(caught.exception.retry_after_seconds, 120)
        self.assertEqual(session.get.call_count, 1)

    def test_retry_after_date_and_invalid_values(self):
        now = datetime(2026, 10, 27, tzinfo=timezone.utc)
        self.assertEqual(retry_after_seconds("Tue, 27 Oct 2026 00:00:03 GMT", now=now), 3)
        self.assertIsNone(retry_after_seconds("nan"))
        self.assertIsNone(retry_after_seconds("garbage"))

    def test_raw_request_errors_never_escape(self):
        error = requests.exceptions.ConnectionError("https://serpapi.com/search?api_key=SENSITIVE")
        transport, _, _ = self.make_transport([error], max_attempts=1)
        with self.assertRaises(ProviderError) as caught:
            transport.get_json(ENDPOINT)
        self.assertEqual(str(caught.exception), "network_error")
        self.assertNotIn("SENSITIVE", repr(caught.exception))

    def test_response_size_json_shape_and_parse_bounds(self):
        for response, category in ((Response(body=b"x" * 100), "response_too_large"),
                                   (Response(body=b"<html>bad</html>"), "invalid_json"),
                                   (Response([]), "invalid_payload")):
            transport, _, _ = self.make_transport([response], max_response_bytes=40)
            with self.subTest(category=category), self.assertRaises(ProviderError) as caught:
                transport.get_json(ENDPOINT)
            self.assertEqual(caught.exception.category, category)
            self.assertTrue(response.closed)

    def test_endpoint_allowlist_rejects_plaintext_external_and_embedded_secrets(self):
        for endpoint in ("http://serpapi.com/search", "https://evil.test/search", "https://serpapi.com/search?api_key=x"):
            transport, session, _ = self.make_transport([])
            with self.subTest(endpoint=endpoint), self.assertRaises(ProviderError):
                transport.get_json(endpoint)
            session.get.assert_not_called()

    def test_deadline_is_reset_on_each_invocation(self):
        transport, _, clock = self.make_transport([Response({}), Response({})], deadline_seconds=1)
        transport.get_json(ENDPOINT)
        clock.value = 100
        self.assertEqual(transport.get_json(ENDPOINT), {})

    def test_budget_wait_is_included_in_deadline_before_network(self):
        transport, session, clock = self.make_transport([], deadline_seconds=1)
        def paced_budget():
            clock.sleep(2)
            return True
        transport.budget_callback = paced_budget
        with self.assertRaises(ProviderError) as caught:
            transport.get_json(ENDPOINT)
        self.assertEqual(caught.exception.category, "deadline_exceeded")
        session.get.assert_not_called()

    def test_budget_callback_must_explicitly_authorize_request(self):
        for value in (1, "yes", None):
            transport, session, _ = self.make_transport([], budget_callback=lambda: value)
            with self.subTest(value=value), self.assertRaises(ProviderError) as caught:
                transport.get_json(ENDPOINT)
            self.assertEqual(caught.exception.category, "budget_exhausted")
            session.get.assert_not_called()

    def test_slow_body_trickle_is_checked_before_large_chunk_can_buffer(self):
        transport, session, clock = self.make_transport([], deadline_seconds=1)
        response = Response({"ok": True})
        def trickle(chunk_size):
            self.assertEqual(chunk_size, 1)
            for byte in response.body:
                clock.sleep(0.2)
                yield bytes([byte])
        response.iter_content = trickle
        session.get.side_effect = [response]
        with self.assertRaises(ProviderError) as caught:
            transport.get_json(ENDPOINT)
        self.assertEqual(caught.exception.category, "deadline_exceeded")
        self.assertTrue(response.closed)
        self.assertLess(clock.value, 2)

    def test_deeply_nested_json_is_rejected_with_safe_error(self):
        response = Response(body=b'{"x":' + b'[' * 2000 + b']' * 2000 + b'}')
        transport, _, _ = self.make_transport([response])
        with self.assertRaises(ProviderError) as caught:
            transport.get_json(ENDPOINT)
        self.assertEqual(caught.exception.category, "invalid_json")
        self.assertTrue(response.closed)
        transport, _, _ = self.make_transport([Response({"text": '[{\\"' * 100})])
        self.assertEqual(transport.get_json(ENDPOINT), {"text": '[{\\"' * 100})

    def test_certificate_errors_are_not_retried(self):
        transport, session, _ = self.make_transport([requests.exceptions.SSLError("raw secret")])
        with self.assertRaises(ProviderError) as caught:
            transport.get_json(ENDPOINT)
        self.assertEqual(caught.exception.category, "tls_error")
        self.assertEqual(session.get.call_count, 1)


class AdapterTests(unittest.TestCase):
    def fetcher(self, payload, kind=GoogleFlightsFetcher):
        return kind("synthetic", transport=Mock(get_json=Mock(return_value=payload)))

    def test_serpapi_retains_good_rows_and_labels_outbound_metadata(self):
        payload = {"best_flights": [None, {"price": -1}, {"price": 250, "flights": [{"airline": "Good Air"}]}],
                   "price_insights": {"typical_price_range": [500, 700]},
                   "search_metadata": {"google_flights_url": "javascript:alert(1)"}}
        offers = self.fetcher(payload).fetch_offers(**QUERY)
        self.assertEqual(len(offers), 1)
        self.assertEqual(offers[0].provider_typical_price_usd, 600)
        self.assertEqual(offers[0].metadata_scope, "outbound")
        self.assertTrue(offers[0].booking_url.startswith("https://www.google.com/travel/flights?"))

    def test_serpapi_null_insights_is_optional_not_fatal(self):
        offers = self.fetcher({"best_flights": [{"price": 250}], "price_insights": None}).fetch_offers(**QUERY)
        self.assertIsNone(offers[0].provider_typical_price_usd)

    def test_semantic_and_shape_errors_are_typed(self):
        for kind, payload in ((GoogleFlightsFetcher, {"error": "SENSITIVE"}),
                              (GoogleFlightsFetcher, {"best_flights": None}),
                              (GoogleFlightsFetcher, []),
                              (KiwiTequilaFetcher, {"error": "SENSITIVE"}),
                              (KiwiTequilaFetcher, {"data": None})):
            with self.subTest(kind=kind.__name__, payload=payload), self.assertRaises(ProviderError) as caught:
                self.fetcher(payload, kind).fetch_offers(**QUERY)
            self.assertNotIn("SENSITIVE", str(caught.exception))

    def test_all_invalid_rows_report_degraded_provider(self):
        for kind, payload in ((GoogleFlightsFetcher, {"best_flights": [{"price": "NaN"}]}),
                              (KiwiTequilaFetcher, {"data": [{"price": 0}]})):
            with self.subTest(kind=kind.__name__), self.assertRaises(ProviderError) as caught:
                self.fetcher(payload, kind).fetch_offers(**QUERY)
            self.assertEqual(caught.exception.category, "invalid_offers")

    def test_healthy_empty_results_stay_distinct(self):
        self.assertEqual(self.fetcher({"best_flights": []}).fetch_offers(**QUERY), [])
        self.assertEqual(self.fetcher({"data": []}, KiwiTequilaFetcher).fetch_offers(**QUERY), [])

    def test_kiwi_partial_results_and_null_optional_country(self):
        payload = {"data": [{"price": "bad"}, {"price": 250, "countryTo": None,
                            "route": [{}, {}], "deep_link": "https://www.kiwi.com/deep?x=1"}]}
        offers = self.fetcher(payload, KiwiTequilaFetcher).fetch_offers(**QUERY)
        self.assertEqual(len(offers), 1)
        self.assertEqual(offers[0].country_code, "GB")
        self.assertEqual(offers[0].metadata_scope, "roundtrip")

    def test_disabled_provider_does_not_silently_report_no_deals(self):
        transport = Mock()
        with self.assertRaises(ProviderError) as caught:
            GoogleFlightsFetcher("", transport=transport).fetch_offers(**QUERY)
        self.assertEqual(caught.exception.category, "provider_disabled")
        transport.get_json.assert_not_called()

    def test_invalid_query_does_not_cost_a_request(self):
        fetcher = self.fetcher({})
        with self.assertRaises(ProviderError) as caught:
            fetcher.fetch_offers(**{**QUERY, "origin": "../etc"})
        self.assertEqual(caught.exception.category, "invalid_query")
        fetcher.transport.get_json.assert_not_called()

    def test_response_cannot_substitute_another_route_or_currency(self):
        for parameters in ({"departure_id": "LAS"}, {"arrival_id": "CDG"},
                           {"currency": "EUR"}, {"outbound_date": "2027-01-02"}):
            payload = {"search_parameters": parameters, "best_flights": [{"price": 100}]}
            with self.subTest(parameters=parameters), self.assertRaises(ProviderError) as caught:
                self.fetcher(payload).fetch_offers(**QUERY)
            self.assertEqual(caught.exception.category, "mismatched_query")
        for kind, payload in (
            (GoogleFlightsFetcher, {"best_flights": [{"price": 100, "flights": [{"departure_airport": {"id": "LAS"}}]}]}),
            (GoogleFlightsFetcher, {"best_flights": [{"price": 100, "flights": [{"arrival_airport": {"id": "CDG"}}]}]}),
            (KiwiTequilaFetcher, {"data": [{"price": 100, "flyFrom": "LAS"}]}),
            (KiwiTequilaFetcher, {"data": [{"price": 100, "flyTo": "CDG"}]}),
        ):
            with self.subTest(kind=kind.__name__, payload=payload), self.assertRaises(ProviderError) as caught:
                self.fetcher(payload, kind).fetch_offers(**QUERY)
            self.assertEqual(caught.exception.category, "invalid_offers")

    def test_scraper_optional_and_safe_numeric_parser(self):
        scraper = FastFlightsScraper(worker_runner=lambda query, timeout: [
            {"price": "$1,234.50", "airlines": ["Air"], "layovers": 1}, {"price": "€1.234,50"}])
        offers = scraper.fetch_offers(**QUERY)
        self.assertEqual(len(offers), 1)
        self.assertEqual(offers[0].price_usd, 1234.5)
        for value in ("-100", "1.234,50", "NaN", float("nan")):
            with self.subTest(value=value), self.assertRaises(ValueError):
                scraper._parse_price(value)

    def test_scraper_timeout_terminates_actual_child(self):
        before = {child.pid for child in multiprocessing.active_children()}
        with patch("fetcher.fast_flights_scraper._scrape_worker", hanging_worker):
            started = time.monotonic()
            with self.assertRaises(ProviderError) as caught:
                _run_subprocess(QUERY, 0.1)
            self.assertEqual(caught.exception.category, "timeout")
            self.assertLess(time.monotonic() - started, 3)
        self.assertEqual({child.pid for child in multiprocessing.active_children()}, before)


if __name__ == "__main__":
    unittest.main()
