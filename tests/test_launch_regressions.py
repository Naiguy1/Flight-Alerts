"""Report regressions exercise real SQLite without live providers or SMTP."""
from dataclasses import asdict, replace
from datetime import date, timedelta
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import yaml

from analyzer.evaluator import DealEvaluator
from db.crud import DatabaseManager
from fetcher.base import FlightOffer, ProviderError
from fetcher.fast_flights_scraper import FastFlightsScraper
from services.scanner import ScanService, build_fetchers, departure_offsets
from settings import RuntimeSettings, validate_config
from tests.test_services import DatabaseFixture, Provider, configuration, offer
from tools.sync_config import main as sync_config


class CalendarTests(unittest.TestCase):
    def test_partitions_reach_every_date_without_more_queries(self):
        window = validate_config({})["scanner"]["search_window"]
        seen = set()
        for phase in range(window["max_days_ahead"] - window["min_days_ahead"] + 1):
            offsets = departure_offsets(window, phase)
            self.assertEqual(len(offsets), len(window["sample_departure_offsets_days"]))
            self.assertEqual(len(set(offsets)), len(offsets))
            self.assertTrue(all(window["min_days_ahead"] <= day <= window["max_days_ahead"] for day in offsets))
            seen.update(offsets)
        self.assertEqual(seen, set(range(window["min_days_ahead"], window["max_days_ahead"] + 1)))
        self.assertEqual(departure_offsets(window, 0), window["sample_departure_offsets_days"])

    def test_one_day_window_and_boundary_samples(self):
        window = {"min_days_ahead": 30, "max_days_ahead": 30, "sample_departure_offsets_days": [30]}
        self.assertEqual(departure_offsets(window, 999), [30])
        window.update(max_days_ahead=37, sample_departure_offsets_days=[30, 37])
        self.assertEqual({d for phase in range(8) for d in departure_offsets(window, phase)}, set(range(30, 38)))


class LaunchScannerTests(DatabaseFixture):
    def scan(self, providers, *, runtime=None):
        return ScanService(self.db, runtime or self.runtime, self.cfg,
                           fetcher_factory=lambda cfg: providers).run()

    def test_scraper_is_first_and_paid_fallback_is_unused_on_results(self):
        paid, scraper = Provider(), Provider()
        scraper.name = "scraper"
        self.assertTrue(self.scan([paid, scraper]).success)
        self.assertEqual((scraper.calls, paid.calls), (2, 0))
        self.assertTrue(scraper.closed and paid.closed)

    def test_scraper_failure_uses_paid_fallback_and_reports_recovered_coverage(self):
        scraper, paid = Provider(failure=ProviderError("invalid_payload")), Provider()
        scraper.name = "scraper"
        result = self.scan([scraper, paid])
        self.assertTrue(result.success)
        self.assertEqual((scraper.calls, paid.calls, result.provider_failures), (2, 2, 2))

    def test_empty_primary_result_uses_fallback(self):
        scraper, paid = Provider(), Provider()
        scraper.name = "scraper"
        scraper.fetch_offers = lambda **query: []
        self.assertTrue(self.scan([scraper, paid]).success)
        self.assertEqual(paid.calls, 2)

    def test_primary_quota_does_not_starve_fallback(self):
        scraper, paid = Provider(failure=ProviderError("budget_exhausted")), Provider()
        scraper.name = "scraper"
        result = self.scan([scraper, paid])
        self.assertTrue(result.success)
        self.assertTrue(result.quota_exhausted)
        self.assertEqual((scraper.calls, paid.calls), (1, 2))

    def test_history_keeps_all_prices_and_evaluation_precedes_recording(self):
        provider = Provider()
        def fetch(**query):
            return [FlightOffer(**query, price_usd=price) for price in (200, 600, 1000)]
        provider.fetch_offers = fetch
        result = self.scan([provider])
        self.assertEqual(result.observations, 6)
        self.assertEqual(self.db.get_historical_route_stats("SLC", "LHR"), (600, 6))
        recent = self.db.dashboard_snapshot(["LHR"])["recent_deals"]
        self.assertEqual(len(recent), 2)
        self.assertTrue(all(row["price_usd"] == 200 for row in recent))

    def test_new_search_replaces_old_cheap_fare_in_dashboard(self):
        first = offer()
        expensive = FlightOffer(**{**asdict(first), "price_usd": 1000})
        self.db.record_offers([first, expensive])
        self.assertEqual(self.db.dashboard_snapshot(["LHR"])["recent_deals"][0]["price_usd"], 200)
        self.db.record_offers([expensive])
        self.assertEqual(self.db.dashboard_snapshot(["LHR"])["recent_deals"], [])

    def test_capacity_warning_precedes_provider_calls(self):
        cfg = configuration()
        cfg["scanner"]["provider_daily_budget"] = 1
        self.db.save_config(cfg, self.db.get_config_version())
        with self.assertLogs("services.scanner", level="WARNING") as logs:
            self.scan([Provider()])
        self.assertIn("scan_capacity_insufficient", logs.output[0])

    def test_monitored_domestic_airport_is_eligible_and_blocklist_still_wins(self):
        cfg = configuration()
        cfg["filters"]["excluded_country_codes"] = ["US"]
        cfg["monitored_destinations"] = [{"code": "HNL", "city": "Honolulu", "country_code": "US",
                                         "baseline_avg_usd": {"SLC": 1000}}]
        cfg = validate_config(cfg)
        self.assertIn("HNL", cfg["filters"]["allowed_domestic_airports"])
        self.db.save_config(cfg, self.db.get_config_version())
        self.assertEqual(self.scan([Provider()]).observations, 2)
        cfg["filters"]["blocked_destinations"] = ["HNL"]
        self.db.save_config(cfg, self.db.get_config_version())
        self.assertEqual(self.scan([Provider()]).total_windows, 0)

    def test_excluded_foreign_country_is_not_overridden_by_domestic_list(self):
        cfg = configuration()
        cfg["filters"].update(excluded_country_codes=["GB"], allowed_domestic_airports=["LHR"])
        self.db.save_config(cfg, self.db.get_config_version())
        self.assertEqual(self.scan([Provider()]).total_windows, 0)
        evaluator = DealEvaluator(self.db, excluded_country_codes=["GB"], allowed_domestic_airports=["LHR"])
        self.assertFalse(evaluator.is_eligible_route(offer()))

    def test_date_rotation_is_used_for_real_jobs(self):
        provider = Provider()
        captured = []
        def fetch(**query):
            captured.append(date.fromisoformat(query["outbound_date"]))
            return []
        provider.fetch_offers = fetch
        service = ScanService(self.db, self.runtime, self.cfg, fetcher_factory=lambda cfg: [provider])
        service.run(job={"id": 1, "lease_token": "unused"})
        first = set(captured)
        captured.clear()
        service.run(job={"id": 2, "lease_token": "unused"})
        self.assertEqual(set(captured), {day + timedelta(days=1) for day in first})

    def test_return_date_alone_defines_a_distinct_itinerary(self):
        self.verified()
        first = offer()
        second = FlightOffer(**{**asdict(first), "return_date":
                                (date.fromisoformat(first.return_date) + timedelta(days=1)).isoformat()})
        self.assertIsNotNone(self.db.reserve_alert(first, 1000, 80))
        self.assertIsNotNone(self.db.reserve_alert(second, 1000, 80))
        self.assertIsNone(self.db.reserve_alert(first, 1000, 80))


