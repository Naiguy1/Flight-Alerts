"""Real SQLite regressions for scan completion, fair baselines and sent-price floors."""
from dataclasses import asdict, replace
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import io
import json
from unittest.mock import Mock, patch

from db.crud import DatabaseManager
from fetcher.base import FlightOffer, ProviderError
from notifier.delivery import DeliveryService
from notifier.emailer import render_destination_deal
from notifier.tokens import TokenSigner
from services.scanner import ScanService, evaluator_for
from services.revalidation import FareRevalidator
from services.budgets import ProviderBudgets
from tests.test_services import DatabaseFixture, Provider, configuration, offer
from tests.test_notifications import SECRET
from tools.audit_baselines import report


class ScanSummaryTests(DatabaseFixture):
    def configure(self, baseline=3000):
        cfg = configuration()
        cfg["monitored_destinations"][0]["baseline_avg_usd"]["SLC"] = baseline
        self.db.save_config(cfg, self.db.get_config_version())
        return cfg

    def rows(self, table):
        with self.db._read() as connection:
            return [dict(row) for row in connection.execute(f"SELECT * FROM {table} ORDER BY id")]

    def delivery(self, revalidate=None):
        notifier = Mock()
        service = DeliveryService(self.db, notifier, TokenSigner(SECRET), "https://alerts.example.com",
            lambda: self.db.get_config(self.cfg), revalidate=revalidate)
        return service, notifier

    def test_kix_first_expensive_result_cannot_consume_cheaper_results_slot(self):
        self.verified()
        self.configure()
        provider = Provider()
        prices = iter([1267, 978])
        def fetch(**query):
            self.assertEqual(self.rows("deliveries"), [])  # Still collecting the scan.
            return [FlightOffer(**query, price_usd=next(prices), source="Google Flights (SerpApi)")]
        provider.fetch_offers = fetch
        result = ScanService(self.db, self.runtime, self.cfg, fetcher_factory=lambda cfg: [provider]).run()
        self.assertTrue(result.success)
        self.assertEqual(result.alerts_reserved, 1)
        self.assertEqual(len(self.rows("alert_logs")), 1)
        payload = json.loads(self.rows("deliveries")[0]["payload"])
        self.assertEqual([item["offer"]["price_usd"] for item in payload["deals"]], [978, 1267])
        service, notifier = self.delivery()
        self.assertEqual(service.process_pending(), 1)
        self.assertIn("from $978", notifier.send_message.call_args.args[1])
        html = notifier.send_message.call_args.args[3]
        self.assertEqual(html.count('FLIGHT DEAL ALERT'), 1)
        self.assertIn("$1,267", html)
        self.assertEqual(len(self.rows("destination_alert_sends")), 1)

    def test_partial_quota_scan_publishes_only_completed_searches(self):
        self.verified()
        provider = Provider()
        def fetch(**query):
            provider.calls += 1
            if provider.calls == 2:
                raise ProviderError("budget_exhausted")
            return [FlightOffer(**query, price_usd=200)]
        provider.fetch_offers = fetch
        result = ScanService(self.db, self.runtime, self.cfg, fetcher_factory=lambda cfg: [provider]).run()
        self.assertFalse(result.success)
        self.assertEqual(result.alerts_reserved, 1)
        payload = json.loads(self.rows("deliveries")[0]["payload"])
        self.assertTrue(payload["scan_partial"])
        self.assertEqual(len(payload["deals"]), 1)

    def test_restart_preserves_candidates_snapshot_and_atomic_finalization(self):
        self.verified()
        cfg = self.configure()
        self.db.enqueue_scan(0)
        job = self.db.claim_scan()
        batch = self.db.begin_scan_batch(job)
        self.db.stage_scan_offers(batch["id"], [replace(offer(), price_usd=1267)], job=job)
        self.db.record_offer(replace(offer(), price_usd=17000))
        self.db.close()
        self.db = DatabaseManager("sqlite:///" + str(self.directory.name) + "/state.db")
        recovered = self.db.begin_scan_batch(job)
        self.assertEqual(recovered["history_max_id"], batch["history_max_id"])
        self.assertEqual(recovered["baseline_at"], batch["baseline_at"])
        self.db.stage_scan_offers(batch["id"], [replace(offer(), price_usd=978)], job=job)
        candidates = [FlightOffer(**item) for item in self.db.scan_batch_offers(batch["id"])]
        self.assertEqual(len(candidates), 1)
        deals = evaluator_for(self.db, cfg, batch=batch).evaluate_offers(candidates)
        self.assertEqual(self.db.finalize_scan_batch(batch["id"], deals, job=job), 1)
        self.assertEqual(self.db.finalize_scan_batch(batch["id"], deals, job=job), 0)
        provider = Provider()
        ScanService(self.db, self.runtime, self.cfg, fetcher_factory=lambda cfg: [provider]).run(job=job)
        self.assertEqual(provider.calls, 0)  # Crash after publish, before worker marks job finished.
        self.assertEqual(len(self.rows("deliveries")), 1)

    def test_stolen_scan_lease_cannot_stage_or_publish(self):
        self.verified()
        self.db.enqueue_scan(0)
        job = self.db.claim_scan()
        batch = self.db.begin_scan_batch(job)
        stale = {**job, "lease_token": "wrong"}
        with self.assertRaises(RuntimeError):
            self.db.stage_scan_offers(batch["id"], [offer()], job=stale)
        deals = evaluator_for(self.db, self.cfg, batch=batch).evaluate_offers([offer()])
        self.assertEqual(self.db.finalize_scan_batch(batch["id"], deals, job=stale), 0)
        self.assertEqual(self.rows("deliveries"), [])

    def test_concurrent_finalizers_publish_only_once(self):
        self.verified()
        self.db.enqueue_scan(0)
        job = self.db.claim_scan()
        batch = self.db.begin_scan_batch(job)
        deals = evaluator_for(self.db, self.cfg, batch=batch).evaluate_offers([offer()])
        other = DatabaseManager("sqlite:///" + str(self.db.path))
        self.addCleanup(other.close)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda db: db.finalize_scan_batch(batch["id"], deals, job=job), [self.db, other]))
        self.assertEqual(sum(results), 1)
        self.assertEqual(len(self.rows("deliveries")), 1)

    def test_optional_delay_applies_to_destination_summaries(self):
        self.verified()
        cfg = self.configure()
        cfg["filters"].update(digest_enabled=True, digest_interval_hours=1)
        self.db.save_config(cfg, self.db.get_config_version())
        ScanService(self.db, self.runtime, self.cfg, fetcher_factory=lambda cfg: [Provider()]).run()
        service, notifier = self.delivery()
        self.assertEqual(service.process_pending(), 0)
        notifier.send_message.assert_not_called()
        self.assertEqual(self.rows("deliveries")[0]["attempts"], 0)

    def test_schema_six_upgrade_preserves_recipients_and_recaps_and_cancels_old_pending_fares(self):
        recipient = self.verified()
        self.db.enqueue_notification(recipient["id"], "deal", {}, "old-deal")
        self.db.enqueue_notification(recipient["id"], "digest", {"weekly": True}, "weekly")
        version = self.db.get_config_version()
        with self.db._write() as connection:
            for table in ("scan_baselines", "scan_candidates", "scan_batches"):
                connection.execute(f"DROP TABLE {table}")
            for index in ("price_history_route_time", "deliveries_recipient_sent"):
                connection.execute(f"DROP INDEX {index}")
            connection.execute("DELETE FROM schema_migrations WHERE version=6")
        upgraded = DatabaseManager("sqlite:///" + str(self.db.path))
        self.addCleanup(upgraded.close)
        self.assertEqual(upgraded.get_config_version(), version)
        self.assertTrue(upgraded.get_recipient(recipient["id"])["verified"])
        self.assertEqual([r["status"] for r in self.rows("deliveries")], ["canceled", "pending"])

    def test_scan_cannot_cross_coverage_gate_using_its_own_observations(self):
        self.verified()
        cfg = self.configure(baseline=500)
        cfg["filters"].update(min_historical_samples_for_db_avg=2,
            historical_min_observation_days=2, historical_min_date_pairs=2)
        self.db.save_config(cfg, self.db.get_config_version())
        self.db.record_offer(replace(offer(), price_usd=1000,
            observed_at=(datetime.now(timezone.utc)-timedelta(days=1)).isoformat()))
        provider, prices = Provider(), iter([1000, 300])
        provider.fetch_offers = lambda **query: [FlightOffer(**query, price_usd=next(prices))]
        result = ScanService(self.db, self.runtime, self.cfg, fetcher_factory=lambda cfg: [provider]).run()
        self.assertEqual(result.alerts_reserved, 0)
        reference, count = self.db.get_historical_route_stats("SLC", "LHR", min_observation_days=2, min_date_pairs=2)
        self.assertIsNotNone(reference)  # Coverage is now sufficient for a FUTURE scan.
        self.assertGreaterEqual(count, 2)

    def test_revalidation_reselects_primary_and_persists_actual_sent_prices(self):
        self.verified()
        self.configure()
        provider, prices = Provider(), iter([700, 500])
        provider.fetch_offers = lambda **query: [FlightOffer(**query, price_usd=next(prices))]
        ScanService(self.db, self.runtime, self.cfg, fetcher_factory=lambda cfg: [provider]).run()
        def revalidate(payload):
            if payload["offer"]["price_usd"] == 500:
                return False
            payload["offer"]["price_usd"] = 600
            payload["discount_percent"] = 80
            return True
        service, notifier = self.delivery(revalidate)
        self.assertEqual(service.process_pending(), 1)
        self.assertIn("from $600", notifier.send_message.call_args.args[1])
        sent = json.loads(self.rows("deliveries")[0]["payload"])
        self.assertEqual([item["offer"]["price_usd"] for item in sent["deals"]], [600])

    def test_options_have_individual_booking_links_and_origins(self):
        first = evaluator_for(self.db, self.cfg).evaluate_offers([offer()])[0]
        payload = {"offer": asdict(first.offer), "average_price_usd": 1000, "discount_percent": 80}
        other = json.loads(json.dumps(payload))
        other["offer"].update(origin="LAS", price_usd=250,
            booking_url="https://www.google.com/travel/flights?q=other")
        other["discount_percent"] = 75
        subject, plain, html = render_destination_deal({"deals": [other, payload]})
        self.assertIn("from $200", subject)
        self.assertIn("LAS", plain)
        self.assertIn("SLC", plain)
        self.assertIn("?q=other", html)
        self.assertIn("$250", html)


