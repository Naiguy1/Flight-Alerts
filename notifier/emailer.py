"""Verified SMTP delivery and escaped email rendering.

Recipients are passed explicitly by the durable delivery service. No recipient is
read from YAML or an environment fallback, so an empty subscription list stays
empty. SMTP acknowledgment can be ambiguous after a network failure; delivery
therefore follows the outbox's documented at-least-once semantics.
"""
from __future__ import annotations

import html
import math
import re
import smtplib
import ssl
from datetime import date
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from typing import Any
from urllib.parse import urlencode, urlsplit

from fetcher.base import sanitize_booking_url
from web.presentation import money, observed_time, percentage, travel_date as display_date

_LOCAL_CHARACTERS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789!#$%&'*+/=?^_`{|}~-")
_DOMAIN_LABEL = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\Z")
_MAX_BODY = 1_000_000


class DeliveryError(Exception):
    """Safe error category suitable for persistence and operator dashboards."""
    def __init__(self, category: str, retryable: bool = True):
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", category):
            category = "delivery_failed"
        self.category = category
        self.retryable = bool(retryable)
        super().__init__(category)


def normalize_email(address: str) -> str:
    """Accept one ASCII mailbox (IDNA domain permitted), never header syntax."""
    if not isinstance(address, str) or len(address) > 254 or any(ord(c) < 32 or ord(c) == 127 for c in address):
        raise ValueError("Invalid email address")
    address = address.strip()
    if address.count("@") != 1:
        raise ValueError("Invalid email address")
    local, domain = address.rsplit("@", 1)
    try:
        domain = domain.encode("idna").decode("ascii").lower()
    except UnicodeError as exc:
        raise ValueError("Invalid email address") from exc
    labels = domain.split(".")
    local_parts = local.split(".")
    valid_local = all(part and all(char in _LOCAL_CHARACTERS for char in part) for part in local_parts)
    if (not 0 < len(local) <= 64 or not valid_local
            or len(labels) < 2 or len(domain) > 253
            or any(not _DOMAIN_LABEL.fullmatch(label) for label in labels)
            or len(local) + len(domain) + 1 > 254):
        raise ValueError("Invalid email address")
    return local.lower() + "@" + domain


def _https_url(url: str, *, booking: bool = False) -> str:
    if booking:
        clean = sanitize_booking_url(url)
        if not clean:
            raise ValueError("Invalid HTTPS URL")
        return clean
    if not isinstance(url, str) or len(url) > 4096 or any(ord(c) <= 32 or ord(c) == 127 for c in url):
        raise ValueError("Invalid HTTPS URL")
    try:
        parsed = urlsplit(url)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username is not None
                or parsed.password is not None
                or (parsed.port is not None and not 1 <= parsed.port <= 65535) or parsed.fragment):
            raise ValueError("Invalid HTTPS URL")
    except (ValueError, UnicodeError) as exc:
        raise ValueError("Invalid HTTPS URL") from exc
    return url


def _text(value: Any, max_length: int = 500) -> str:
    raw = str(value if value is not None else "")
    if len(raw) > max_length or any(ord(c) < 32 or ord(c) == 127 for c in raw):
        raise ValueError("Invalid message field")
    return raw


def _positive(value: Any) -> float:
    if isinstance(value, bool):
        raise ValueError("Invalid fare")
    number = float(value)
    if not math.isfinite(number) or number <= 0 or number > 1_000_000:
        raise ValueError("Invalid fare")
    return number


def email_button(label: str, url: str) -> str:
    """A real HTTPS link with inline styles for clients without stylesheet support."""
    return ('<table role="presentation" cellpadding="0" cellspacing="0"><tr><td bgcolor="#0b7353" '
            'style="border-radius:8px;background-color:#0b7353;text-align:center">'
            '<a href="' + html.escape(_https_url(url), quote=True) + '" '
            'style="display:inline-block;padding:15px 24px;color:#ffffff;font-weight:bold;'
            'font-size:16px;line-height:24px;text-decoration:none">'
            + html.escape(_text(label, 100)) + '</a></td></tr></table>')