class ConfigAndOperationsTests(unittest.TestCase):
    def test_drift_warning_preserves_dashboard_settings_and_version(self):
        db = DatabaseManager("sqlite:///:memory:")
        try:
            defaults = configuration()
            db.get_config(defaults)
            self.assertFalse(db.warn_config_drift(defaults))
            changed = configuration(offsets=(40,))
            version = db.save_config(changed, db.get_config_version())
            with self.assertLogs("db.crud", level="WARNING") as logs:
                self.assertTrue(db.warn_config_drift(defaults))
            self.assertIn("python -m tools.sync_config", logs.output[0])
            self.assertEqual(db.get_config(defaults), changed)
            self.assertEqual(db.get_config_version(), version)
        finally:
            db.close()

    def test_sync_supports_fresh_database_updates_and_idempotent_repeats(self):
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.yaml"
            config_path.write_text(yaml.safe_dump(configuration()))
            runtime = RuntimeSettings(database_url="sqlite:///" + str(Path(directory) / "db.sqlite"),
                                      config_path=str(config_path))
            with patch("tools.sync_config.RuntimeSettings.from_env", return_value=runtime), patch("sys.stdout", new_callable=io.StringIO):
                sync_config()
                db = DatabaseManager(runtime.database_url)
                try:
                    self.assertEqual(db.get_config_version(), 1)
                    cfg = configuration(offsets=(40,))
                    config_path.write_text(yaml.safe_dump(cfg))
                    sync_config()
                    self.assertEqual(db.get_config({}), cfg)
                    self.assertEqual(db.get_config_version(), 2)
                    sync_config()
                    self.assertEqual(db.get_config_version(), 2)
                finally:
                    db.close()

    def test_every_compose_service_has_bounded_logs(self):
        root = Path(__file__).resolve().parents[1]
        compose = yaml.safe_load((root / "docker-compose.yml").read_text())
        for name, service in compose["services"].items():
            with self.subTest(service=name):
                self.assertEqual(service["logging"]["options"], {"max-size": "20m", "max-file": "5"})

    def test_real_scraper_can_be_closed_repeatedly(self):
        scraper = FastFlightsScraper(worker_runner=lambda *args, **kwargs: [])
        scraper.close()
        scraper.close()

    def test_real_provider_factory_orders_scraper_before_paid_providers(self):
        runtime = replace(RuntimeSettings(), enable_scraper=True, serpapi_api_key="synthetic", kiwi_api_key="synthetic")
        providers = build_fetchers(runtime, configuration(), None)
        try:
            self.assertEqual([provider.name for provider in providers], ["scraper", "serpapi", "kiwi"])
        finally:
            for provider in providers:
                provider.close()
