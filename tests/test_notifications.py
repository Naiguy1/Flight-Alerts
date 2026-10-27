"""No-network notification security and delivery-state regression tests."""
from __future__ import annotations

import copy
import smtplib
import ssl
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

from notifier.delivery import DeliveryService, quiet_seconds
from notifier.emailer import DeliveryError, EmailNotifier, normalize_email, render_deal, render_status
from notifier.tokens import TokenError, TokenSigner
from db.crud import DatabaseManager

UTC = timezone.utc
NOW = datetime(2027, 1, 10, 15, 0, tzinfo=UTC)
SECRET = "a-secure-test-secret-with-at-least-32-bytes"


def deal_payload(now=NOW):
    return {
        "offer": {
            "origin": "SLC", "destination": "LHR", "destination_city": "London",
            "country_code": "GB", "outbound_date": "2027-02-10", "return_date": "2027-02-17",
            "price_usd": 250.0, "airlines": ["Example Airlines"], "layovers": 1,
            "source": "Example", "booking_url": "https://www.google.com/travel/flights?q=SLC",
            "observed_at": now.isoformat(), "currency": "USD", "metadata_scope": "outbound",
        },
        "average_price_usd": 1000.0, "discount_percent": 75.0,
        "baseline_source": "Configured baseline",
    }


class TokenTests(unittest.TestCase):
    def setUp(self):
        self.clock = [1000.0]
        self.signer = TokenSigner(SECRET, clock=lambda: self.clock[0])

    def test_roundtrip_and_purpose_binding(self):
        token = self.signer.issue(42, "confirm", 60)
        self.assertEqual(self.signer.verify(token, "confirm"), 42)
        with self.assertRaises(TokenError):
            self.signer.verify(token, "unsubscribe")

    def test_expiry_at_exact_boundary(self):
        token = self.signer.issue(42, "confirm", 60)
        self.clock[0] = 1060
        with self.assertRaises(TokenError):
            self.signer.verify(token, "confirm")

    def test_tamper_and_wrong_secret(self):
        token = self.signer.issue(42, "unsubscribe", 60)
        for value in (token + "x", token.replace("v1.", "v2."), token[:-5], "", ".", "x" * 2048, "☃"):
            with self.subTest(value=value), self.assertRaises(TokenError):
                self.signer.verify(value, "unsubscribe")
        other = TokenSigner("b" * 32, clock=lambda: 1000)
        with self.assertRaises(TokenError):
            other.verify(token, "unsubscribe")

    def test_validation(self):
        with self.assertRaises(ValueError):
            TokenSigner("weak")
        for recipient, purpose, ttl in ((True, "confirm", 60), (0, "confirm", 60), (1, "admin", 60), (1, "confirm", 0)):
            with self.subTest(recipient=recipient, purpose=purpose, ttl=ttl), self.assertRaises(ValueError):
                self.signer.issue(recipient, purpose, ttl)

    def test_future_issued_token_is_rejected(self):
        token = self.signer.issue(42, "confirm", 60)
        self.clock[0] = 900
        with self.assertRaises(TokenError):
            self.signer.verify(token, "confirm")


