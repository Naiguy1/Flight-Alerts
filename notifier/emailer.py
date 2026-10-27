import logging
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
import yaml
from jinja2 import Template
from analyzer.evaluator import QualifiedDeal

logger = logging.getLogger(__name__)

HTML_EMAIL_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Flight Deal Alert</title>
</head>
<body style="margin:0; padding:0; background-color:#f4f6f8; font-family:-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; color:#1c252c;">
  <table width="100%" cellpadding="0" cellspacing="0" border="0" style="background-color:#f4f6f8; padding:24px 12px;">
    <tr>
      <td align="center">
        <table width="600" cellpadding="0" cellspacing="0" border="0" style="max-width:600px; width:100%; background-color:#ffffff; border-radius:8px; overflow:hidden; border:1px solid #dce1e6;">
          <tr>
            <td style="background-color:#0f2942; padding:24px 28px; color:#ffffff;">
              <div style="font-size:12px; text-transform:uppercase; letter-spacing:1.2px; color:#8ab4f8; font-weight:600;">
                Flight Deal Alert ({{ deal.offer.origin }})
              </div>
              <div style="font-size:26px; font-weight:700; margin-top:6px;">
                {{ deal.offer.origin }} to {{ deal.offer.destination_city }} ({{ deal.offer.destination }})
              </div>
            </td>
          </tr>
          <tr>
            <td style="padding:24px 28px; border-bottom:1px solid #eaeeef; background-color:#f8fbff;">
              <table width="100%" cellpadding="0" cellspacing="0" border="0">
                <tr>
                  <td width="50%" valign="top">
                    <div style="font-size:13px; color:#5a6872; font-weight:500;">Found Roundtrip Fare</div>
                    <div style="font-size:36px; font-weight:800; color:#0b6e4f; margin-top:4px;">
                      ${{ "%.0f"|format(deal.offer.price_usd) }}
                    </div>
                    <div style="font-size:13px; color:#5a6872; margin-top:4px;">
                      Typical Route Average: <span style="text-decoration:line-through;">${{ "%.0f"|format(deal.average_price_usd) }}</span>
                    </div>
                  </td>
                  <td width="50%" valign="middle" align="right">
                    <div style="display:inline-block; background-color:#0b6e4f; color:#ffffff; padding:10px 16px; border-radius:6px; font-size:20px; font-weight:700;">
                      Save {{ "%.1f"|format(deal.discount_percent) }}%
                    </div>
                    <div style="font-size:12px; color:#5a6872; margin-top:8px;">
                      Save ${{ "%.0f"|format(deal.average_price_usd - deal.offer.price_usd) }} off baseline
                    </div>
                  </td>
                </tr>
              </table>
            </td>
          </tr>
          <tr>
            <td style="padding:24px 28px;">
              <table width="100%" cellpadding="8" cellspacing="0" border="0" style="font-size:15px;">
                <tr>
                  <td width="38%" style="color:#5a6872; font-weight:600; border-bottom:1px solid #f0f2f4;">Route</td>
                  <td style="color:#1c252c; border-bottom:1px solid #f0f2f4;">
                    <strong>{{ deal.offer.origin }}</strong> &rarr; <strong>{{ deal.offer.destination }}</strong> ({{ deal.offer.destination_city }}{% if deal.offer.country_code %}, {{ deal.offer.country_code }}{% endif %})
                  </td>
                </tr>
                <tr>
                  <td style="color:#5a6872; font-weight:600; border-bottom:1px solid #f0f2f4;">Dates</td>
                  <td style="color:#1c252c; border-bottom:1px solid #f0f2f4;">
                    {{ deal.offer.outbound_date }} to {{ deal.offer.return_date }} ({{ deal.offer.trip_duration_days }} days)
                  </td>
                </tr>
                <tr>
                  <td style="color:#5a6872; font-weight:600; border-bottom:1px solid #f0f2f4;">Airline(s)</td>
                  <td style="color:#1c252c; border-bottom:1px solid #f0f2f4;">
                    {{ deal.offer.airlines | join(", ") }}
                  </td>
                </tr>
                <tr>
                  <td style="color:#5a6872; font-weight:600; border-bottom:1px solid #f0f2f4;">Layovers</td>
                  <td style="color:#1c252c; border-bottom:1px solid #f0f2f4;">
                    {% if deal.offer.layovers == 0 %}Nonstop{% elif deal.offer.layovers == 1 %}1 Layover{% else %}{{ deal.offer.layovers }} Layovers{% endif %}
                  </td>
                </tr>
                <tr>
                  <td style="color:#5a6872; font-weight:600;">Data Source</td>
                  <td style="color:#1c252c;">
                    {{ deal.offer.source }} (Baseline: {{ deal.baseline_source }})
                  </td>
                </tr>
              </table>
              <div style="text-align:center; margin-top:28px; margin-bottom:8px;">
                <a href="{{ deal.offer.booking_url }}" target="_blank" rel="noopener noreferrer"
                   style="background-color:#1a73e8; color:#ffffff; text-decoration:none; padding:14px 28px; border-radius:6px; font-weight:700; font-size:16px; display:inline-block;">
                  View &amp; Book Flight
                </a>
              </div>
            </td>
          </tr>
        </table>
      </td>
    </tr>
  </table>
