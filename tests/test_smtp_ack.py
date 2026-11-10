"""SMTP acceptance remains successful when connection teardown fails."""
import smtplib
import ssl
import unittest
from unittest.mock import MagicMock, patch

from notifier.emailer import DeliveryError, EmailNotifier


class SMTPAcknowledgmentTests(unittest.TestCase):
    def setUp(self):
        self.server = MagicMock()
        self.server.__enter__.return_value = self.server
        self.server.send_message.return_value = {}
        self.notifier = EmailNotifier("smtp.example.invalid", 587, "", "", "alerts@example.com")

    def send(self):
        with patch("notifier.emailer.smtplib.SMTP", return_value=self.server):
            return self.notifier.send_message("reader@example.com", "Deal", "A fare", "<p>A fare</p>")

    def test_accepted_data_survives_quit_failures(self):
        for error in (ssl.SSLError("teardown"), smtplib.SMTPResponseException(450, b"quit"),
                      OSError("teardown")):
            with self.subTest(error=type(error).__name__):
                self.server.__exit__.side_effect = error
                self.assertIsNone(self.send())
        self.assertEqual(self.server.send_message.call_count, 3)

    def test_data_rejections_remain_definite(self):
        for code, retryable in ((450, True), (550, False)):
            with self.subTest(code=code):
                self.server.send_message.side_effect = smtplib.SMTPDataError(code, b"rejected")
                with self.assertRaises(DeliveryError) as caught:
                    self.send()
                self.assertEqual(caught.exception.category, "smtp_rejected")
                self.assertEqual(caught.exception.retryable, retryable)

    def test_network_or_tls_failure_during_data_is_ambiguous(self):
        for error in (OSError("data"), ssl.SSLError("data")):
            with self.subTest(error=type(error).__name__):
                self.server.send_message.side_effect = error
                with self.assertRaises(DeliveryError) as caught:
                    self.send()
                self.assertEqual(caught.exception.category, "smtp_network_failed")
                self.assertTrue(caught.exception.retryable)

    def test_tls_failure_before_data_remains_definite(self):
        self.server.starttls.side_effect = ssl.SSLError("handshake")
        with self.assertRaises(DeliveryError) as caught:
            self.send()
        self.assertEqual(caught.exception.category, "smtp_tls_failed")
        self.assertFalse(caught.exception.retryable)
        self.server.send_message.assert_not_called()


if __name__ == "__main__":
    unittest.main()