class RenderingAndSMTPTests(unittest.TestCase):
    def notifier(self, **kwargs):
        config = dict(smtp_host="smtp.example.invalid", smtp_port=587, smtp_username="smtp-user",
                      smtp_password="SYNTHETIC_SECRET", sender_email="alerts@example.invalid")
        config.update(kwargs)
        return EmailNotifier(**config)

    def test_mailbox_validation(self):
        self.assertEqual(normalize_email(" Person+tag@EXAMPLE.COM "), "person+tag@example.com")
        for invalid in ("one@example.com,two@example.com", "Name <one@example.com>",
                        "one@example.com\r\nBcc: two@example.com", "one@localhost", ".one@example.com", "one..two@example.com"):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                normalize_email(invalid)

    def test_verified_starttls_before_login(self):
        connection = Mock()
        server = connection.__enter__ = Mock(return_value=connection)
        connection.__exit__ = Mock(return_value=False)
        connection.send_message.return_value = {}
        with patch("notifier.emailer.smtplib.SMTP", return_value=connection):
            self.notifier().send_message("recipient@example.invalid", "Safe subject", "Plain", "<p>Safe</p>")
        context = connection.starttls.call_args.kwargs["context"]
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(context.check_hostname)
        calls = [call[0] for call in connection.method_calls]
        self.assertLess(calls.index("starttls"), calls.index("login"))
        self.assertEqual(connection.send_message.call_args.kwargs["to_addrs"], ["recipient@example.invalid"])
        self.assertEqual(str(connection.send_message.call_args.args[0]["To"]), "recipient@example.invalid")
        self.assertIsNotNone(server)

    def test_tls_failure_never_authenticates_and_has_safe_error(self):
        connection = Mock()
        connection.__enter__ = Mock(return_value=connection)
        connection.__exit__ = Mock(return_value=False)
        connection.starttls.side_effect = ssl.SSLError("SYNTHETIC_SECRET")
        with patch("notifier.emailer.smtplib.SMTP", return_value=connection), self.assertRaises(DeliveryError) as captured:
            self.notifier().send_message("recipient@example.invalid", "Subject", "Plain", "<p>Body</p>")
        self.assertEqual(captured.exception.category, "smtp_tls_failed")
        self.assertFalse(captured.exception.retryable)
        self.assertNotIn("SYNTHETIC_SECRET", str(captured.exception))
        connection.login.assert_not_called()
        connection.send_message.assert_not_called()

    def test_ssl_port_uses_verified_context(self):
        connection = Mock()
        connection.__enter__ = Mock(return_value=connection)
        connection.__exit__ = Mock(return_value=False)
        connection.send_message.return_value = {}
        with patch("notifier.emailer.smtplib.SMTP_SSL", return_value=connection) as smtp:
            self.notifier(smtp_port=465).send_message("recipient@example.invalid", "Subject", "Plain", "<p>Body</p>")
        self.assertTrue(smtp.call_args.kwargs["context"].check_hostname)
        connection.starttls.assert_not_called()

    def test_subject_header_injection_is_rejected(self):
        with patch("notifier.emailer.smtplib.SMTP") as smtp, self.assertRaises(ValueError):
            self.notifier().send_message("recipient@example.invalid", "Hi\nBcc: evil@example.com", "Plain", "<p>Body</p>")
        smtp.assert_not_called()

    def test_smtp_refusal_is_sanitized_and_classified(self):
        connection = Mock()
        connection.__enter__ = Mock(return_value=connection)
        connection.__exit__ = Mock(return_value=False)
        connection.send_message.side_effect = smtplib.SMTPRecipientsRefused({"private@example.com": (450, b"SYNTHETIC_SECRET")})
        with patch("notifier.emailer.smtplib.SMTP", return_value=connection), self.assertRaises(DeliveryError) as captured:
            self.notifier().send_message("recipient@example.invalid", "Subject", "Plain", "<p>Body</p>")
        self.assertTrue(captured.exception.retryable)
        self.assertNotIn("private@example.com", str(captured.exception))

    def test_html_is_escaped_and_links_are_allowlisted(self):
        payload = deal_payload()
        payload["offer"]["destination_city"] = '<img src=x onerror="alert(1)">'
        payload["offer"]["airlines"] = ['<script>alert(1)</script>']
        _, _, card = render_deal(payload)
        self.assertNotIn("<img", card)
        self.assertNotIn("<script", card)
        self.assertIn("&lt;script&gt;", card)
        for unsafe in ("javascript:alert(1)", "https://evil.example/travel", "https://www.google.com@evil.example/", "http://www.google.com/", "https://www.google.com/url?q=https://evil.example/",
                       "https://www.google.com/travel/flights/../../url"):
            payload["offer"]["booking_url"] = unsafe
            with self.subTest(unsafe=unsafe), self.assertRaises(ValueError):
                render_deal(payload)

    def test_status_fields_are_escaped(self):
        _, _, card = render_status({"flight_iata": "AA123", "flight_date": "2027-02-10", "current": {"status": "active", "departure_gate": "<img src=x>"}})
        self.assertIn("&lt;img src=x&gt;", card)
        self.assertNotIn("<img", card)

    def test_status_unknown_delay_is_not_reported_as_zero_or_provider_update(self):
        _, plain, card = render_status({"flight_iata": "AA123", "flight_date": "2027-02-10", "current": {
            "status": "scheduled", "delay_minutes": None, "departure_gate": None,
            "timestamp_source": "observation", "updated_at": "2027-02-10T00:00:00+00:00",
        }})
        self.assertIn("Reported delay (minutes): Not available", plain)
        self.assertIn("Last checked", card)
        self.assertNotIn("Provider update", card)

    def test_discount_claim_must_match_fare_and_baseline(self):
        payload = deal_payload()
        payload["discount_percent"] = 99
        with self.assertRaises(ValueError):
            render_deal(payload)

    def test_legacy_constructor_has_no_yaml_or_env_recipient_fallback(self):
        notifier = self.notifier(recipient_email="old@example.com", config_path="/must/not/be/read")
        self.assertFalse(hasattr(notifier, "_get_recipients"))


