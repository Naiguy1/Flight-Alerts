"""Real-SQLite regressions for destination spam, rolling limits, and recovery."""
import copy
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta, timezone
from pathlib import Path
from threading import Barrier
from unittest.mock import Mock, patch

from db.crud import DatabaseManager
from notifier.delivery import DeliveryService
from notifier.emailer import DeliveryError
from notifier.tokens import TokenSigner
from tests.test_notifications import NOW, SECRET, deal_payload


class DestinationLimitTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.now = NOW.replace(tzinfo=None)
        self.addCleanup(patch.stopall)
        patch("db.crud._now", side_effect=lambda: self.now).start()
        self.url = "sqlite:///" + str(Path(self.temporary.name) / "alerts.db")
        self.db = DatabaseManager(self.url)
        self.recipient = self.db.invite_recipient("one@example.invalid")["id"]
        self.db.confirm_recipient(self.recipient)
        self.notifier = Mock()
        self.config = {"filters": {"freshness_hours": 24, "weekly_newsletter_enabled": True}}
        self.sequence = 0

    def service(self, db=None):
        return DeliveryService(db or self.db, self.notifier, TokenSigner(SECRET),
            "https://alerts.example.com", lambda: self.config,
            clock=lambda: self.now.replace(tzinfo=timezone.utc))

    def payload(self, destination="STT", origin="SLC", price=288):
        payload = deal_payload(self.now.replace(tzinfo=timezone.utc))
        payload["offer"].update(destination=destination, destination_city=destination,
            origin=origin, price_usd=price,
            outbound_date=(self.now + timedelta(days=30 + self.sequence)).date().isoformat(),
            return_date=(self.now + timedelta(days=37 + self.sequence)).date().isoformat())
        payload["discount_percent"] = (1 - price / 1000) * 100
        return payload

    def enqueue(self, destination="STT", origin="SLC", recipient=None, payload=None, kind="deal"):
        self.sequence += 1
        self.assertTrue(self.db.enqueue_notification(recipient or self.recipient, kind,
            payload or self.payload(destination, origin), f"test:{self.sequence}"))

    def sql(self, statement, parameters=()):
        with self.db._write() as connection:
            return [dict(row) for row in connection.execute(statement, parameters)]

    def test_ten_travel_dates_and_price_improvements_send_one_email(self):
        for index in range(10):
            self.sequence += 1
            payload = self.payload(price=378-index*10)
            self.assertIsNotNone(self.db.reserve_alert(payload["offer"], 1000,
                payload["discount_percent"]))
        self.assertEqual(self.service().process_pending(), 1)
        self.assertEqual(self.notifier.send_message.call_count, 1)
        self.assertEqual(len(self.sql("SELECT * FROM deliveries WHERE status='canceled' "
            "AND last_error='destination_cooldown'")), 9)
        # A significant reduction and a different origin do not bypass the gap.
        self.enqueue(origin="LAX", payload=self.payload(origin="LAX", price=100))
        self.assertEqual(self.service().process_pending(), 0)

    def test_72h_spacing_and_two_per_rolling_week_survive_restart(self):
        start = self.now
        self.enqueue()
        self.assertEqual(self.service().process_pending(), 1)
        self.now = start + timedelta(hours=72, microseconds=-1)
        self.enqueue()
        self.assertEqual(self.service().process_pending(), 0)
        self.now = start + timedelta(hours=72)
        self.enqueue(origin="LAX", payload=self.payload(origin="LAX", price=250))
        self.db = DatabaseManager(self.url)
        self.assertEqual(self.service().process_pending(), 1)
        self.now = start + timedelta(days=6)
        self.enqueue()
        self.assertEqual(self.service().process_pending(), 0)
        self.now = start + timedelta(days=7, microseconds=-1)
        self.enqueue()
        self.assertEqual(self.service().process_pending(), 0)
        self.now = start + timedelta(days=7)
        self.enqueue(payload=self.payload(price=200))
        self.assertEqual(self.service().process_pending(), 1)
        self.assertEqual(self.notifier.send_message.call_count, 3)

    def test_destinations_and_recipients_have_independent_allowances(self):
        other = self.db.invite_recipient("two@example.invalid")["id"]
        self.db.confirm_recipient(other)
        self.enqueue()
        self.enqueue(destination="SJU")
        self.enqueue(recipient=other)
        self.assertEqual(self.service().process_pending(), 3)

    def test_same_or_more_expensive_fare_is_suppressed_even_after_72_hours(self):
        self.enqueue()
        self.assertEqual(self.service().process_pending(), 1)
        self.now += timedelta(hours=72)
        for price in (288, 300):
            self.enqueue(payload=self.payload(origin="LAS", price=price))
            self.assertEqual(self.service().process_pending(), 0)
        self.enqueue(payload=self.payload(origin="LAS", price=250))
        self.assertEqual(self.service().process_pending(), 1)

    def test_same_price_becomes_eligible_at_exact_seven_day_boundary(self):
        self.enqueue()
        self.assertEqual(self.service().process_pending(), 1)
        self.now += timedelta(days=7)
        self.enqueue()
        self.assertEqual(self.service().process_pending(), 1)

    def test_weekly_floor_uses_revalidated_sent_price(self):
        self.enqueue(payload=self.payload(price=400))
        service = self.service()
        def refresh(payload):
            payload["offer"]["price_usd"] = 250
            payload["discount_percent"] = 75
            return True
        service.revalidate = refresh
        self.assertEqual(service.process_pending(), 1)
        self.now += timedelta(hours=72)
        self.enqueue(payload=self.payload(price=300))
        self.assertEqual(self.service().process_pending(), 0)

    def test_slow_smtp_starts_cooldown_from_acknowledgment(self):
        start = self.now
        self.enqueue()
        def accepted(*args):
            self.now += timedelta(seconds=20)
        self.notifier.send_message.side_effect = accepted
        self.assertEqual(self.service().process_pending(), 1)
        self.now = start + timedelta(hours=72)
        self.enqueue()
        self.assertEqual(self.service().process_pending(), 0)
        self.now += timedelta(seconds=20)
        self.enqueue(payload=self.payload(price=250))
        self.assertEqual(self.service().process_pending(), 1)

    def test_explicit_digest_filters_blocked_destination_and_spends_one_slot(self):
        self.enqueue()
        self.assertEqual(self.service().process_pending(), 1)
        self.enqueue(kind="digest", payload={"deals": [self.payload(), self.payload("SJU")]})
        self.assertEqual(self.service().process_pending(), 1)
        self.assertIn("SJU", self.notifier.send_message.call_args.args[2])
        self.assertNotIn("STT", self.notifier.send_message.call_args.args[2])
        self.enqueue(destination="SJU")
        self.assertEqual(self.service().process_pending(), 0)

    def test_grouped_digest_counts_one_email_per_destination(self):
        self.config["filters"].update(digest_enabled=True, digest_interval_hours=1)
        self.enqueue()
        self.enqueue()
        self.enqueue(destination="SJU")
        self.sql("UPDATE deliveries SET created_at=?", ((self.now-timedelta(hours=2)).isoformat(),))
        self.assertEqual(self.service().process_pending(), 3)
        self.assertEqual(self.notifier.send_message.call_count, 1)
        self.assertEqual(len(self.sql("SELECT * FROM destination_alert_sends")), 2)

    def test_weekly_summary_is_exempt_and_preserves_cheaper_price(self):
        self.enqueue()
        self.assertEqual(self.service().process_pending(), 1)
        self.enqueue(kind="digest", payload={"weekly": True, "week_start": "2027-01-03",
            "week_end": "2027-01-10", "deals": [self.payload(price=100)]})
        self.assertEqual(self.service().process_pending(), 1)
        self.assertIn("$100", self.notifier.send_message.call_args.args[2])
        self.assertEqual(len(self.sql("SELECT * FROM destination_alert_sends")), 1)

    def test_two_workers_cannot_admit_same_destination_concurrently(self):
        self.enqueue()
        self.enqueue()
        rows = self.db.claim_deliveries(limit=2)
        other = DatabaseManager(self.url)
        barrier = Barrier(2)
        def reserve(pair):
            database, row = pair
            barrier.wait()
            return database.reserve_destination_alerts([row["id"]], row["lease_token"], {"STT"})
        with ThreadPoolExecutor(max_workers=2) as pool:
            allowed = list(pool.map(reserve, [(self.db, rows[0]), (other, rows[1])]))
        self.assertEqual(sum(bool(value) for value in allowed), 1)

    def test_lost_lease_cannot_spend_allowance(self):
        self.enqueue()
        row = self.db.claim_deliveries(limit=1)[0]
        self.now += timedelta(minutes=6)
        self.assertIsNone(self.db.reserve_destination_alerts([row["id"]], row["lease_token"], {"STT"}))
        self.assertEqual(self.sql("SELECT * FROM destination_alert_sends"), [])

    def test_ambiguous_network_acceptance_prevents_retry_spam(self):
        self.enqueue()
        self.notifier.send_message.side_effect = DeliveryError("smtp_network_failed", True)
        self.assertEqual(self.service().process_pending(), 0)
        self.now += timedelta(minutes=2)
        self.notifier.send_message.side_effect = None
        self.assertEqual(self.service().process_pending(), 0)
        self.assertEqual(self.notifier.send_message.call_count, 1)
        self.assertEqual(self.sql("SELECT last_error FROM deliveries")[0]["last_error"], "destination_cooldown")

    def test_crash_after_reservation_does_not_resend_on_lease_recovery(self):
        self.enqueue()
        row = self.db.claim_deliveries(limit=1)[0]
        self.assertEqual(self.db.reserve_destination_alerts([row["id"]], row["lease_token"], {"STT"}), {"STT"})
        self.now += timedelta(minutes=6)
        self.db = DatabaseManager(self.url)
        self.assertEqual(self.service().process_pending(), 0)
        self.notifier.send_message.assert_not_called()

    def test_cleanup_cannot_reset_weekly_allowance(self):
        start = self.now
        for day in (0, 3):
            self.now = start + timedelta(days=day)
            self.enqueue(payload=self.payload(price=288-day))
            self.assertEqual(self.service().process_pending(), 1)
        self.now = start + timedelta(days=6)
        self.db.cleanup(retention_days=1)
        self.assertEqual(len(self.sql("SELECT * FROM destination_alert_sends")), 2)
        self.enqueue()
        self.assertEqual(self.service().process_pending(), 0)

    def test_upgrade_backfills_actual_sent_alerts_and_live_digests(self):
        # Build the pre-upgrade state, including a historical weekly digest.
        self.enqueue()
        self.enqueue(kind="digest", payload={"deals": [self.payload(), copy.deepcopy(self.payload())]})
        self.enqueue(kind="digest", payload={"weekly": True, "deals": [self.payload("SJU")]})
        self.sql("UPDATE deliveries SET status='sent',sent_at=?", (self.now.isoformat()+"Z",))
        self.sql("DROP TABLE destination_alert_sends")
        self.sql("DELETE FROM schema_migrations WHERE version=5")
        self.db = DatabaseManager(self.url)
        slots = self.sql("SELECT * FROM destination_alert_sends")
        self.assertEqual(len(slots), 2)
        self.assertEqual({row["destination"] for row in slots}, {"STT"})
        self.enqueue()
        self.enqueue(destination="SJU")
        self.assertEqual(self.service().process_pending(), 1)
        self.assertIn("SJU", self.notifier.send_message.call_args.args[1])
