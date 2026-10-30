"""Whole-dollar, safe display formatting and dashboard template regressions."""
from decimal import Decimal
import importlib.util
from pathlib import Path
import unittest

from web.presentation import money, observed_time, percentage, travel_date


class DisplayFormattingTests(unittest.TestCase):
    def test_prices_round_half_up_and_group_thousands(self):
        for raw, expected in ((146.00, "$146"), (326.53, "$327"),
                              (1338.2666666667, "$1,338"), (326.5, "$327"),
                              (Decimal("999.50"), "$1,000"), (0, "$0")):
            self.assertEqual(money(raw), expected)
        original = Decimal("326.53")
        money(original)
        self.assertEqual(original, Decimal("326.53"))

    def test_missing_or_unsafe_values_do_not_break_rendering(self):
        for value in (None, "", "<script>alert(1)</script>", float("nan"), float("inf")):
            self.assertEqual(money(value), "—")
            self.assertEqual(percentage(value), "—")
        self.assertEqual(travel_date("not a date"), "—")
        self.assertEqual(observed_time(None), "—")

    def test_discounts_and_dates_are_readable_and_utc_is_explicit(self):
        self.assertEqual(percentage(55.34567), "55.3%")
        self.assertEqual(percentage(Decimal("55.35")), "55.4%")
        self.assertEqual(travel_date("2026-12-02"), "Dec 2, 2026")
        self.assertEqual(observed_time("2026-10-28T17:28:58.268090-06:00"),
                         "Oct 28, 2026 · 23:28 UTC")


@unittest.skipUnless(importlib.util.find_spec("jinja2"), "Jinja2 is required")
class DashboardRenderingTests(unittest.TestCase):
    def render(self, **snapshot):
        from jinja2 import Environment, FileSystemLoader, select_autoescape
        environment = Environment(loader=FileSystemLoader(Path(__file__).resolve().parents[1] / "web/templates"),
                                  autoescape=select_autoescape(["html"], default=True))
        environment.filters.update(money=money, percentage=percentage, observed_time=observed_time,
                                   travel_date=travel_date)
        cfg = {"scanner": {"scan_interval_hours": 6}, "filters": {
            "discount_threshold_percent": 50, "deduplication_window_hours": 48,
            "price_improvement_usd": 35, "price_improvement_percent": 10},
            "monitored_destinations": [{"code": "TPA", "city": "Tampa", "country_code": "US",
                                        "baseline_avg_usd": {"SLC": 326.53}}]}
        return environment.get_template("dashboard.html").render(
            cfg=cfg, snapshot=snapshot, recipients=[], watches=[], notice="", csrf_token="synthetic-csrf",
            config_version=7)

    def test_cards_round_every_price_and_preserve_form_security_and_links(self):
        output = self.render(recent_deals=[{
            "origin": "SLC", "destination": "TPA", "destination_city": "Tampa <script>alert(1)</script>",
            "outbound_date": "2026-12-02", "return_date": "2026-12-09", "price_usd": 146,
            "average_price_usd": 326.53, "discount_percent": 55.345, "samples": 15,
            "observed_at": "2026-10-28T23:28:58.268090+00:00", "airlines": ["Frontier"],
            "booking_url": "https://www.google.com/travel/flights"}],
            route_stats=[{"origin": "LAS", "destination": "AKL", "samples": 15,
                          "avg_price": 1338.26666666667}],
            alerts=[{"origin": "SLC", "destination": "TPA", "alerted_price_usd": 146.49,
                     "discount_percent": 55.345, "baseline_source": "Historical"}])
        self.assertIn('>55.3% off</span>', output)
        self.assertIn('aria-label="55.3% below the 90-day route average"', output)
        self.assertIn('class="fare-price">$146</strong>', output)
        self.assertIn('90-day average $327">$327</s>', output)
        self.assertIn("$1,338", output)
        self.assertIn("SLC $327", output)
        self.assertNotIn("1338.266", output)
        self.assertNotIn("$146.00", output)
        self.assertNotIn("<script>", output)
        self.assertIn("&lt;script&gt;", output)
        self.assertIn('name="csrf_token" value="synthetic-csrf"', output)
        self.assertIn('name="config_version" value="7"', output)
        self.assertIn('action="/scan" method="post"', output)
        self.assertIn('action="/settings" method="post"', output)
        self.assertIn('rel="noopener noreferrer"', output)

    def test_empty_state_and_no_history_are_readable(self):
        output = self.render()
        self.assertIn("No matching deals right now", output)
        self.assertIn("0 matching fares", output)
        self.assertIn("No route history.", output)