class FakeDatabase:
    def __init__(self):
        self.recipients = {
            1: {"id": 1, "email": "one@example.invalid", "verified": True, "enabled": True},
            2: {"id": 2, "email": "two@example.invalid", "verified": True, "enabled": True},
        }
        self.rows = []
        self.now = NOW
        self.claims = 0
        self.before_get = None

    def add(self, recipient_id=1, kind="deal", payload=None, created=None):
        row = dict(id=len(self.rows) + 1, recipient_id=recipient_id, kind=kind,
                   payload=copy.deepcopy(payload if payload is not None else deal_payload()),
                   created_at=(created or self.now).isoformat(), next_attempt_at=self.now,
                   attempts=0, state="pending", lease_token="")
        self.rows.append(row)
        return row

    def get_recipient(self, recipient_id):
        if self.before_get:
            self.before_get(recipient_id)
        recipient = self.recipients.get(recipient_id)
        return dict(recipient) if recipient else None

    def claim_deliveries(self, limit=1, lease_seconds=300, digest_mode=False):
        eligible = [r for r in self.rows if r["state"] == "pending" and r["next_attempt_at"] <= self.now]
        if not eligible:
            return []
        rows = [eligible[0]]
        if digest_mode and rows[0]["kind"] == "deal":
            rows = [r for r in eligible if r["kind"] == "deal" and r["recipient_id"] == rows[0]["recipient_id"]][:100]
        self.claims += 1
        for row in rows:
            row.update(state="sending", attempts=row["attempts"] + 1, lease_token=str(self.claims))
        return copy.deepcopy(rows)

    def _row(self, row_id, token):
        row = self.rows[row_id - 1]
        assert row["lease_token"] == token and row["state"] == "sending"
        return row

    def cancel_delivery(self, row_id, token, reason="recipient_disabled"):
        self._row(row_id, token).update(state="canceled", error=reason)

    def defer_delivery(self, row_id, token, retry_after_seconds):
        row = self._row(row_id, token)
        row.update(state="pending", attempts=row["attempts"] - 1, next_attempt_at=self.now + timedelta(seconds=retry_after_seconds))

    def update_delivery(self, row_id, token, success, error="", retry_after_seconds=60, max_attempts=5):
        row = self._row(row_id, token)
        state = "sent" if success else ("pending" if row["attempts"] < max_attempts else "failed")
        row.update(state=state, error=error, next_attempt_at=self.now + timedelta(seconds=retry_after_seconds))

    def update_delivery_group(self, ids, token, success, error="", retry_after_seconds=60, max_attempts=5, digest=False):
        for row_id in ids:
            self.update_delivery(row_id, token, success, error, retry_after_seconds, max_attempts)
        if digest and success:
            self.recipients[self.rows[ids[0] - 1]["recipient_id"]]["last_digest_at"] = self.now.isoformat()

    def renew_delivery_group(self, ids, token, lease_seconds=300):
        return all(self.rows[row_id - 1]["lease_token"] == token
                   and self.rows[row_id - 1]["state"] == "sending" for row_id in ids)


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        self.db = FakeDatabase()
        self.notifier = Mock()
        self.config = {"filters": {"freshness_hours": 48, "digest_interval_hours": 24}}
        self.service = DeliveryService(self.db, self.notifier, TokenSigner(SECRET), "https://alerts.example.com", lambda: self.config, clock=lambda: self.db.now)

    def test_deal_delivery_has_private_unsubscribe_link(self):
        row = self.db.add()
        self.assertEqual(self.service.process_pending(), 1)
        self.assertEqual(row["state"], "sent")
        call = self.notifier.send_message.call_args.args
        self.assertEqual(call[0], "one@example.invalid")
        link = call[4]
        self.assertEqual(urlsplit(link).hostname, "alerts.example.com")
        token = parse_qs(urlsplit(link).query)["token"][0]
        self.assertEqual(self.service.signer.verify(token, "unsubscribe"), 1)
        self.assertNotIn("one@example.invalid", link)

    def test_partial_recipient_failure_does_not_suppress_other_recipients(self):
        failed, sent = self.db.add(1), self.db.add(2)
        def send(recipient, *args):
            if recipient.startswith("one"):
                raise DeliveryError("smtp_network_failed", True)
        self.notifier.send_message.side_effect = send
        self.assertEqual(self.service.process_pending(), 1)
        self.assertEqual(failed["state"], "pending")
        self.assertEqual(sent["state"], "sent")
        self.db.now += timedelta(minutes=2)
        self.notifier.send_message.side_effect = None
        self.assertEqual(self.service.process_pending(), 1)
        self.assertEqual(failed["state"], "sent")
        self.assertEqual(self.notifier.send_message.call_count, 3)

    def test_unverified_or_disabled_recipients_do_not_receive_alerts(self):
        for field in ("verified", "enabled"):
            with self.subTest(field=field):
                self.setUp()
                self.db.recipients[1][field] = False
                row = self.db.add()
                self.assertEqual(self.service.process_pending(), 0)
                self.assertEqual(row["state"], "canceled")
                self.notifier.send_message.assert_not_called()

    def test_verification_can_reach_pending_recipient(self):
        self.db.recipients[1]["verified"] = False
        row = self.db.add(kind="verification", payload={})
        self.assertEqual(self.service.process_pending(), 1)
        self.assertEqual(row["state"], "sent")
        self.assertIn("/subscriptions/confirm?token=", self.notifier.send_message.call_args.args[2])

    def test_unsubscribe_rechecked_before_network(self):
        gets = [0]
        def disable_on_second_get(recipient):
            gets[0] += 1
            if gets[0] == 2:
                self.db.recipients[recipient]["enabled"] = False
        self.db.before_get = disable_on_second_get
        row = self.db.add()
        self.assertEqual(self.service.process_pending(), 0)
        self.assertEqual(row["state"], "canceled")
        self.notifier.send_message.assert_not_called()

    def test_pause_defers_without_spending_retry_attempts(self):
        self.config["filters"]["alerts_paused"] = True
        row = self.db.add()
        self.assertEqual(self.service.process_pending(), 0)
        self.assertEqual(row["state"], "pending")
        self.assertEqual(row["attempts"], 0)
        self.notifier.send_message.assert_not_called()

    def test_stale_fares_are_canceled(self):
        row = self.db.add(payload=deal_payload(NOW - timedelta(days=3)))
        self.assertEqual(self.service.process_pending(), 0)
        self.assertEqual(row["error"], "fare_expired")
        self.notifier.send_message.assert_not_called()

    def test_malformed_payload_is_canceled_without_crashing_worker(self):
        row = self.db.add(payload=[])
        self.assertEqual(self.service.process_pending(), 0)
        self.assertEqual(row["state"], "canceled")

    def test_terminal_failure_does_not_retry(self):
        self.notifier.send_message.side_effect = DeliveryError("recipient_refused", False)
        row = self.db.add()
        self.service.process_pending()
        self.assertEqual(row["state"], "failed")

    def test_digest_groups_messages_and_persists_cadence(self):
        self.config["filters"]["digest_enabled"] = True
        first = self.db.add(created=NOW - timedelta(hours=25))
        second = self.db.add(created=NOW - timedelta(hours=24))
        self.assertEqual(self.service.process_pending(), 2)
        self.assertEqual(first["state"], "sent")
        self.assertEqual(second["state"], "sent")
        self.assertEqual(self.notifier.send_message.call_count, 1)
        self.assertIn("2 fares", self.notifier.send_message.call_args.args[1])
        third = self.db.add(created=NOW - timedelta(hours=25))
        self.assertEqual(self.service.process_pending(), 0)
        self.assertEqual(third["state"], "pending")
        self.assertEqual(third["attempts"], 0)
        self.assertGreater(third["next_attempt_at"], NOW + timedelta(hours=23))

    def test_digest_waits_for_first_interval(self):
        self.config["filters"]["digest_enabled"] = True
        row = self.db.add()
        self.assertEqual(self.service.process_pending(), 0)
        self.assertEqual(row["attempts"], 0)
        self.notifier.send_message.assert_not_called()

    def test_revalidation_unavailable_cancels_fare(self):
        row = self.db.add()
        self.service.revalidate = Mock(return_value=False)
        self.assertEqual(self.service.process_pending(), 0)
        self.assertEqual(row["error"], "fare_unavailable")
        self.service.revalidate.assert_called_once()
        self.notifier.send_message.assert_not_called()

    def test_revalidation_exception_is_sanitized_and_retryable(self):
        row = self.db.add()
        self.service.revalidate = Mock(side_effect=RuntimeError("SYNTHETIC_SECRET URL"))
        with self.assertLogs("notifier.delivery", level="WARNING") as logs:
            self.assertEqual(self.service.process_pending(), 0)
        self.assertEqual(row["state"], "pending")
        self.assertEqual(row["error"], "fare_revalidation_failed")
        self.assertNotIn("SYNTHETIC_SECRET", " ".join(logs.output))
        self.notifier.send_message.assert_not_called()

    def test_digest_omits_unavailable_fare(self):
        self.config["filters"]["digest_enabled"] = True
        first = self.db.add(created=NOW - timedelta(hours=25))
        second = self.db.add(created=NOW - timedelta(hours=25))
        self.service.revalidate = Mock(side_effect=[False, True])
        self.assertEqual(self.service.process_pending(), 1)
        self.assertEqual(first["state"], "canceled")
        self.assertEqual(second["state"], "sent")
        self.assertEqual(self.notifier.send_message.call_count, 1)

    def test_send_budget_denial_defers_all_kinds_without_spending_attempt(self):
        for kind in ("deal", "verification", "status"):
            with self.subTest(kind=kind):
                self.setUp()
                if kind == "verification":
                    self.db.recipients[1]["verified"] = False
                payload = ({"flight_iata": "AA123", "flight_date": "2027-01-10", "current": {"status": "active"}}
                           if kind == "status" else ({} if kind == "verification" else deal_payload()))
                row = self.db.add(kind=kind, payload=payload)
                self.service.send_budget = Mock(return_value=False)
                self.assertEqual(self.service.process_pending(), 0)
                self.assertEqual(row["state"], "pending")
                self.assertEqual(row["attempts"], 0)
                self.service.send_budget.assert_called_once()
                self.notifier.send_message.assert_not_called()

    def test_consent_is_rechecked_after_send_budget_wait(self):
        row = self.db.add()
        def budget():
            self.db.recipients[1]["enabled"] = False
            return True
        self.service.send_budget = budget
        self.assertEqual(self.service.process_pending(), 0)
        self.assertEqual(row["state"], "canceled")
        self.notifier.send_message.assert_not_called()

    def test_lost_lease_prevents_smtp(self):
        self.db.add()
        self.db.renew_delivery_group = Mock(return_value=False)
        self.assertEqual(self.service.process_pending(limit=1), 0)
        self.notifier.send_message.assert_not_called()

    def test_expired_status_is_not_sent(self):
        row = self.db.add(kind="status", created=NOW - timedelta(hours=7), payload={
            "flight_iata": "AA123", "flight_date": "2027-01-10", "current": {"status": "active"},
        })
        self.assertEqual(self.service.process_pending(), 0)
        self.assertEqual(row["error"], "status_expired")
        self.notifier.send_message.assert_not_called()

    def test_explicit_digest_discards_stale_payloads(self):
        row = self.db.add(kind="digest", payload={"deals": [deal_payload(NOW - timedelta(days=3))]})
        self.assertEqual(self.service.process_pending(), 0)
        self.assertEqual(row["error"], "fare_expired")

    def test_custom_https_port_is_retained_in_signed_links(self):
        self.service = DeliveryService(self.db, self.notifier, TokenSigner(SECRET), "https://alerts.example.com:8443", lambda: self.config, clock=lambda: self.db.now)
        self.db.add()
        self.assertEqual(self.service.process_pending(), 1)
        self.assertEqual(urlsplit(self.notifier.send_message.call_args.args[4]).port, 8443)

    def test_arbitrary_public_base_url_is_rejected(self):
        for base in ("http://alerts.example.com", "https://evil@example.com", "https://alerts.example.com/path"):
            with self.subTest(base=base), self.assertRaises(ValueError):
                DeliveryService(self.db, self.notifier, TokenSigner(SECRET), base, lambda: {})


