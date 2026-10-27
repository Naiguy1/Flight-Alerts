"""Configuration and fare-policy regressions; no external network dependencies."""
from __future__ import annotations

import copy
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from analyzer.evaluator import DealEvaluator
from fetcher.base import FlightOffer
from settings import RuntimeSettings, load_config, validate_config, validate_watched_flight


def base_config():
    return validate_config({
        "scanner": {"origins": ["SLC"], "request_delay_seconds": 0},
        "filters": {},
        "monitored_destinations": [
            {"code": "LHR", "city": "London", "country_code": "GB", "baseline_avg_usd": {"SLC": 1000}}
        ],
    })


def fare(price=250, *, origin="SLC", destination="LHR", **extras):
    outbound = datetime.now(timezone.utc).date() + timedelta(days=35)
    return FlightOffer(
        origin=origin, destination=destination, destination_city="London", country_code="GB",
        outbound_date=outbound.isoformat(), return_date=(outbound + timedelta(days=7)).isoformat(),
        price_usd=price, **extras,
    )


class ConfigurationTests(unittest.TestCase):
    def test_public_defaults_have_no_addresses_and_validate(self):
        cfg = load_config(str(Path(__file__).resolve().parents[1] / "config.yaml"))
        self.assertEqual(cfg["filters"]["recipient_emails"], [])
        self.assertEqual(cfg["scanner"]["currency"], "USD")
        self.assertEqual(len(cfg["monitored_destinations"]), 40)

    def test_normalization_never_mutates_callers_configuration(self):
        source = {"scanner": {"origins": ["slc", "SLC"]}, "filters": {"excluded_country_codes": []}}
        original = copy.deepcopy(source)
        cfg = validate_config(source)
        self.assertEqual(source, original)
        self.assertEqual(cfg["scanner"]["origins"], ["SLC"])
        self.assertEqual(cfg["filters"]["excluded_country_codes"], [])

    def test_nonfinite_boolean_and_out_of_range_values_are_rejected(self):
        for value in (float("nan"), float("inf"), float("-inf"), True, False, None, -1, 0, 100):
            cfg = base_config()
            cfg["filters"]["discount_threshold_percent"] = value
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_config(cfg)
        for value in (1.5, 0, 9):
            cfg = base_config()
            cfg["scanner"]["concurrency"] = value
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_config(cfg)

    def test_currency_dates_and_digest_policy_are_validated(self):
        cfg = base_config()
        cfg["scanner"]["currency"] = "EUR"
        with self.assertRaises(ValueError):
            validate_config(cfg)
        cfg = base_config()
        cfg["scanner"]["search_window"]["sample_departure_offsets_days"] = [366]
        with self.assertRaises(ValueError):
            validate_config(cfg)
        cfg = base_config()
        cfg["filters"].update(digest_enabled=True, freshness_hours=6, digest_interval_hours=6)
        with self.assertRaises(ValueError):
            validate_config(cfg)

    def test_recipient_validation_matches_delivery_policy(self):
        for value in (".a@example.com", "a..b@example.com", "a@example.com\r\nBcc:b@example.com", "a@localhost", "a@example.com,b@example.com"):
            cfg = base_config()
            cfg["filters"]["recipient_emails"] = [value]
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_config(cfg)

    def test_destination_and_baseline_constraints(self):
        for change in ({"code": "12A"}, {"country_code": "USA"}, {"city": "x\ny"}, {"baseline_avg_usd": {"SLC": float("nan")}}):
            cfg = base_config()
            cfg["monitored_destinations"][0].update(change)
            with self.subTest(change=change), self.assertRaises(ValueError):
                validate_config(cfg)
        cfg = base_config()
        cfg["monitored_destinations"].append(copy.deepcopy(cfg["monitored_destinations"][0]))
        with self.assertRaises(ValueError):
            validate_config(cfg)

    def test_flight_watch_has_exact_identifier_and_iso_date(self):
        self.assertEqual(validate_watched_flight("aa123", "2027-01-10"), ("AA123", "2027-01-10"))
        for code, day in (("AA1<script>", "2027-01-10"), ("AA123", "20270110"), ("AA123", "2027-02-30")):
            with self.subTest(code=code, day=day), self.assertRaises(ValueError):
                validate_watched_flight(code, day)


