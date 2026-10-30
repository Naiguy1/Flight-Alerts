"""Email rendering stays readable without remote assets or stylesheet support."""
import unittest

from notifier.emailer import render_deal, render_invitation, render_message


class EmailDesignTests(unittest.TestCase):
    def payload(self):
        price, average = 146.0, 326.53
        return {"offer": {"origin": "SLC", "destination": "TPA", "destination_city": "Tampa",
                          "price_usd": price, "currency": "USD", "outbound_date": "2027-01-27",
                          "return_date": "2027-02-03", "airlines": ["Frontier"], "layovers": 1,
                          "observed_at": "2026-10-28T23:28:58.268090+00:00",
                          "booking_url": "https://www.google.com/travel/flights"},
                "average_price_usd": average, "discount_percent": (average-price)/average*100,
                "baseline_source": "Historical route average (15 observations, 90 days)"}

    def test_deal_has_whole_prices_savings_and_honest_baseline_label(self):
        subject, plain, card = render_deal(self.payload())
        self.assertIn("$146", subject)
        self.assertIn("55.3%", subject)
        self.assertIn("$327 USD", plain)
        self.assertIn("$327</s>", card)
        self.assertIn("Save<br>55.3%", card)
        self.assertIn("Save $181 off baseline", card)
        self.assertIn("Historical route average", card)
        self.assertIn("outbound details", card)
        self.assertNotIn("$146.00", plain + card)
        self.assertNotIn("$326.53", plain + card)
        self.assertIn('bgcolor="#0b7353"', card)
        self.assertIn('role="presentation"', card)
        self.assertIn("View fare with provider", card)

    def test_invitation_keeps_expiry_consent_and_real_signed_links(self):
        confirm = "https://alerts.example.invalid/subscriptions/confirm?token=synthetic-confirm"
        unsubscribe = "https://alerts.example.invalid/subscriptions/unsubscribe?token=synthetic-disable"
        subject, plain, message = render_invitation(confirm, unsubscribe)
        self.assertEqual(subject, "Confirm your Flight-Alerts subscription")
        for part in (plain, message):
            self.assertIn(confirm, part)
            self.assertIn(unsubscribe, part)
            self.assertIn("48 hours", part)
        self.assertIn("Confirm only if", message)
        self.assertIn("No alerts will be sent until you confirm", message)
        self.assertIn('bgcolor="#102c44"', message)
        self.assertIn("Confirm subscription", message)
        self.assertNotIn("<script", message)
        self.assertNotIn("<img", message)

    def test_untrusted_fields_and_titles_are_escaped(self):
        payload = self.payload()
        payload["offer"]["destination_city"] = "<script>bad()</script>"
        payload["baseline_source"] = "<img src=x>"
        _, _, card = render_deal(payload)
        message = render_message("<script>bad()</script>", card)
        self.assertNotIn("<script>", message)
        self.assertNotIn("<img", message)
        self.assertIn("&lt;script&gt;", message)
        with self.assertRaises(ValueError):
            render_invitation("http://unsafe.example", "https://alerts.example.invalid/unsubscribe")