</body>
</html>
"""


class EmailNotifier:
    def __init__(
        self,
        smtp_host: str,
        smtp_port: int,
        smtp_username: str,
        smtp_password: str,
        sender_email: str,
        recipient_email: str,
        config_path: str = "/app/config.yaml",
    ):
        self.smtp_host = smtp_host
        self.smtp_port = smtp_port
        self.smtp_username = smtp_username
        self.smtp_password = smtp_password
        self.sender_email = sender_email
        self.config_path = config_path
        self.fallback_recipient = recipient_email
        self.template = Template(HTML_EMAIL_TEMPLATE)

    def _get_recipients(self) -> list[str]:
        """Reads recipient_emails from config.yaml, falling back to .env if not set."""
        try:
            with open(self.config_path, "r", encoding="utf-8") as fh:
                cfg = yaml.safe_load(fh) or {}
                filters = cfg.get("filters") or {}
                if "recipient_emails" in filters and isinstance(filters["recipient_emails"], list):
                    cleaned = []
                    for item in filters["recipient_emails"]:
                        for part in str(item).split(","):
                            addr = part.strip()
                            if addr and addr.lower() not in [c.lower() for c in cleaned]:
                                cleaned.append(addr)
                    if cleaned:
                        return cleaned
        except Exception as exc:
            logger.warning("Could not read recipients from config.yaml: %s", exc)
        return [r.strip() for r in self.fallback_recipient.split(",") if r.strip()]

    def send_deal_alert(self, deal: QualifiedDeal) -> bool:
        recipients = self._get_recipients()
        if not recipients:
            logger.warning("No recipient emails configured; skipping email alert.")
            return False

        offer = deal.offer
        subject = (
            f"[Deal Alert] {offer.origin} to {offer.destination_city} ({offer.destination}) - "
            f"${offer.price_usd:.0f} RT ({deal.discount_percent:.0f}% Off)"
        )
        plain_body = self.build_plain_text(deal)
        html_body = self.build_html(deal)

        sent_count = 0
        try:
            with smtplib.SMTP(self.smtp_host, self.smtp_port, timeout=20) as server:
                server.ehlo()
                server.starttls()
                server.ehlo()
                if self.smtp_username and self.smtp_password:
                    server.login(self.smtp_username, self.smtp_password)

                # Send an individual email to each recipient so no one sees anyone else's address
                for recipient in recipients:
                    msg = MIMEMultipart("alternative")
                    msg["Subject"] = subject
                    msg["From"] = self.sender_email
                    msg["To"] = recipient
                    msg.attach(MIMEText(plain_body, "plain", "utf-8"))
                    msg.attach(MIMEText(html_body, "html", "utf-8"))

                    try:
                        server.sendmail(self.sender_email, [recipient], msg.as_string())
                        sent_count += 1
                        logger.info(
                            "Delivered private alert email for %s->%s to %s",
                            offer.origin, offer.destination, recipient,
                        )
                    except Exception as recipient_exc:
                        logger.error("Failed delivering to %s: %s", recipient, recipient_exc)

            return sent_count > 0
        except Exception as exc:
            logger.error("SMTP connection failed for %s->%s: %s", offer.origin, offer.destination, exc)
            return False

    def build_html(self, deal: QualifiedDeal) -> str:
        return self.template.render(deal=deal)

    def build_plain_text(self, deal: QualifiedDeal) -> str:
        offer = deal.offer
        return (
            f"FLIGHT DEAL ALERT: {offer.origin} -> {offer.destination_city} ({offer.destination})\n"
            f"Found Price: ${offer.price_usd:.0f} (Typical Avg: ${deal.average_price_usd:.0f} | Save {deal.discount_percent:.1f}%)\n"
            f"Dates: {offer.outbound_date} to {offer.return_date} ({offer.trip_duration_days} days)\n"
            f"Airlines: {', '.join(offer.airlines)} | Layovers: {offer.layovers}\n"
            f"Book / View Itinerary: {offer.booking_url}\n"
        )