class RuntimeConfigurationTests(unittest.TestCase):
    def test_file_secrets_are_loaded_and_never_in_repr(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "token"
            path.write_text("SYNTHETIC_TOKEN_SECRET_" + "x" * 32 + "\n")
            with patch.dict(os.environ, {"TOKEN_SECRET_FILE": str(path)}, clear=True):
                runtime = RuntimeSettings.from_env()
            self.assertTrue(runtime.token_secret.startswith("SYNTHETIC_TOKEN_SECRET_"))
            self.assertNotIn("SYNTHETIC_TOKEN_SECRET_", repr(runtime))

    def test_conflicting_secret_sources_fail_closed(self):
        with patch.dict(os.environ, {"TOKEN_SECRET": "x" * 32, "TOKEN_SECRET_FILE": "/unused"}, clear=True):
            with self.assertRaises(ValueError):
                RuntimeSettings.from_env()

    def test_noncanonical_origins_and_unsupported_database_are_rejected(self):
        for origin in ("http://example.com", "https://user:secret@example.com", "https://example.com/path", "https://example.com?token=secret", "https://exam\nple.com", "https://example.com:99999"):
            with self.subTest(origin=origin), patch.dict(os.environ, {"PUBLIC_BASE_URL": origin}, clear=True):
                with self.assertRaises(ValueError):
                    RuntimeSettings.from_env()
        with patch.dict(os.environ, {"DATABASE_URL": "postgresql://localhost/app"}, clear=True):
            with self.assertRaises(ValueError):
                RuntimeSettings.from_env()


class FarePolicyTests(unittest.TestCase):
    def db(self, average=None, count=0):
        db = Mock()
        db.get_historical_route_stats.return_value = (average, count)
        return db

    def test_evaluation_has_no_delivery_or_persistence_side_effects(self):
        db = self.db()
        result = DealEvaluator(db, route_baselines={"LHR": {"SLC": 1000}}).evaluate_offers([fare()])
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].discount_percent, 75)
        self.assertEqual(db.method_calls, [unittest.mock.call.get_historical_route_stats("SLC", "LHR")])

    def test_minimum_samples_enforced_without_configured_baseline(self):
        self.assertEqual(DealEvaluator(self.db(1000, 1), min_historical_samples=15).evaluate_offers([fare()]), [])
        self.assertEqual(len(DealEvaluator(self.db(1000, 15), min_historical_samples=15).evaluate_offers([fare()])), 1)

    def test_historical_baseline_is_not_artificially_raised_to_configured_reference(self):
        evaluator = DealEvaluator(self.db(500, 20), route_baselines={"LHR": {"SLC": 1000}})
        self.assertEqual(evaluator.resolve_average_price(fare())[0], 500)

    def test_provider_reference_is_explicit_and_statistics_cached(self):
        db = self.db(1000, 20)
        evaluator = DealEvaluator(db)
        evaluator.evaluate_offers([fare(250), fare(300)])
        evaluator.evaluate_offers([fare(200)])
        db.get_historical_route_stats.assert_called_once()
        result = evaluator.resolve_average_price(fare(provider_typical_price_usd=800))
        self.assertEqual(result, (800, "Provider typical-price midpoint"))

    def test_configured_origins_empty_exclusions_and_blocklist_are_respected(self):
        offer = fare(origin="DEN")
        evaluator = DealEvaluator(self.db(), allowed_origins=["DEN"], excluded_country_codes=[])
        self.assertTrue(evaluator.is_eligible_route(offer))
        evaluator.blocked_terms = {"london"}
        self.assertFalse(evaluator.is_eligible_route(offer))

    def test_invalid_prices_rejected_at_domain_and_discount_boundaries(self):
        for value in (0, -1, float("nan"), float("inf")):
            with self.subTest(value=value), self.assertRaises(ValueError):
                fare(value)
            with self.subTest(value=value), self.assertRaises(ValueError):
                DealEvaluator.calculate_discount_percent(1000, value)