class RobustBaselineTests(DatabaseFixture):
    def test_expensive_alternatives_and_repeat_queries_cannot_raise_reference(self):
        now = datetime.now(timezone.utc)
        first = offer()
        second = replace(first, return_date=(datetime.fromisoformat(first.return_date)+timedelta(days=1)).date().isoformat())
        for days, prices in ((2, (200, 400)), (1, (1000, 1200))):
            for item, price in zip((first, second), prices):
                item = replace(item, price_usd=price, observed_at=(now-timedelta(days=days)).isoformat())
                self.db.record_offers([item, replace(item, price_usd=17000)] * 10)
        self.assertEqual(self.db.get_historical_route_stats("SLC", "LHR", min_observation_days=2, min_date_pairs=2), (700, 4))
        self.assertEqual(self.db.get_historical_route_stats("SLC", "LHR"), (None, 4))
        output = io.StringIO()
        report(self.db.path, output)
        self.assertIn("historical_eligible", output.getvalue())
        self.assertIn("False", output.getvalue())

    def test_future_observations_and_past_departures_do_not_contribute(self):
        now = datetime.now(timezone.utc)
        self.db.record_offer(replace(offer(), price_usd=900,
            observed_at=(now+timedelta(days=1)).isoformat()))
        self.db.record_offer(replace(offer(), price_usd=10000,
            outbound_date=(now-timedelta(days=3)).date().isoformat(),
            return_date=(now-timedelta(days=1)).date().isoformat()))
        self.assertEqual(self.db.get_historical_route_stats("SLC", "LHR"), (None, 0))

    def test_saved_reference_survives_history_cleanup_and_revalidation(self):
        cfg = configuration()
        cfg["filters"].update(min_historical_samples_for_db_avg=2,
            historical_min_observation_days=2, historical_min_date_pairs=2)
        self.db.save_config(cfg, self.db.get_config_version())
        now, first = datetime.now(timezone.utc), offer()
        for days, prices in ((2, (200, 400)), (1, (1000, 1200))):
            for offset, price in enumerate(prices):
                self.db.record_offer(replace(first, price_usd=price,
                    outbound_date=(now+timedelta(days=40+offset)).date().isoformat(),
                    return_date=(now+timedelta(days=47+offset)).date().isoformat(),
                    observed_at=(now-timedelta(days=days)).isoformat()))
        self.verified()
        batch = self.db.begin_scan_batch()
        deals = evaluator_for(self.db, cfg, batch=batch).evaluate_offers([first])
        self.assertEqual(deals[0].average_price_usd, 700)
        self.db.finalize_scan_batch(batch["id"], deals)
        with self.db._write() as connection:
            connection.execute("DELETE FROM price_history")
        with self.db._read() as connection:
            payload = json.loads(connection.execute("SELECT payload FROM deliveries").fetchone()[0])["deals"][0]
        validator = FareRevalidator(self.db, self.runtime, self.cfg, ProviderBudgets(self.db))
        self.assertTrue(validator(payload))
        self.assertEqual(payload["average_price_usd"], 700)
