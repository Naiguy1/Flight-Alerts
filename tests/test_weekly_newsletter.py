"""Weekly recaps include cooldown-suppressed fares without live-price claims."""
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from db.crud import DatabaseManager
from fetcher.base import FlightOffer
from notifier.delivery import DeliveryService
from notifier.emailer import render_deal
from notifier.tokens import TokenSigner


class WeeklyNewsletterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = DatabaseManager('sqlite:///' + str(Path(self.tmp.name) / 'test.db'))
        self.recipient = self.db.invite_recipient('weekly@example.com')
        self.db.confirm_recipient(self.recipient['id'])
        self.end = datetime(2027, 1, 11, tzinfo=timezone.utc)
        self.now = self.end.replace(hour=15)
        self.time_patch = patch("db.crud._now", side_effect=lambda: self.now.replace(tzinfo=None))
        self.time_patch.start()
        self.addCleanup(self.time_patch.stop)
        self.notifier = Mock()
        self.revalidate = Mock(side_effect=AssertionError('Historical recap must not revalidate'))
        self.delivery = DeliveryService(self.db, self.notifier, TokenSigner('s' * 40),
            'https://example.com', lambda: {'filters': {'weekly_newsletter_enabled': True}},
            clock=lambda: self.now.replace(tzinfo=timezone.utc), revalidate=self.revalidate)
        with self.db._write() as con:
            con.execute('DELETE FROM deliveries')

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def offer(self, price, observed=None):
        return dict(origin='SLC', destination='STT', destination_city='St. Thomas', country_code='VI',
            outbound_date='2027-04-06', return_date='2027-04-15', price_usd=price,
            observed_at=(observed or self.end - timedelta(days=2)).isoformat())

    def record(self, price, observed=None):
        return self.db.reserve_alert(self.offer(price, observed), 650, (650-price)/650*100,
                                     baseline_source='Configured reference')

    def test_lower_cooldown_fare_wins_and_restart_does_not_duplicate(self):
        self.assertIsNotNone(self.record(313))
        self.assertIsNone(self.record(288))
        self.assertEqual(self.db.enqueue_weekly_newsletter(self.end, self.now), 1)
        self.assertEqual(self.db.enqueue_weekly_newsletter(self.end, self.now), 0)
        with self.db._write() as con:
            con.execute("DELETE FROM deliveries WHERE kind='deal'")
        self.assertEqual(self.delivery.process_pending(), 1)
        args = self.notifier.send_message.call_args.args
        self.assertIn('weekly best deals', args[1])
        self.assertIn('$288', args[2])
        self.assertNotIn('$313', args[2])
        self.assertIn('Historical prices', args[2])
        self.assertIn('Unsubscribe:', args[2])
        self.revalidate.assert_not_called()

    def test_empty_week_and_boundary(self):
        self.record(288, self.end)
        self.record(250, self.end - timedelta(days=8))
        self.assertEqual(self.db.enqueue_weekly_newsletter(self.end, self.now), 0)

    def test_historical_low_is_preserved_after_departure(self):
        cheapest = self.offer(250)
        cheapest['outbound_date'] = '2027-01-10'
        cheapest['return_date'] = '2027-01-17'
        self.db.reserve_alert(cheapest, 650, (650-250)/650*100)
        self.record(288)
        self.db.enqueue_weekly_newsletter(self.end, self.now)
        with self.db._write() as con:
            con.execute("DELETE FROM deliveries WHERE kind='deal'")
        self.assertEqual(self.delivery.process_pending(), 1)
        self.assertIn('$250', self.notifier.send_message.call_args.args[2])
        self.assertNotIn('$288', self.notifier.send_message.call_args.args[2])

    def test_weekly_low_does_not_require_an_instant_alert_or_comparison(self):
        self.record(313)
        self.db.record_weekly_offers([self.offer(200)])
        self.db.enqueue_weekly_newsletter(self.end, self.now)
        with self.db._write() as con:
            con.execute("DELETE FROM deliveries WHERE kind='deal'")
        self.assertEqual(self.delivery.process_pending(), 1)
        args = self.notifier.send_message.call_args.args
        self.assertIn('$200', args[2])
        self.assertIn('$200', args[3])
        self.assertNotIn('$313', args[2])
        self.assertNotIn('Save ', args[2])
        self.assertNotIn('Baseline:', args[3])
        self.revalidate.assert_not_called()

    def test_instant_renderer_still_requires_a_valid_comparison(self):
        payload = {'offer': self.offer(200)}
        with self.assertRaises((KeyError, ValueError)):
            render_deal(payload)
        self.assertIn('$200', render_deal(payload, historical=True)[1])

    def test_lost_scan_lease_cannot_change_the_weekly_low(self):
        self.db.record_weekly_offers([self.offer(200)], scan_job_id=123, scan_lease_token='stale')
        self.assertEqual(self.db.enqueue_weekly_newsletter(self.end, self.now), 0)

    def test_scanner_keeps_lowest_fare_below_alert_discount_threshold(self):
        from services.scanner import ScanService
        from settings import RuntimeSettings, validate_config
        cfg = validate_config({
            'scanner': {'origins': ['SLC'], 'request_delay_seconds': 0, 'concurrency': 1,
                        'search_window': {'sample_departure_offsets_days': [35, 65]}},
            'filters': {'excluded_country_codes': [], 'recipient_emails': []},
            'monitored_destinations': [{'code': 'STT', 'city': 'St. Thomas', 'country_code': 'VI',
                                        'baseline_avg_usd': {'SLC': 650}}],
        })
        self.db.get_config(cfg)
        prices = iter([600, 550])
        observed = (self.end - timedelta(days=2)).isoformat()
        provider = Mock(name='provider')
        provider.name = 'serpapi'
        provider.fetch_offers.side_effect = lambda **query: [FlightOffer(
            **query, price_usd=next(prices), observed_at=observed)]
        scanner = ScanService(self.db, RuntimeSettings(), cfg, fetcher_factory=lambda _: [provider])
        result = scanner.run()
        self.assertEqual(result.observations, 2)
        self.assertEqual(result.alerts_reserved, 0)
        self.assertEqual(self.db.enqueue_weekly_newsletter(self.end, self.now), 1)
        self.assertEqual(self.delivery.process_pending(), 1)
        self.assertIn('$550', self.notifier.send_message.call_args.args[2])
        self.assertNotIn('$600', self.notifier.send_message.call_args.args[2])

    def test_scanner_saves_weekly_observations_while_alerts_are_paused(self):
        from services.scanner import ScanService
        from settings import RuntimeSettings, validate_config
        cfg = validate_config({
            'scanner': {'origins': ['SLC'], 'request_delay_seconds': 0,
                        'search_window': {'sample_departure_offsets_days': [35]}},
            'filters': {'alerts_paused': True, 'recipient_emails': [], 'excluded_country_codes': []},
            'monitored_destinations': [{'code': 'STT', 'city': 'St. Thomas', 'country_code': 'VI',
                                        'baseline_avg_usd': {'SLC': 650}}],
        })
        self.db.get_config(cfg)
        observed = (self.end - timedelta(days=2)).isoformat()
        provider = Mock()
        provider.name = 'serpapi'
        provider.fetch_offers.side_effect = lambda **query: [FlightOffer(
            **query, price_usd=200, observed_at=observed)]
        scanner = ScanService(self.db, RuntimeSettings(), cfg, fetcher_factory=lambda _: [provider])
        result = scanner.run()
        self.assertEqual(result.observations, 1)
        self.assertEqual(result.alerts_reserved, 0)
        self.assertEqual(self.db.enqueue_weekly_newsletter(self.end, self.now), 1)
        self.assertEqual(self.delivery.process_pending(), 1)
        self.assertIn('$200', self.notifier.send_message.call_args.args[2])

    def test_disabled_recipient_and_expired_edition(self):
        self.record(288)
        self.db.enqueue_weekly_newsletter(self.end, self.now)
        self.now += timedelta(days=7)
        self.assertEqual(self.delivery.process_pending(), 0)
        self.notifier.send_message.assert_not_called()

    def test_unsubscribe_before_send(self):
        self.record(288)
        self.db.enqueue_weekly_newsletter(self.end, self.now)
        self.db.disable_recipient(self.recipient['id'])
        self.delivery.process_pending()
        self.notifier.send_message.assert_not_called()

    def test_monday_schedule_pause_and_opt_out(self):
        from services.worker import Worker
        worker = Worker.__new__(Worker)
        worker.db = self.db
        self.record(288)
        config = {"filters": {"weekly_newsletter_enabled": True, "alerts_paused": False}}
        self.assertEqual(worker.schedule_newsletter(config, now=self.end + timedelta(hours=14)), 0)
        config["filters"]["alerts_paused"] = True
        self.assertEqual(worker.schedule_newsletter(config, now=self.now), 0)
        config["filters"]["alerts_paused"] = False
        self.assertEqual(worker.schedule_newsletter(config, now=self.now), 1)
        self.delivery.config_getter = lambda: {"filters": {"weekly_newsletter_enabled": False}}
        with self.db._write() as con:
            con.execute("DELETE FROM deliveries WHERE kind='deal'")
        self.assertEqual(self.delivery.process_pending(), 0)
        self.notifier.send_message.assert_not_called()

    def test_quiet_hours_and_pause_defer_weekly_delivery(self):
        self.record(288)
        self.db.enqueue_weekly_newsletter(self.end, self.now)
        with self.db._write() as con:
            con.execute("DELETE FROM deliveries WHERE kind='deal'")
        self.delivery.config_getter = lambda: {"filters": {
            "weekly_newsletter_enabled": True,
            "quiet_hours": {"start": 14, "end": 16, "timezone": "UTC"}}}
        self.assertEqual(self.delivery.process_pending(), 0)
        self.notifier.send_message.assert_not_called()
        self.now += timedelta(hours=2)
        self.delivery.config_getter = lambda: {"filters": {
            "weekly_newsletter_enabled": True, "alerts_paused": True}}
        self.assertEqual(self.delivery.process_pending(), 0)
        self.notifier.send_message.assert_not_called()