class QuietHoursTests(unittest.TestCase):
    def test_overnight_window(self):
        quiet = {"start": 22, "end": 7, "timezone": "America/Denver"}
        at_night = datetime(2027, 1, 10, 6, 0, tzinfo=UTC)  # 23:00 previous local day.
        self.assertGreater(quiet_seconds(at_night, quiet), 7 * 3600)
        self.assertEqual(quiet_seconds(NOW, quiet), 0)

    def test_daytime_window_and_disabled_equal_hours(self):
        self.assertGreater(quiet_seconds(NOW, {"start": 14, "end": 16, "timezone": "UTC"}), 0)
        self.assertEqual(quiet_seconds(NOW, {"start": 7, "end": 7, "timezone": "UTC"}), 0)

    def test_spring_dst_uses_utc_timeline(self):
        at_night = datetime(2027, 3, 14, 8, 30, tzinfo=UTC)  # 01:30 MST, before gap.
        delay = quiet_seconds(at_night, {"start": 22, "end": 3, "timezone": "America/Denver"})
        self.assertLess(delay, 3600)
        self.assertGreater(delay, 1700)


class SQLiteDeliveryIntegrationTests(unittest.TestCase):
    """Exercise real leases, persistence and subscriptions with SMTP mocked."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.now = datetime(2026, 10, 27, 15, 0, tzinfo=UTC)
        self.time_patch = patch("db.crud._now", side_effect=lambda: self.now.replace(tzinfo=None))
        self.time_patch.start()
        self.db = DatabaseManager("sqlite:///" + str(Path(self.directory.name) / "alerts.db"))
        self.config = {"filters": {"freshness_hours": 24, "digest_interval_hours": 1}}
        self.db.get_config(self.config)
        self.recipient = self.db.invite_recipient("one@example.invalid")
        self.db.confirm_recipient(self.recipient["id"])
        self.notifier = Mock()
        self.service = DeliveryService(
            self.db, self.notifier, TokenSigner(SECRET), "https://alerts.example.com",
            lambda: self.config, clock=lambda: self.now,
        )

    def tearDown(self):
        self.db.close()
        self.time_patch.stop()
        self.directory.cleanup()

    def payload(self, observed=None):
        payload = deal_payload(observed or self.now)
        payload["offer"].update(
            outbound_date=(self.now + timedelta(days=35)).date().isoformat(),
            return_date=(self.now + timedelta(days=42)).date().isoformat(),
        )
        return payload

    def enqueue(self, key, recipient=None, payload=None):
        self.assertTrue(self.db.enqueue_notification(
            recipient or self.recipient["id"], "deal", payload or self.payload(), key,
        ))

    def rows(self):
        with self.db._read() as connection:
            return [dict(row) for row in connection.execute("SELECT * FROM deliveries ORDER BY id")]

    def test_partial_failure_retries_only_the_failed_recipient_across_restarts(self):
        second = self.db.invite_recipient("two@example.invalid")
        self.db.confirm_recipient(second["id"])
        self.enqueue("first")
        self.enqueue("second", second["id"])
        def send(recipient, *args):
            if recipient.startswith("one"):
                raise DeliveryError("smtp_network_failed")
        self.notifier.send_message.side_effect = send
        self.assertEqual(self.service.process_pending(limit=2), 1)
        self.assertEqual([row["status"] for row in self.rows()], ["retry", "sent"])
        # A newly constructed service/DB handle must preserve the retry state.
        self.now += timedelta(seconds=61)
        self.notifier.send_message.side_effect = None
        restarted = DatabaseManager("sqlite:///" + str(Path(self.directory.name) / "alerts.db"))
        try:
            delivery = DeliveryService(restarted, self.notifier, TokenSigner(SECRET), "https://alerts.example.com",
                                       lambda: self.config, clock=lambda: self.now)
            self.assertEqual(delivery.process_pending(limit=2), 1)
        finally:
            restarted.close()
        self.assertEqual([row["status"] for row in self.rows()], ["sent", "sent"])
        self.assertEqual([row["attempts"] for row in self.rows()], [2, 1])
        self.assertEqual(self.notifier.send_message.call_count, 3)

    def test_expired_claim_after_slow_revalidation_never_sends_and_is_recoverable(self):
        self.enqueue("first")
        def slow_check(payload):
            self.now += timedelta(seconds=301)
            return True
        self.service.revalidate = slow_check
        with self.assertLogs("notifier.delivery", level="WARNING") as logs:
            self.assertEqual(self.service.process_pending(limit=1), 0)
        self.assertIn("notification_lease_lost", " ".join(logs.output))
        self.notifier.send_message.assert_not_called()
        self.service.revalidate = None
        self.assertEqual(self.service.process_pending(limit=1), 1)
        self.assertEqual(self.rows()[0]["attempts"], 2)
        self.assertEqual(self.rows()[0]["status"], "sent")

    def test_unsubscribe_during_budget_wait_cancels_claim_before_smtp(self):
        self.enqueue("first")
        def budget():
            self.db.disable_recipient(self.recipient["id"])
            return True
        self.service.send_budget = budget
        self.assertEqual(self.service.process_pending(limit=1), 0)
        self.assertEqual(self.rows()[0]["status"], "canceled")
        self.notifier.send_message.assert_not_called()

    def test_digest_renews_entire_group_between_slow_revalidations(self):
        self.config["filters"]["digest_enabled"] = True
        queued_at = self.now - timedelta(hours=2)
        with patch("db.crud._now", return_value=queued_at.replace(tzinfo=None)):
            self.enqueue("first", payload=self.payload(queued_at))
            self.enqueue("second", payload=self.payload(queued_at))
        def slow_check(payload):
            self.now += timedelta(seconds=200)
            return True
        self.service.revalidate = slow_check
        self.assertEqual(self.service.process_pending(limit=1), 2)
        self.assertEqual([row["status"] for row in self.rows()], ["sent", "sent"])
        self.assertEqual(self.notifier.send_message.call_count, 1)
        self.assertIsNotNone(self.db.get_recipient(self.recipient["id"])["last_digest_at"])

    def test_retries_are_bounded_and_do_not_resurrect_failed_delivery(self):
        self.enqueue("first")
        self.notifier.send_message.side_effect = DeliveryError("smtp_network_failed")
        for _ in range(5):
            self.assertEqual(self.service.process_pending(limit=1), 0)
            self.now += timedelta(hours=1)
        self.assertEqual(self.rows()[0]["status"], "failed")
        self.assertEqual(self.rows()[0]["attempts"], 5)
        self.service.process_pending(limit=1)
        self.assertEqual(self.notifier.send_message.call_count, 5)

    def test_quiet_hours_defer_without_consuming_delivery_attempts(self):
        self.enqueue("first")
        self.config["filters"]["quiet_hours"] = {"start": 14, "end": 16, "timezone": "UTC"}
        self.assertEqual(self.service.process_pending(limit=1), 0)
        self.assertEqual(self.rows()[0]["status"], "pending")
        self.assertEqual(self.rows()[0]["attempts"], 0)
        self.notifier.send_message.assert_not_called()


if __name__ == "__main__":
    unittest.main()
