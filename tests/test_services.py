"""Real SQLite application-service checks; every provider is an in-process fake."""
from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from db.crud import DatabaseManager
from fetcher.base import FlightOffer, ProviderError
from services.budgets import ProviderBudgets
from services.revalidation import FareRevalidator, RevalidationError
from services.scanner import ScanService
from services.status import StatusService
from settings import RuntimeSettings, validate_config


def configuration(offsets=(35, 65)):
    return validate_config({
        "scanner": {"origins": ["SLC"], "request_delay_seconds": 0, "concurrency": 1,
                    "search_window": {"sample_departure_offsets_days": list(offsets)}},
        "filters": {"recipient_emails": [], "excluded_country_codes": [], "min_historical_samples_for_db_avg": 15},
        "monitored_destinations": [{"code": "LHR", "city": "London", "country_code": "GB",
                                    "baseline_avg_usd": {"SLC": 1000}}],
    })


def offer(now=None):
    now = now or datetime.now(timezone.utc)
    return FlightOffer(origin="SLC", destination="LHR", destination_city="London", country_code="GB",
                       outbound_date=(now.date() + timedelta(days=35)).isoformat(),
                       return_date=(now.date() + timedelta(days=42)).isoformat(), price_usd=200,
                       source="Google Flights (SerpApi)", booking_url="https://www.google.com/travel/flights",
                       observed_at=now.isoformat())


class Provider:
    name = "serpapi"

    def __init__(self, *, failure=None, price=200):
        self.calls, self.closed = 0, False
        self.failure, self.price = failure, price

    def fetch_offers(self, **query):
        self.calls += 1
        if self.failure:
            raise self.failure
        return [FlightOffer(**query, price_usd=self.price, source="Google Flights (SerpApi)",
                            booking_url="https://www.google.com/travel/flights")]

    def close(self):
        self.closed = True


