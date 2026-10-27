import os
import subprocess
import threading
import urllib.parse
import yaml
from dotenv import load_dotenv
from fastapi import FastAPI, Form, Request, BackgroundTasks
from fastapi.responses import HTMLResponse, RedirectResponse
from jinja2 import Template
from sqlalchemy import select, desc, func

from analyzer.evaluator import QualifiedDeal
from db.crud import DatabaseManager
from db.models import PriceHistory, AlertLog
from fetcher.base import FlightOffer
from notifier.emailer import EmailNotifier

app = FastAPI(title="SLC & LAS Flight Deal Monitor")

CONFIG_PATH = os.getenv("CONFIG_PATH", "/app/config.yaml")
DB_URL = os.getenv("DATABASE_URL", "sqlite:////app/data/flight_deals.db")
db = DatabaseManager(DB_URL)
scan_lock = threading.Lock()

DASHBOARD_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>SLC & LAS Flight Deal Monitor</title>
  <style>
    body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; background: #f4f6f8; color: #1c252c; margin: 0; padding: 16px; }
    .container { max-width: 1180px; margin: 0 auto; }
    .header { display: flex; flex-wrap: wrap; justify-content: space-between; align-items: center; gap: 12px; margin-bottom: 20px; }
    .notice { background: #e6f4ea; border: 1px solid #0b6e4f; color: #0b6e4f; padding: 12px 16px; border-radius: 6px; margin-bottom: 18px; font-weight: 600; font-size: 14px; }
    .card { background: #fff; border-radius: 8px; padding: 20px; margin-bottom: 20px; border: 1px solid #dce1e6; overflow-x: auto; }
    h1, h2, h3 { margin-top: 0; color: #0f2942; }
    table { width: 100%; border-collapse: collapse; font-size: 14px; }
    th, td { text-align: left; padding: 10px 12px; border-bottom: 1px solid #eaeeef; vertical-align: middle; }
    th { color: #5a6872; font-weight: 600; background: #f8fbff; }
    .badge { background: #0b6e4f; color: #fff; padding: 4px 8px; border-radius: 4px; font-weight: 600; font-size: 12px; }
    .tag { display: inline-flex; align-items: center; gap: 8px; background: #e8eaed; padding: 6px 12px; border-radius: 16px; font-size: 13px; margin: 4px; }
    .btn { background: #1a73e8; color: #fff; border: none; padding: 9px 15px; border-radius: 6px; font-weight: 600; cursor: pointer; text-decoration: none; font-size: 14px; display: inline-block; }
    .btn-book { background: #0b6e4f; color: #fff; padding: 6px 12px; border-radius: 5px; font-weight: 600; font-size: 13px; text-decoration: none; display: inline-block; white-space: nowrap; }
    .btn-book:hover { background: #08543c; }
    .btn-secondary { background: #0f2942; }
    .btn-danger { background: #c5221f; color: #fff; border: none; padding: 5px 10px; font-size: 12px; border-radius: 4px; cursor: pointer; font-weight: 600; }
    .form-grid { display: flex; flex-wrap: wrap; gap: 10px; align-items: flex-end; margin-bottom: 18px; background: #f8fbff; padding: 14px; border-radius: 6px; border: 1px solid #e1e8f0; }
    .form-group { display: flex; flex-direction: column; gap: 4px; font-size: 12px; font-weight: 600; color: #4a5568; }
    input[type="text"], input[type="number"], input[type="email"] { padding: 8px 10px; border: 1px solid #cbd5e0; border-radius: 4px; font-size: 14px; }
    .airport-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(250px, 1fr)); gap: 10px; }
    .airport-pill { display: flex; justify-content: space-between; align-items: center; background: #f8f9fa; border: 1px solid #dadce0; border-radius: 6px; padding: 8px 12px; font-size: 13px; }
  </style>
</head>
<body>
  <div class="container">
    {% if msg %}
    <div class="notice">{{ msg }}</div>
    {% endif %}

    <div class="header">
      <div>
        <h1>SLC &amp; LAS Flight Deal Monitor</h1>
        <small>Scanning every <strong>{{ scan_interval_hours }} hours</strong> &bull; Alerting on <strong>{{ "%.0f"|format(threshold) }}%+</strong> below baseline (Private individual delivery)</small>
      </div>
      <form action="/scan" method="post" style="margin:0;">
        <button type="submit" class="btn">Trigger Immediate Scan</button>
      </form>
    </div>

    <!-- Email Alert Recipients Management -->
    <div class="card">
      <h2>Email Alert Recipients ({{ recipient_emails | length }})</h2>
      <p style="font-size:13px; color:#5a6872; margin-top:0;">
        Each address receives its own private copy of deal alerts so recipients never see each other's address:
      </p>
      <div style="margin-bottom: 16px;">
        {% if recipient_emails %}
          {% for email in recipient_emails %}
          <span class="tag">
            <strong>{{ email }}</strong>
            <form action="/emails/remove" method="post" style="display:inline; margin:0;">
              <input type="hidden" name="email" value="{{ email }}">
              <button type="submit" class="btn-danger" title="Remove {{ email }}">Remove &times;</button>
            </form>
          </span>
          {% endfor %}
        {% else %}
          <p style="color:#c5221f; font-size:13px;">No recipient emails currently configured. Add an email below.</p>
        {% endif %}
      </div>
      <div style="display:flex; gap:10px; flex-wrap:wrap; align-items:center;">
        <form action="/emails/add" method="post" style="display:flex; gap:8px; flex-wrap:wrap; margin:0;">
          <input type="email" name="email" placeholder="name@example.com" style="width:260px;" required>
          <button type="submit" class="btn btn-secondary">+ Add Recipient Email</button>
        </form>
        <form action="/emails/test" method="post" style="margin:0;">
          <button type="submit" class="btn" style="background:#0b6e4f;">Send Latest Alert to Email List Now</button>
        </form>
      </div>
    </div>

    <!-- Last 12 Triggered Deal Alerts with Direct Booking Links -->
    <div class="card">
      <h2>Last 12 Triggered Deal Alerts ({{ "%.0f"|format(threshold) }}%+ Off)</h2>
      {% if alerts %}
      <table>
        <thead>
          <tr>
            <th>Sent (UTC)</th>
            <th>Route</th>
            <th>Dates</th>
            <th>Airline &amp; Stops</th>
            <th>Found Price</th>
            <th>Baseline Avg</th>
            <th>Savings</th>
            <th>Action</th>
          </tr>
        </thead>
        <tbody>
          {% for a in alerts %}
          <tr>
            <td>{{ a.sent_at }}</td>
            <td><strong>{{ a.origin }} &rarr; {{ a.destination }}</strong>{% if a.destination_city %} ({{ a.destination_city }}){% endif %}</td>
            <td>{{ a.outbound_date }} to {{ a.return_date }}</td>
            <td>{{ a.airlines }} &bull; {% if a.layovers == 0 %}Nonstop{% elif a.layovers == 1 %}1 Layover{% else %}{{ a.layovers }} Layovers{% endif %}</td>
            <td><strong style="color:#0b6e4f; font-size:16px;">${{ "%.0f"|format(a.alerted_price_usd) }}</strong></td>
            <td style="text-decoration:line-through; color:#5a6872;">${{ "%.0f"|format(a.average_price_usd) }}</td>
            <td><span class="badge">-{{ "%.1f"|format(a.discount_percent) }}%</span></td>
            <td>
              <a href="{{ a.booking_url }}" target="_blank" rel="noopener noreferrer" class="btn-book">
                View &amp; Book &rarr;
              </a>
            </td>
          </tr>
          {% endfor %}
        </tbody>
      </table>
      {% else %}
      <p>No alerts triggered yet. Deals dropping {{ "%.0f"|format(threshold) }}% or more below average will appear here.</p>
      {% endif %}
    </div>

    <!-- Last 12 Lowest Scanned Flight Deals Across Active Monitored Routes -->
    <div class="card">
      <h2>Latest 12 Lowest Scanned Flights (Click to Book)</h2>
      {% if recent_deals %}
      <table>
        <thead>
          <tr>
            <th>Scanned (UTC)</th>
            <th>Route</th>
            <th>Travel Dates</th>
            <th>Airline &amp; Stops</th>
            <th>Roundtrip Price</th>
            <th>Action</th>
          </tr>
        </thead>
        <tbody>
          {% for d in recent_deals %}
          <tr>
            <td>{{ d.recorded_at }}</td>
            <td><strong>{{ d.origin }} &rarr; {{ d.destination }}</strong> ({{ d.destination_city }}, {{ d.country_code }})</td>
            <td>{{ d.outbound_date }} to {{ d.return_date }}</td>
            <td>{{ d.airlines }} &bull; {% if d.layovers == 0 %}Nonstop{% elif d.layovers == 1 %}1 Layover{% else %}{{ d.layovers }} Layovers{% endif %}</td>
            <td><strong>${{ "%.0f"|format(d.price_usd) }}</strong></td>
            <td>
              <a href="{{ d.booking_url }}" target="_blank" rel="noopener noreferrer" class="btn-book" style="background:#1a73e8;">
                View &amp; Book &rarr;
              </a>
            </td>
          </tr>
          {% endfor %}
        </tbody>
      </table>
      {% else %}
      <p>Scanning initial routes... Refresh in a few seconds.</p>
      {% endif %}
    </div>

    <!-- Monitored Airports Management (Add / Remove) -->
    <div class="card">
      <h2>Monitored Destination Airports ({{ destinations | length }})</h2>
      <form action="/airports/add" method="post" class="form-grid">
        <div class="form-group">
          <label>Airport Code (IATA)</label>
          <input type="text" name="code" placeholder="e.g. OGG or DUB" maxlength="3" style="width:110px; text-transform:uppercase;" required>
        </div>
        <div class="form-group">
          <label>City / Airport Name</label>
          <input type="text" name="city" placeholder="e.g. Maui or Dublin" style="width:180px;" required>
        </div>
        <div class="form-group">
          <label>Country (2-Letter)</label>
          <input type="text" name="country_code" placeholder="US, CA, MX, JP..." maxlength="2" style="width:110px; text-transform:uppercase;" required>
        </div>
        <div class="form-group">
          <label>SLC Avg Baseline ($)</label>
          <input type="number" name="slc_baseline" value="800" min="50" max="5000" style="width:120px;" required>
        </div>
        <div class="form-group">
          <label>LAS Avg Baseline ($)</label>
          <input type="number" name="las_baseline" value="720" min="50" max="5000" style="width:120px;" required>
        </div>
        <button type="submit" class="btn btn-secondary">+ Add / Update Airport</button>
      </form>

      <div class="airport-grid">
        {% for dest in destinations %}
        <div class="airport-pill">
          <div>
            <strong>{{ dest.code }}</strong> &bull; {{ dest.city }} ({{ dest.country_code }})<br>
            <span style="color:#5a6872; font-size:11px;">
              Base: SLC ${{ "%.0f"|format(dest.slc_base) }} | LAS ${{ "%.0f"|format(dest.las_base) }}
            </span>
          </div>
          <form action="/airports/remove" method="post" style="margin:0;">
            <input type="hidden" name="code" value="{{ dest.code }}">
            <button type="submit" class="btn-danger" title="Stop monitoring {{ dest.code }}">&times;</button>
          </form>
        </div>
        {% endfor %}
      </div>
    </div>

    <!-- Blocklist Management -->
    <div class="card">
      <h2>Destination Blocklist</h2>
      <p style="font-size:13px; color:#5a6872;">Airports or cities explicitly suppressed from alerts:</p>
      <div style="margin-bottom: 14px;">
        {% for item in blocked_destinations %}
          <span class="tag">
            {{ item }}
            <form action="/blocklist/remove" method="post" style="display:inline; margin:0;">
              <input type="hidden" name="term" value="{{ item }}">
              <button type="submit" class="btn-danger" title="Unblock {{ item }}">&times;</button>
            </form>
          </span>
        {% endfor %}
      </div>
      <form action="/blocklist/add" method="post" style="display:flex; gap:8px; flex-wrap:wrap;">
        <input type="text" name="term" placeholder="Add IATA code or city to block" required>
        <button type="submit" class="btn btn-secondary">Block Destination</button>
      </form>
    </div>

    <!-- Route Baselines & Lowest Observed Fares -->
    <div class="card">
      <h2>Historical Route Averages (Active Monitored Airports Only)</h2>
      <table>
        <thead>
          <tr>
            <th>Origin</th>
            <th>Destination</th>
            <th>City</th>
            <th>Samples</th>
            <th>Min Observed</th>
            <th>Effective Baseline</th>
            <th>{{ "%.0f"|format(threshold) }}% Alert Target</th>
          </tr>
        </thead>
        <tbody>
          {% for r in route_stats %}
          <tr>
            <td><strong>{{ r.origin }}</strong></td>
            <td><strong>{{ r.destination }}</strong></td>
            <td>{{ r.destination_city }} ({{ r.country_code }})</td>
            <td>{{ r.samples }}</td>
            <td>${{ "%.0f"|format(r.min_price) }}</td>
            <td>${{ "%.0f"|format(r.effective_base) }}</td>
            <td><strong>&le; ${{ "%.0f"|format(r.target_price) }}</strong></td>
          </tr>
          {% endfor %}
        </tbody>
      </table>
    </div>
  </div>
</body>
</html>
"""


def read_config() -> dict:
    with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def write_config(cfg: dict) -> None:
    with open(CONFIG_PATH, "w", encoding="utf-8") as fh:
        yaml.safe_dump(cfg, fh, sort_keys=False)


def get_clean_email_list(cfg: dict) -> list[str]:
    filters = cfg.setdefault("filters", {})
    if "recipient_emails" not in filters:
        env_raw = os.getenv("ALERT_RECIPIENT_EMAIL", "")
        filters["recipient_emails"] = [e.strip() for e in env_raw.split(",") if e.strip()]
        write_config(cfg)

    raw_list = filters.get("recipient_emails") or []
    cleaned = []
    for entry in raw_list:
        for part in str(entry).split(","):
            addr = part.strip()
            if addr and addr.lower() not in [c.lower() for c in cleaned]:
                cleaned.append(addr)
    return cleaned


def build_fallback_booking_url(origin: str, dest: str, outbound: str, ret: str) -> str:
    q = urllib.parse.quote(f"Flights to {dest} from {origin} on {outbound} through {ret}")
    return f"https://www.google.com/travel/flights?q={q}&curr=USD"


def fmt_dt(val) -> str:
    if hasattr(val, "strftime"):
        return val.strftime("%Y-%m-%d %H:%M")
    return str(val or "")[:16]


def run_locked_manual_scan() -> None:
    if not scan_lock.acquire(blocking=False):
        return
    try:
        subprocess.run(["python", "main.py", "--config", CONFIG_PATH, "--run-once"], check=False)
    finally:
        scan_lock.release()


@app.get("/", response_class=HTMLResponse)
def index(request: Request, msg: str = ""):
    cfg = read_config()
    scanner_cfg = cfg.get("scanner") or {}
    scan_interval_hours = int(scanner_cfg.get("scan_interval_hours", 3))

    filters_cfg = cfg.get("filters") or {}
    threshold = float(filters_cfg.get("discount_threshold_percent", 50.0))
    blocked_destinations = filters_cfg.get("blocked_destinations") or []
    blocked_lower = {str(b).strip().lower() for b in blocked_destinations}
    recipient_emails = get_clean_email_list(cfg)

    raw_destinations = cfg.get("monitored_destinations") or []
    destinations = []
    city_map = {}
    base_map = {}
    active_codes = set()
    for d in raw_destinations:
        code = str(d.get("code", "")).upper()
        city = str(d.get("city", ""))
        country = str(d.get("country_code", ""))
        base_dict = d.get("baseline_avg_usd") or d.get("baseline_avg_USD") or {}
        slc_base = float(base_dict.get("SLC", 800))
        las_base = float(base_dict.get("LAS", 720))
        city_map[code] = city
        base_map[code] = {"SLC": slc_base, "LAS": las_base}
        if code.lower() not in blocked_lower and city.strip().lower() not in blocked_lower:
            active_codes.add(code)
        destinations.append({
            "code": code,
            "city": city,
            "country_code": country,
            "slc_base": slc_base,
            "las_base": las_base,
        })

    with db.get_session() as session:
        raw_alerts = session.execute(
            select(AlertLog).order_by(desc(AlertLog.sent_at)).limit(12)
        ).scalars().all()

        alerts = []
        for a in raw_alerts:
            url = a.booking_url or build_fallback_booking_url(
                a.origin, a.destination, a.outbound_date, a.return_date
            )
            city = getattr(a, "destination_city", "") or city_map.get(a.destination.upper(), "")
            alerts.append({
                "sent_at": fmt_dt(a.sent_at),
                "origin": a.origin,
                "destination": a.destination,
                "destination_city": city,
                "outbound_date": a.outbound_date,
                "return_date": a.return_date,
                "airlines": getattr(a, "airlines", "") or "View Carrier",
                "layovers": int(getattr(a, "layovers", 0) or 0),
                "alerted_price_usd": float(a.alerted_price_usd or 0),
                "average_price_usd": float(a.average_price_usd or 0),
                "discount_percent": float(a.discount_percent or 0),
                "booking_url": url,
            })

        recent_stmt = select(PriceHistory)
        if active_codes:
            recent_stmt = recent_stmt.where(PriceHistory.destination.in_(active_codes))
        raw_recent = session.execute(
            recent_stmt.order_by(PriceHistory.price_usd.asc(), desc(PriceHistory.recorded_at)).limit(12)
        ).scalars().all()

        recent_deals = []
        for d in raw_recent:
            url = d.booking_url or build_fallback_booking_url(
                d.origin, d.destination, d.outbound_date, d.return_date
            )
            recent_deals.append({
                "recorded_at": fmt_dt(d.recorded_at),
                "origin": d.origin,
                "destination": d.destination,
                "destination_city": d.destination_city,
                "country_code": d.country_code,
                "outbound_date": d.outbound_date,
                "return_date": d.return_date,
                "airlines": d.airlines or "Google Flights",
                "layovers": int(d.layovers or 0),
                "price_usd": float(d.price_usd or 0),
                "booking_url": url,
            })

        stats_stmt = select(
            PriceHistory.origin,
            PriceHistory.destination,
            PriceHistory.destination_city,
            PriceHistory.country_code,
            func.count(PriceHistory.id).label("samples"),
            func.min(PriceHistory.price_usd).label("min_price"),
            func.avg(PriceHistory.price_usd).label("avg_price"),
        )
        if active_codes:
            stats_stmt = stats_stmt.where(PriceHistory.destination.in_(active_codes))
        stats_stmt = stats_stmt.group_by(
            PriceHistory.origin,
            PriceHistory.destination,
            PriceHistory.destination_city,
            PriceHistory.country_code,
        ).order_by(PriceHistory.origin, PriceHistory.destination)

        raw_stats = session.execute(stats_stmt).all()
        route_stats = []
        for r in raw_stats:
            avg_p = float(r.avg_price or 0)
            cfg_base = base_map.get(r.destination.upper(), {}).get(r.origin.upper(), avg_p)
            effective_base = max(avg_p, cfg_base) if int(r.samples or 0) >= 15 else cfg_base
            route_stats.append({
                "origin": r.origin,
                "destination": r.destination,
                "destination_city": r.destination_city,
                "country_code": r.country_code,
                "samples": int(r.samples or 0),
                "min_price": float(r.min_price or 0),
                "effective_base": effective_base,
                "target_price": effective_base * (1.0 - threshold / 100.0),
            })

    tmpl = Template(DASHBOARD_HTML)
    return tmpl.render(
        msg=msg,
        scan_interval_hours=scan_interval_hours,
        threshold=threshold,
        recipient_emails=recipient_emails,
        destinations=destinations,
        blocked_destinations=blocked_destinations,
        alerts=alerts,
        recent_deals=recent_deals,
        route_stats=route_stats,
    )


@app.post("/emails/add")
def add_email(email: str = Form(...)):
    cfg = read_config()
    emails = get_clean_email_list(cfg)
    added = []
    for part in email.split(","):
        clean = part.strip()
        if clean and clean.lower() not in [e.lower() for e in emails]:
            emails.append(clean)
            added.append(clean)
    cfg.setdefault("filters", {})["recipient_emails"] = emails
    write_config(cfg)
    msg = urllib.parse.quote(f"Added {', '.join(added) if added else email.strip()} to recipient list.")
    return RedirectResponse(url=f"/?msg={msg}", status_code=303)


@app.post("/emails/remove")
def remove_email(email: str = Form(...)):
    cfg = read_config()
    target = email.strip().lower()
    emails = get_clean_email_list(cfg)
    cfg.setdefault("filters", {})["recipient_emails"] = [
        e for e in emails if e.strip().lower() != target
    ]
    write_config(cfg)
    msg = urllib.parse.quote(f"Removed {email.strip()} from recipient list.")
    return RedirectResponse(url=f"/?msg={msg}", status_code=303)


@app.post("/emails/test")
def test_emails():
    load_dotenv()
    cfg = read_config()
    recipients = get_clean_email_list(cfg)
    if not recipients:
        return RedirectResponse(url="/?msg=No+recipient+emails+configured+to+test.", status_code=303)

    city_map = {}
    country_map = {}
    for d in cfg.get("monitored_destinations") or []:
        code = str(d.get("code", "")).upper()
        city_map[code] = str(d.get("city", ""))
        country_map[code] = str(d.get("country_code", ""))

    notifier = EmailNotifier(
        smtp_host=os.getenv("SMTP_HOST", "smtp.gmail.com"),
        smtp_port=int(os.getenv("SMTP_PORT", "587")),
        smtp_username=os.getenv("SMTP_USERNAME", ""),
        smtp_password=os.getenv("SMTP_PASSWORD", ""),
        sender_email=os.getenv("ALERT_SENDER_EMAIL", ""),
        recipient_email=os.getenv("ALERT_RECIPIENT_EMAIL", ""),
        config_path=CONFIG_PATH,
    )

    with db.get_session() as session:
        latest_alert = session.execute(
            select(AlertLog).order_by(desc(AlertLog.sent_at)).limit(1)
        ).scalar_one_or_none()

    if latest_alert:
        code_upper = latest_alert.destination.upper()
        city = getattr(latest_alert, "destination_city", "") or city_map.get(code_upper, code_upper)
        country = country_map.get(code_upper, "")
        url = latest_alert.booking_url or build_fallback_booking_url(
            latest_alert.origin, latest_alert.destination, latest_alert.outbound_date, latest_alert.return_date
        )
        airlines_list = [a.strip() for a in (getattr(latest_alert, "airlines", "") or "Google Flights").split(",") if a.strip()]
        test_deal = QualifiedDeal(
            offer=FlightOffer(
                origin=latest_alert.origin,
                destination=latest_alert.destination,
                destination_city=city,
                country_code=country,
                outbound_date=latest_alert.outbound_date,
                return_date=latest_alert.return_date,
                price_usd=float(latest_alert.alerted_price_usd),
                airlines=airlines_list,
                layovers=int(getattr(latest_alert, "layovers", 0) or 0),
                source=latest_alert.source,
                booking_url=url,
            ),
            average_price_usd=float(latest_alert.average_price_usd),
            discount_percent=float(latest_alert.discount_percent),
            baseline_source="Resent Latest Alert",
        )
    else:
        test_deal = QualifiedDeal(
            offer=FlightOffer(
                origin="SLC",
                destination="HND",
                destination_city="Tokyo",
                country_code="JP",
                outbound_date="2027-02-10",
                return_date="2027-02-17",
                price_usd=590.0,
                airlines=["Delta Air Lines"],
                layovers=1,
                source="Dashboard Test",
                booking_url="https://www.google.com/travel/flights",
            ),
            average_price_usd=1250.0,
            discount_percent=52.8,
            baseline_source="Test Baseline",
        )

    if notifier.send_deal_alert(test_deal):
        msg = urllib.parse.quote(f"Delivered latest deal alert individually to: {', '.join(recipients)}")
    else:
        msg = urllib.parse.quote("Failed to send email. Check 'docker logs flight_web' for SMTP error details.")
    return RedirectResponse(url=f"/?msg={msg}", status_code=303)


@app.post("/airports/add")
def add_airport(
    code: str = Form(...),
    city: str = Form(...),
    country_code: str = Form(...),
    slc_baseline: float = Form(800),
    las_baseline: float = Form(720),
):
    cfg = read_config()
    clean_code = code.strip().upper()
    clean_city = city.strip()
    clean_country = country_code.strip().upper()

    if not clean_code or len(clean_code) != 3:
        return RedirectResponse(url="/?msg=Invalid+3-letter+IATA+code", status_code=303)

    destinations = cfg.setdefault("monitored_destinations", [])
    updated = False
    for d in destinations:
        if str(d.get("code", "")).upper() == clean_code:
            d["city"] = clean_city
            d["country_code"] = clean_country
            d.pop("baseline_avg_USD", None)
            d["baseline_avg_usd"] = {"SLC": float(slc_baseline), "LAS": float(las_baseline)}
            updated = True
            break

    if not updated:
        destinations.append({
            "code": clean_code,
            "city": clean_city,
            "country_code": clean_country,
            "baseline_avg_usd": {"SLC": float(slc_baseline), "LAS": float(las_baseline)},
        })

    filters = cfg.setdefault("filters", {})
    allowed_dom = filters.setdefault("allowed_domestic_airports", [])
    if clean_country == "US" and clean_code not in allowed_dom:
        allowed_dom.append(clean_code)

    blocked = filters.get("blocked_destinations", [])
    filters["blocked_destinations"] = [
        b for b in blocked if str(b).strip().lower() not in {clean_code.lower(), clean_city.lower()}
    ]

    write_config(cfg)
    msg = urllib.parse.quote(f"Saved {clean_code} ({clean_city}) to monitored airports.")
    return RedirectResponse(url=f"/?msg={msg}", status_code=303)


@app.post("/airports/remove")
def remove_airport(code: str = Form(...)):
    cfg = read_config()
    clean_code = code.strip().upper()

    destinations = cfg.get("monitored_destinations", [])
    cfg["monitored_destinations"] = [
        d for d in destinations if str(d.get("code", "")).upper() != clean_code
    ]

    filters = cfg.get("filters", {})
    if "allowed_domestic_airports" in filters:
        filters["allowed_domestic_airports"] = [
            a for a in filters["allowed_domestic_airports"] if str(a).strip().upper() != clean_code
        ]

    write_config(cfg)
    msg = urllib.parse.quote(f"Removed {clean_code} from monitored airports.")
    return RedirectResponse(url=f"/?msg={msg}", status_code=303)


@app.post("/blocklist/add")
def add_blocklist(term: str = Form(...)):
    cfg = read_config()
    cleaned = term.strip()
    blocklist = cfg.setdefault("filters", {}).setdefault("blocked_destinations", [])
    if cleaned and cleaned not in blocklist:
        blocklist.append(cleaned)
        write_config(cfg)
    msg = urllib.parse.quote(f"Added '{cleaned}' to blocklist.")
    return RedirectResponse(url=f"/?msg={msg}", status_code=303)


@app.post("/blocklist/remove")
def remove_blocklist(term: str = Form(...)):
    cfg = read_config()
    cleaned = term.strip().lower()
    blocklist = cfg.get("filters", {}).get("blocked_destinations", [])
    cfg["filters"]["blocked_destinations"] = [
        x for x in blocklist if str(x).strip().lower() != cleaned
    ]
    write_config(cfg)
    msg = urllib.parse.quote(f"Removed '{term.strip()}' from blocklist.")
    return RedirectResponse(url=f"/?msg={msg}", status_code=303)


@app.post("/scan")
def trigger_manual_scan(background_tasks: BackgroundTasks):
    if scan_lock.locked():
        msg = urllib.parse.quote("A manual scan is already running in the background.")
        return RedirectResponse(url=f"/?msg={msg}", status_code=303)
    background_tasks.add_task(run_locked_manual_scan)
    msg = urllib.parse.quote("Manual background scan started! Refresh in 30-60 seconds to see new fares.")
    return RedirectResponse(url=f"/?msg={msg}", status_code=303)
