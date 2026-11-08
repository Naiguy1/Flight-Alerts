"""Weekly recaps include cooldown-suppressed fares without live-price claims."""
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from db.crud import DatabaseManager
from notifier.delivery import DeliveryService
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