def render_message(title: str, body_html: str, unsubscribe_url: str | None = None) -> str:
    title = html.escape(_text(title, 200))
    footer = ""
    if unsubscribe_url is not None:
        footer = ('<p style="margin:12px 0 0"><a href="'
                  + html.escape(_https_url(unsubscribe_url), quote=True)
                  + '" style="color:#b7d5f8;text-decoration:underline">Unsubscribe from alerts</a></p>')
    # Only internally generated, escaped markup may be passed as body_html.
    # Tables and inline colors preserve the design when email CSS is stripped.
    return ('<!doctype html><html lang="en"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            '<title>' + title + '</title></head>'
            '<body bgcolor="#141b24" style="margin:0;padding:0;background-color:#141b24;'
            'color:#f3f7fb;font-family:Arial,Helvetica,sans-serif">'
            '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" bgcolor="#141b24">'
            '<tr><td align="center" style="padding:24px 12px">'
            '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
            'style="max-width:640px;border:1px solid #3a4959;border-radius:16px;background-color:#1f2933">'
            '<tr><td bgcolor="#102c44" style="padding:28px 24px;background-color:#102c44;'
            'border-radius:16px 16px 0 0">'
            '<p style="margin:0 0 10px;color:#9fc5ff;font-size:12px;letter-spacing:2px;font-weight:bold">'
            'FLIGHT ALERTS</p><h1 style="margin:0;color:#ffffff;font-size:28px;line-height:1.3">'
            + title + '</h1></td></tr><tr><td style="padding:24px;color:#f3f7fb;line-height:1.6">'
            + body_html + '</td></tr><tr><td style="padding:20px 24px;border-top:1px solid #3a4959;'
            'color:#c0cbd7;font-size:12px;line-height:1.6">Flight Alerts' + footer
            + '</td></tr></table></td></tr></table></body></html>')


def render_invitation(confirmation_url: str, unsubscribe_url: str) -> tuple[str, str, str]:
    confirmation_url, unsubscribe_url = _https_url(confirmation_url), _https_url(unsubscribe_url)
    subject = "Confirm your Flight-Alerts subscription"
    plain = ("An administrator invited this address to Flight-Alerts.\n"
             "Confirm only if you want to receive flight alerts:\n" + confirmation_url
             + "\nThis confirmation link expires in 48 hours.\n"
             "If this invitation was unexpected, ignore it or disable it:\n" + unsubscribe_url + "\n")
    content = ('<p style="margin:0 0 16px;font-size:18px;color:#f3f7fb">You’re invited to Flight Alerts.</p>'
               '<p style="margin:0 0 24px;color:#c0cbd7">An administrator invited this address. '
               'Confirm only if you want to receive flight alerts.</p>'
               + email_button("Confirm subscription", confirmation_url)
               + '<p style="margin:18px 0 0;color:#c0cbd7;font-size:13px">'
               'This confirmation link expires in <strong>48 hours</strong>. '
               'If this invitation was unexpected, ignore it or use the unsubscribe link below. '
               'No alerts will be sent until you confirm.</p>')
    return subject, plain, render_message("Confirm your flight alerts", content, unsubscribe_url)