class DatabaseFixture(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.db = DatabaseManager("sqlite:///" + str(Path(self.directory.name) / "state.db"))
        self.cfg = configuration()
        self.db.get_config(self.cfg)
        self.runtime = RuntimeSettings()

    def tearDown(self):
        self.db.close()
        self.directory.cleanup()

    def verified(self):
        recipient = self.db.invite_recipient("recipient@example.invalid")
        self.db.confirm_recipient(recipient["id"])
        return recipient


class ScannerTests(DatabaseFixture):
    def service(self, provider=None, **kwargs):
        return ScanService(self.db, self.runtime, self.cfg,
                           fetcher_factory=lambda cfg: [] if provider is None else [provider], **kwargs)

    def test_complete_scan_records_history_and_reserves_verified_delivery(self):
        self.verified()
        provider = Provider()
        result = self.service(provider).run()
        self.assertTrue(result.success)
        self.assertEqual((result.total_windows, result.windows, result.completed_windows), (2, 2, 2))
        self.assertEqual(result.observations, 2)
        self.assertEqual(result.alerts_reserved, 1)
        self.assertEqual(len(self.db.dashboard_snapshot(["LHR"])["deliveries"]), 1)
        self.assertTrue(provider.closed)

    def test_dry_run_does_not_change_history_alerts_or_deliveries(self):
        self.verified()
        result = self.service(Provider()).run(dry_run=True)
        self.assertTrue(result.success)
        snapshot = self.db.dashboard_snapshot(["LHR"])
        self.assertEqual(snapshot["recent_deals"], [])
        self.assertEqual(snapshot["alerts"], [])
        self.assertEqual(snapshot["deliveries"], [])
        self.assertEqual(result.observations, 0)

    def test_disabled_providers_cannot_report_success(self):
        result = self.service().run()
        self.assertFalse(result.success)
        self.assertTrue(result.providers_disabled)
        self.assertEqual(result.error_category, "providers_not_configured")

    def test_healthy_no_results_is_complete_coverage(self):
        provider = Provider()
        provider.fetch_offers = Mock(return_value=[])
        result = self.service(provider).run()
        self.assertTrue(result.success)
        self.assertEqual(result.provider_successes, 2)
        self.assertEqual(result.observations, 0)

    def test_one_failed_provider_does_not_stop_healthy_data(self):
        bad, good = Provider(failure=ProviderError("invalid_payload")), Provider()
        bad.name = "kiwi"
        scanner = ScanService(self.db, self.runtime, self.cfg, fetcher_factory=lambda cfg: [bad, good])
        result = scanner.run()
        self.assertFalse(result.success)
        self.assertEqual(result.provider_failures, 2)
        self.assertEqual(result.observations, 2)
        self.assertEqual(good.calls, 2)
        self.assertTrue(bad.closed and good.closed)

    def test_quota_exhaustion_opens_circuit_without_hammering_every_window(self):
        provider = Provider(failure=ProviderError("budget_exhausted", retryable=True))
        result = self.service(provider).run()
        self.assertFalse(result.success)
        self.assertTrue(result.quota_exhausted)
        self.assertEqual(provider.calls, 1)
        self.assertLess(result.windows, result.total_windows)
        self.assertEqual(result.error_category, "provider_quota_exhausted")

    def test_authentication_failure_is_not_mislabeled_quota(self):
        provider = Provider(failure=ProviderError("authentication_failed"))
        result = self.service(provider).run()
        self.assertFalse(result.quota_exhausted)
        self.assertEqual(provider.calls, 1)
        self.assertFalse(result.success)

    def test_shutdown_stops_before_fetch_and_reports_canceled(self):
        stop = threading.Event()
        stop.set()
        provider = Provider()
        result = self.service(provider, stop_event=stop).run()
        self.assertEqual(provider.calls, 0)
        self.assertTrue(result.canceled)
        self.assertFalse(result.success)

    def test_database_failure_still_closes_provider(self):
        provider = Provider()
        with patch.object(self.db, "record_offers", side_effect=RuntimeError("private diagnostics")):
            with self.assertRaises(RuntimeError):
                self.service(provider).run()
        self.assertTrue(provider.closed)

    def test_live_pause_prevents_reservation(self):
        self.verified()
        cfg = configuration()
        cfg["filters"]["alerts_paused"] = True
        self.db.save_config(cfg, self.db.get_config_version())
        result = self.service(Provider()).run()
        self.assertTrue(result.success)
        self.assertEqual(result.alerts_reserved, 0)
        self.assertEqual(self.db.dashboard_snapshot(["LHR"])["deliveries"], [])

    def test_lowered_live_baseline_cannot_reserve_old_inflated_savings(self):
        self.verified()
        provider = Provider()
        original = provider.fetch_offers
        def changed_policy(**query):
            cfg = configuration()
            cfg["monitored_destinations"][0]["baseline_avg_usd"]["SLC"] = 300
            self.db.save_config(cfg, self.db.get_config_version())
            return original(**query)
        provider.fetch_offers = changed_policy
        result = self.service(provider).run()
        self.assertTrue(result.success)
        self.assertEqual(result.alerts_reserved, 0)
        self.assertEqual(self.db.dashboard_snapshot(["LHR"])["alerts"], [])

    def test_origin_destination_overlap_skips_impossible_self_routes(self):
        cfg = configuration(offsets=(35,))
        cfg["scanner"]["origins"] = ["SLC", "LAS"]
        cfg["monitored_destinations"] = [{"code": "SLC", "city": "Salt Lake City", "country_code": "US",
                                          "baseline_avg_usd": {"LAS": 500}}]
        cfg["filters"]["allowed_domestic_airports"] = ["SLC"]
        self.db.save_config(cfg, self.db.get_config_version())
        provider = Provider()
        result = self.service(provider).run()
        self.assertTrue(result.success)
        self.assertEqual(result.total_windows, 1)
        self.assertEqual(provider.calls, 1)

    def test_rotation_stride_remains_coprime_for_97_windows(self):
        cfg = configuration(offsets=(35,))
        cfg["monitored_destinations"] = []
        for number in range(97):
            code = "A" + chr(65 + number // 26) + chr(65 + number % 26)
            cfg["monitored_destinations"].append({"code": code, "city": code, "country_code": "GB", "baseline_avg_usd": {"SLC": 1000}})
        self.db.save_config(cfg, self.db.get_config_version())
        first_routes = []
        for job_id in (1, 2):
            provider = Provider(failure=ProviderError("budget_exhausted"))
            def fail(**query):
                first_routes.append(query["destination"])
                raise ProviderError("budget_exhausted")
            provider.fetch_offers = fail
            result = self.service(provider).run(job={"id": job_id, "lease_token": "not-used-before30s"})
            self.assertFalse(result.success)
        self.assertNotEqual(first_routes[0], first_routes[1])

    def test_another_claimant_cannot_run_same_scan(self):
        self.db.enqueue_scan(0)
        first = self.db.claim_scan()
        self.assertIsNotNone(first)
        self.assertIsNone(self.db.claim_scan())
        result = self.service(Provider()).run(job=first)
        self.assertTrue(result.success)
        self.assertTrue(self.db.finish_scan(first["id"], first["lease_token"], result.success))


class BudgetTests(unittest.TestCase):
    def test_shared_pacer_and_authoritative_db_account_each_attempt(self):
        clock = [0.0]
        db = Mock()
        db.provider_budget.return_value = True
        budget = ProviderBudgets(db, clock=lambda: clock[0], sleep=lambda seconds: clock.__setitem__(0, clock[0] + seconds))
        self.assertTrue(budget.consume("serpapi", 10, 1000))
        self.assertTrue(budget.consume("serpapi", 10, 1000))
        self.assertGreaterEqual(clock[0], 6)
        self.assertEqual(db.provider_budget.call_count, 2)

    def test_deadline_or_stop_prevents_budget_and_network_consumption(self):
        clock = [0.0]
        db = Mock()
        db.provider_budget.return_value = True
        budget = ProviderBudgets(db, clock=lambda: clock[0], sleep=lambda seconds: clock.__setitem__(0, clock[0] + seconds))
        budget.consume("serpapi", 1, 1000)
        self.assertFalse(budget.consume("serpapi", 1, 1000, deadline=10))
        self.assertEqual(db.provider_budget.call_count, 1)
        stop = threading.Event()
        stop.set()
        self.assertFalse(budget.consume("kiwi", 10, 1000, stop_event=stop))
        self.assertEqual(db.provider_budget.call_count, 1)


class RevalidationTests(DatabaseFixture):
    def revalidator(self, provider, **kwargs):
        return FareRevalidator(self.db, self.runtime, self.cfg, ProviderBudgets(self.db),
                               fetcher_factory=lambda config, names: [provider], **kwargs)

    def payload(self, age=0):
        return {"offer": asdict(offer(datetime.now(timezone.utc) - timedelta(seconds=age))),
                "average_price_usd": 1000, "discount_percent": 80, "baseline_source": "Configured"}

    def test_fresh_snapshot_needs_no_extra_paid_search(self):
        provider = Provider()
        self.assertTrue(self.revalidator(provider)(self.payload()))
        self.assertEqual(provider.calls, 0)

    def test_old_fare_rechecks_once_and_cache_is_reused_across_recipients(self):
        provider = Provider(price=150)
        first, second = self.payload(age=600), self.payload(age=600)
        validator = self.revalidator(provider)
        self.assertTrue(validator(first))
        self.assertTrue(validator(second))
        self.assertEqual(provider.calls, 1)
        self.assertTrue(provider.closed)
        self.assertEqual(first["offer"]["price_usd"], 150)
        self.assertEqual(first["discount_percent"], 85)

    def test_unavailable_or_more_expensive_fare_is_not_sent(self):
        provider = Provider(price=400)
        self.assertFalse(self.revalidator(provider)(self.payload(age=600)))
        self.assertEqual(provider.calls, 1)

    def test_slow_requery_uses_updated_time_for_fresh_observation(self):
        now = [datetime.now(timezone.utc)]
        payload = self.payload(age=600)
        provider = Provider()
        def slow(**query):
            now[0] += timedelta(seconds=90)
            return [FlightOffer(**query, price_usd=150, source="Google Flights (SerpApi)",
                                observed_at=now[0].isoformat())]
        provider.fetch_offers = slow
        validator = self.revalidator(provider, clock=lambda: now[0])
        self.assertTrue(validator(payload))
        self.assertEqual(payload["offer"]["price_usd"], 150)

    def test_stolen_revalidation_lease_cannot_write_cache(self):
        provider = Provider()
        with patch.object(self.db, "renew_lease", return_value=False), patch.object(self.db, "set_provider_cache", wraps=self.db.set_provider_cache) as write:
            with self.assertRaises(RevalidationError):
                self.revalidator(provider)(self.payload(age=600))
            write.assert_not_called()
        self.assertTrue(provider.closed)

    def test_unknown_source_cannot_trigger_arbitrary_provider_or_request(self):
        payload = self.payload(age=600)
        payload["offer"]["source"] = "https://untrusted.example/api"
        provider = Provider()
        self.assertFalse(self.revalidator(provider)(payload))
        self.assertEqual(provider.calls, 0)

    def test_provider_error_is_generic_and_releases_revalidation_lease(self):
        provider = Provider(failure=RuntimeError("SYNTHETIC_SECRET"))
        with self.assertRaises(RevalidationError) as captured:
            self.revalidator(provider)(self.payload(age=600))
        self.assertNotIn("SYNTHETIC_SECRET", str(captured.exception))
        self.assertTrue(provider.closed)
        # The previous failure cannot leave a claimed cache forever.
        self.assertTrue(self.revalidator(Provider())(self.payload(age=600)))

    def test_live_blocklist_or_threshold_is_applied_before_sending(self):
        payload = self.payload()
        config = configuration()
        config["filters"]["blocked_destinations"] = ["LHR"]
        self.db.save_config(config, self.db.get_config_version())
        self.assertFalse(self.revalidator(Provider())(payload))


class StatusServiceTests(DatabaseFixture):
    def test_status_initial_snapshot_then_transition_enqueues_once(self):
        self.verified()
        today = datetime.now(timezone.utc).date().isoformat()
        self.db.watch_flight("AA123", today)
        status = dict(flight_iata="AA123", flight_date=today, status="scheduled", delay_minutes=0,
                      departure_gate="A1", arrival_gate=None, observed_at=datetime.now(timezone.utc).isoformat())
        provider = Mock()
        provider.fetch_status.return_value = status
        service = StatusService(self.db, self.runtime, self.cfg, ProviderBudgets(self.db), fetcher=provider)
        self.assertEqual(service.poll(), 0)
        provider.fetch_status.return_value = dict(status, status="active", delay_minutes=25)
        self.assertEqual(service.poll(), 1)
        self.assertEqual(service.poll(), 0)
        deliveries = self.db.dashboard_snapshot([])["deliveries"]
        self.assertEqual(len(deliveries), 1)
        self.assertEqual(deliveries[0]["kind"], "status")

    def test_future_watches_do_not_consume_requests(self):
        tomorrow = (datetime.now(timezone.utc).date() + timedelta(days=1)).isoformat()
        self.db.watch_flight("AA123", tomorrow)
        provider = Mock()
        service = StatusService(self.db, self.runtime, self.cfg, ProviderBudgets(self.db), fetcher=provider)
        self.assertEqual(service.poll(), 0)
        provider.fetch_status.assert_not_called()

    def test_lost_status_lease_cannot_update_watched_state(self):
        self.db.watch_flight("AA123", datetime.now(timezone.utc).date().isoformat())
        provider = Mock()
        provider.fetch_status.return_value = {"status": "active"}
        service = StatusService(self.db, self.runtime, self.cfg, ProviderBudgets(self.db), fetcher=provider)
        with patch.object(self.db, "renew_lease", side_effect=[True, False]), patch.object(self.db, "update_flight_status", wraps=self.db.update_flight_status) as update:
            self.assertEqual(service.poll(), 0)
            update.assert_not_called()

    def test_provider_failure_releases_shared_poll_lease(self):
        self.db.watch_flight("AA123", datetime.now(timezone.utc).date().isoformat())
        provider = Mock()
        provider.fetch_status.side_effect = ProviderError("rate_limited")
        service = StatusService(self.db, self.runtime, self.cfg, ProviderBudgets(self.db), fetcher=provider)
        self.assertEqual(service.poll(), 0)
        lease = self.db.acquire_lease("flight-status-poll")
        self.assertIsNotNone(lease)
        self.db.release_lease("flight-status-poll", lease)


if __name__ == "__main__":
    unittest.main()
