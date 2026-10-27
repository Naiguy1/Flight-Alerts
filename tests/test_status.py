"""Mocked status-adapter contract tests; no third-party API requests."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import unittest
from unittest.mock import Mock

from fetcher.base import ProviderError
from fetcher.status import FlightStatusFetcher, _historical_cutoff


class FlightStatusTests(unittest.TestCase):
    def setUp(self):
        self.day = datetime.now(timezone.utc).date().isoformat()
        self.row = {
            "flight_date": self.day,
            "flight_status": "active",
            "flight": {"iata": "UA123"},
            "departure": {"iata": "SLC", "gate": "A12", "delay": 10,
                          "scheduled": f"{self.day}T10:00:00+00:00"},
            "arrival": {"iata": "DEN", "gate": "B2", "delay": 5},
            "live": None,
        }
        self.transport = Mock()
        self.transport.get_json.return_value = {"data": [self.row]}
        self.fetcher = FlightStatusFetcher("synthetic-test-key", transport=self.transport)

    def test_normalizes_exact_flight(self):
        result = self.fetcher.fetch_status("UA123", self.day)
        self.assertEqual(result["flight_iata"], "UA123")
        self.assertEqual(result["status"], "active")
        self.assertEqual(result["delay_minutes"], 10)
        self.assertEqual(result["departure_gate"], "A12")
        self.assertEqual(result["timestamp_source"], "observation")
        args, kwargs = self.transport.get_json.call_args
        self.assertEqual(args[0], "https://api.aviationstack.com/v1/flights")
        self.assertEqual(kwargs["params"]["flight_date"], self.day)
        self.assertEqual(kwargs["params"]["flight_iata"], "UA123")

    def test_no_key_is_disabled_before_network(self):
        fetcher = FlightStatusFetcher(transport=self.transport)
        with self.assertRaises(ProviderError):
            fetcher.fetch_status("UA123", self.day)
        self.transport.get_json.assert_not_called()

    def test_future_watch_makes_no_request(self):
        tomorrow = (datetime.now(timezone.utc).date() + timedelta(days=1)).isoformat()
        with self.assertRaises(ProviderError):
            self.fetcher.fetch_status("UA123", tomorrow)
        self.transport.get_json.assert_not_called()

    def test_historical_limit_uses_calendar_months(self):
        from datetime import date

        self.assertEqual(_historical_cutoff(date(2026, 5, 31)), date(2026, 2, 28))
        old = (datetime.now(timezone.utc).date() - timedelta(days=100)).isoformat()
        with self.assertRaises(ProviderError):
            self.fetcher.fetch_status("UA123", old)
        self.transport.get_json.assert_not_called()

    def test_other_date_or_number_never_selected(self):
        other = deepcopy(self.row)
        other["flight_date"] = "2000-01-01"
        self.transport.get_json.return_value = {"data": [other]}
        self.assertEqual(self.fetcher.fetch_status("UA123", self.day), {})
        other["flight_date"] = self.day
        other["flight"]["iata"] = "UA999"
        self.assertEqual(self.fetcher.fetch_status("UA123", self.day), {})

    def test_ambiguous_multileg_number_rejected(self):
        other = deepcopy(self.row)
        other["departure"]["iata"] = "DEN"
        other["arrival"]["iata"] = "LAX"
        self.transport.get_json.return_value = {"data": [self.row, other]}
        with self.assertRaises(ProviderError):
            self.fetcher.fetch_status("UA123", self.day)

    def test_identical_duplicates_are_safe(self):
        self.transport.get_json.return_value = {"data": [self.row, deepcopy(self.row)]}
        self.assertEqual(self.fetcher.fetch_status("UA123", self.day)["status"], "active")

    def test_semantic_errors_sanitized(self):
        for error_type in ["invalid_access_key", "function_access_restricted", "usage_limit_reached", "unknown", {}]:
            with self.subTest(error_type=error_type):
                self.transport.get_json.return_value = {"error": {"type": error_type, "info": "synthetic-secret"}}
                with self.assertRaises(ProviderError) as raised:
                    self.fetcher.fetch_status("UA123", self.day)
                self.assertNotIn("synthetic-secret", str(raised.exception))

    def test_malformed_shape_and_truncation_rejected(self):
        for payload in [[], {}, {"data": None}, {"data": [], "pagination": {"total": 100}},
                        {"data": [], "pagination": []}]:
            with self.subTest(payload=payload):
                self.transport.get_json.return_value = payload
                with self.assertRaises(ProviderError):
                    self.fetcher.fetch_status("UA123", self.day)

    def test_invalid_status_delay_or_gate_rejected(self):
        for field, value in [("gate", "<script>"), ("delay", float("nan")), ("delay", -1), ("delay", True)]:
            with self.subTest(field=field, value=value):
                row = deepcopy(self.row)
                row["departure"][field] = value
                self.transport.get_json.return_value = {"data": [row]}
                with self.assertRaises(ProviderError):
                    self.fetcher.fetch_status("UA123", self.day)
        row = deepcopy(self.row)
        row["flight_status"] = "fabricated"
        self.transport.get_json.return_value = {"data": [row]}
        with self.assertRaises(ProviderError):
            self.fetcher.fetch_status("UA123", self.day)

    def test_invalid_exact_row_cannot_hide_another_leg(self):
        other = deepcopy(self.row)
        other["departure"]["scheduled"] = None
        self.transport.get_json.return_value = {"data": [self.row, other]}
        with self.assertRaises(ProviderError):
            self.fetcher.fetch_status("UA123", self.day)

    def test_unknown_delay_remains_unknown(self):
        self.row["departure"]["delay"] = None
        self.row["arrival"]["delay"] = None
        self.assertIsNone(self.fetcher.fetch_status("UA123", self.day)["delay_minutes"])

    def test_provider_live_timestamp_is_normalized(self):
        self.row["live"] = {"updated": f"{self.day}T10:00:00Z"}
        result = self.fetcher.fetch_status("UA123", self.day)
        self.assertEqual(result["updated_at"], f"{self.day}T10:00:00+00:00")
        self.assertEqual(result["timestamp_source"], "provider")


if __name__ == "__main__":
    unittest.main()