def render_deal(payload: dict) -> tuple[str, str, str]:
    offer = payload.get("offer")
    if not isinstance(offer, dict):
        raise ValueError("Invalid deal payload")
    origin = _text(offer.get("origin"), 3)
    destination = _text(offer.get("destination"), 3)
    if not re.fullmatch(r"[A-Z]{3}", origin) or not re.fullmatch(r"[A-Z]{3}", destination):
        raise ValueError("Invalid route")
    if offer.get("currency", "USD") != "USD":
        raise ValueError("Unsupported fare currency")
    price, average = _positive(offer["price_usd"]), _positive(payload["average_price_usd"])
    discount = float(payload["discount_percent"])
    if not math.isfinite(discount) or not 0 <= discount <= 100:
        raise ValueError("Invalid discount")
    expected_discount = (average - price) / average * 100
    if not math.isclose(discount, expected_discount, rel_tol=1e-8, abs_tol=1e-6):
        raise ValueError("Discount does not match the fare and baseline")
    city = _text(offer.get("destination_city", destination), 100)
    outbound = _text(offer.get("outbound_date"), 10)
    returning = _text(offer.get("return_date"), 10)
    if date.fromisoformat(returning) <= date.fromisoformat(outbound):
        raise ValueError("Invalid travel dates")
    raw_url = offer.get("booking_url")
    if raw_url is None or raw_url == "":
        raw_url = "https://www.google.com/travel/flights?" + urlencode({
            "q": f"Flights from {origin} to {destination} on {outbound} through {returning}", "curr": "USD",
        })
    booking = _https_url(raw_url, booking=True)
    airlines = offer.get("airlines", [])
    if not isinstance(airlines, list) or len(airlines) > 20:
        raise ValueError("Invalid airline list")
    airline_names = ", ".join(_text(a, 100) for a in airlines) or "See booking provider"
    source = _text(offer.get("source", "Provider"), 100)
    baseline_source = _text(payload.get("baseline_source", "Configured baseline"), 200)
    observed = _text(offer.get("observed_at", "See alert discovery time"), 100)
    scope = "outbound details" if offer.get("metadata_scope", "outbound") == "outbound" else "roundtrip details"
    stops = offer.get("layovers", 0)
    if type(stops) is not int or not 0 <= stops <= 20:
        raise ValueError("Invalid stop count")
    subject = f"[Flight deal] {origin} to {destination}: {money(price)} ({percentage(discount)} below baseline)"
    savings = average - price
    plain = (
        f"{origin} to {city} ({destination})\nRoundtrip search fare: {money(price)} USD\n"
        f"Baseline: {money(average)} USD ({baseline_source}); difference: {percentage(discount)}\n"
        f"Save {money(savings)} against this baseline.\n"
        f"Travel dates: {outbound} to {returning}\nAirlines ({scope}): {airline_names}\n"
        f"Layovers ({scope}): {stops}\nSource: {source}\nObserved: {observed}\n"
        f"Fare availability and conditions can change. Recheck with the provider.\nBook: {booking}\n"
    )
    def cell(label: str, value: str) -> str:
        return ('<tr><th scope="row" width="35%" style="padding:12px 8px 12px 0;text-align:left;'
                'vertical-align:top;border-bottom:1px solid #3a4959;color:#c0cbd7;font-size:14px">'
                + html.escape(label) + '</th><td style="padding:12px 0;border-bottom:1px solid #3a4959;'
                'color:#f3f7fb;font-size:14px;overflow-wrap:anywhere">' + html.escape(value) + '</td></tr>')
    card = ('<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
            'style="margin:0 0 28px"><tr><td>'
            '<p style="margin:0 0 6px;color:#9fc5ff;font-size:12px;letter-spacing:1px;font-weight:bold">'
            + html.escape(f"FLIGHT DEAL ALERT ({origin})")
            + '</p><h2 style="margin:0 0 20px;font-size:26px;line-height:1.3;color:#ffffff">'
            + html.escape(f"{origin} to {city} ({destination})") + '</h2>'
            '<table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr>'
            '<td width="55%" valign="top" style="padding:0 12px 20px 0">'
            '<p style="margin:0;color:#c0cbd7;font-size:14px">Roundtrip search fare · USD</p>'
            '<p style="margin:4px 0;color:#7ad6ad;font-size:40px;line-height:1.2;font-weight:bold">'
            + money(price) + '</p><p style="margin:8px 0 0;color:#c0cbd7;font-size:14px">'
            'Baseline: <s style="text-decoration:line-through">' + money(average) + '</s></p></td>'
            '<td width="45%" valign="top" style="padding-bottom:20px">'
            '<table role="presentation" width="100%" cellpadding="0" cellspacing="0">'
            '<tr><td align="center" bgcolor="#0b7353" style="background-color:#0b7353;'
            'border-radius:10px;padding:14px 8px;color:#ffffff;font-size:23px;font-weight:bold;line-height:1.3">'
            'Save<br>' + percentage(discount) + '</td></tr></table>'
            '<p style="margin:8px 0 0;color:#c0cbd7;text-align:center;font-size:13px">Save '
            + money(savings) + ' off baseline</p></td></tr></table>'
            '<table width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse">')
    for label, value in (
        ("Route", f"{origin} → {destination} ({city})"),
        ("Dates", f"{display_date(outbound)} to {display_date(returning)}"),
        (f"Airlines ({scope})", airline_names),
        (f"Layovers ({scope})", str(stops)), ("Baseline source", baseline_source),
        ("Source", source), ("Observed", observed_time(observed) if observed_time(observed) != "—" else observed),
    ):
        card += cell(label, value)
    card += ('</table><p style="margin:20px 0 16px;color:#c0cbd7;font-size:13px">'
             'Fare availability and conditions can change. Recheck with the provider.</p>'
             + email_button("View fare with provider", booking) + '</td></tr></table>')

    return subject, plain, card


def render_status(payload: dict) -> tuple[str, str, str]:
    flight = _text(payload.get("flight_iata"), 10)
    if not re.fullmatch(r"[A-Z0-9]{2,3}[0-9]{1,4}[A-Z]?", flight):
        raise ValueError("Invalid flight identifier")
    travel_date = _text(payload.get("flight_date"), 10)
    date.fromisoformat(travel_date)
    current = payload.get("current")
    if not isinstance(current, dict):
        raise ValueError("Invalid status payload")
    subject = f"[Flight status] {flight} on {travel_date}"
    def available(value):
        return "Not available" if value is None or value == "" else value

    timestamp_label = "Provider update" if current.get("timestamp_source") == "provider" else "Last checked"
    labels = (
        ("Status", current.get("status", "Unknown")),
        ("Reported delay (minutes)", available(current.get("delay_minutes"))),
        ("Departure gate", available(current.get("departure_gate"))),
        ("Arrival gate", available(current.get("arrival_gate"))),
        (timestamp_label, available(current.get("updated_at"))),
    )
    plain = f"{flight} on {travel_date}\n" + "\n".join(label + ": " + _text(value) for label, value in labels)
    card = '<p>' + html.escape(f"{flight} on {travel_date}") + '</p><dl>'
    for label, value in labels:
        card += '<dt>' + html.escape(label) + '</dt><dd>' + html.escape(_text(value)) + '</dd>'
    return subject, plain + "\nCheck the airline for the latest operational information.\n", card + '</dl><p>Check the airline for the latest operational information.</p>'


class EmailNotifier:
    def __init__(
        self, smtp_host: str, smtp_port: int, smtp_username: str, smtp_password: str,
        sender_email: str, recipient_email: str = "", config_path: str | None = None,
        *, timeout: float = 20,
    ):
        self.smtp_host = smtp_host
        self.smtp_port = int(smtp_port)
        self.smtp_username = smtp_username
        self._smtp_password = smtp_password
        self.sender_email = normalize_email(sender_email) if sender_email else ""
        self.timeout = float(timeout)
        if not 1 <= self.timeout <= 60 or not 1 <= self.smtp_port <= 65535:
            raise ValueError("Invalid SMTP configuration")
        if bool(smtp_username) != bool(smtp_password):
            raise ValueError("SMTP username and password must both be configured")
        # Compatibility constructor arguments are deliberately not a recipient
        # source. Only verified DB subscriptions authorize notification delivery.
        del recipient_email, config_path

    def send_message(
        self, recipient: str, subject: str, plain_body: str, html_body: str,
        unsubscribe_url: str | None = None,
    ) -> None:
        recipient = normalize_email(recipient)
        subject = _text(subject, 200)
        if not self.smtp_host or not self.sender_email:
            raise DeliveryError("smtp_not_configured", retryable=False)
        if not isinstance(plain_body, str) or not isinstance(html_body, str) or len(plain_body) + len(html_body) > _MAX_BODY:
            raise DeliveryError("message_too_large", retryable=False)
        message = EmailMessage()
        message["From"] = self.sender_email
        message["To"] = recipient
        message["Subject"] = subject
        message["Date"] = formatdate(localtime=False, usegmt=True)
        message["Message-ID"] = make_msgid(domain=self.sender_email.rsplit("@", 1)[1])
        if unsubscribe_url:
            message["List-Unsubscribe"] = "<" + _https_url(unsubscribe_url) + ">"
        message.set_content(plain_body)
        message.add_alternative(html_body, subtype="html")
        context = ssl.create_default_context()
        try:
            if self.smtp_port == 465:
                connection = smtplib.SMTP_SSL(self.smtp_host, self.smtp_port, timeout=self.timeout, context=context)
            else:
                connection = smtplib.SMTP(self.smtp_host, self.smtp_port, timeout=self.timeout)
            with connection as server:
                server.ehlo()
                if self.smtp_port != 465:
                    server.starttls(context=context)
                    server.ehlo()
                if self.smtp_username:
                    server.login(self.smtp_username, self._smtp_password)
                refused = server.send_message(message, from_addr=self.sender_email, to_addrs=[recipient])
                if refused:
                    raise DeliveryError("recipient_refused", retryable=False)
        except ssl.SSLError:
            raise DeliveryError("smtp_tls_failed", retryable=False) from None
        except smtplib.SMTPAuthenticationError:
            raise DeliveryError("smtp_authentication_failed", retryable=False) from None
        except smtplib.SMTPRecipientsRefused as exc:
            transient = any(400 <= int(code) < 500 for code, _ in exc.recipients.values())
            raise DeliveryError("recipient_refused", retryable=transient) from None
        except smtplib.SMTPResponseException as exc:
            raise DeliveryError("smtp_rejected", retryable=400 <= exc.smtp_code < 500) from None
        except (smtplib.SMTPException, OSError):
            # The server may already have accepted DATA. The durable worker may
            # retry; duplicates remain possible under documented at-least-once semantics.
            raise DeliveryError("smtp_network_failed", retryable=True) from None
