import argparse
import logging
import os
import random
import time
from datetime import datetime, timedelta, timezone
from typing import Dict, List
import yaml
from apscheduler.schedulers.blocking import BlockingScheduler
from dotenv import load_dotenv

from analyzer.evaluator import DealEvaluator
from db.crud import DatabaseManager
from fetcher.base import FlightFetcher, FlightOffer
from fetcher.fast_flights_scraper import FastFlightsScraper
from fetcher.google_flights import GoogleFlightsFetcher
from fetcher.secondary_aggregator import KiwiTequilaFetcher
from notifier.emailer import EmailNotifier

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("flight_alerter")


def load_config(path: str = "config.yaml") -> dict:
    with open(path, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def build_route_baselines(destinations_cfg: List[dict]) -> Dict[str, Dict[str, float]]:
    baselines: Dict[str, Dict[str, float]] = {}
    for item in destinations_cfg:
        code = str(item.get("code", "")).upper()
        base_dict = item.get("baseline_avg_usd") or item.get("baseline_avg_USD") or {}
        baselines[code] = {k.upper(): float(v) for k, v in base_dict.items()}
    return baselines


def sync_evaluator_with_config(evaluator: DealEvaluator, cfg_path: str) -> dict:
    config = load_config(cfg_path)
    filters_cfg = config.get("filters", {})
    evaluator.discount_threshold_percent = float(filters_cfg.get("discount_threshold_percent", 50.0))
    evaluator.blocked_terms = {
        str(item).strip().lower() for item in filters_cfg.get("blocked_destinations", [])
    }
    evaluator.allowed_domestic_airports = {
        str(a).strip().upper() for a in filters_cfg.get("allowed_domestic_airports", [])
    }
    evaluator.route_baselines = build_route_baselines(config.get("monitored_destinations", []))
    return config


def run_scan_cycle(
    config: dict,
    fetchers: List[FlightFetcher],
    evaluator: DealEvaluator,
    notifier: EmailNotifier,
    db: DatabaseManager,
    dry_run: bool = False,
) -> None:
    cfg_path = os.getenv("CONFIG_PATH", "config.yaml")
    config = sync_evaluator_with_config(evaluator, cfg_path)

    scanner_cfg = config["scanner"]
    origins = scanner_cfg.get("origins", ["SLC", "LAS"])
    destinations = config.get("monitored_destinations", [])
    offsets = scanner_cfg["search_window"].get("sample_departure_offsets_days", [35, 65, 95, 130, 165])
    trip_duration = int(scanner_cfg["search_window"].get("trip_duration_days", 7))
    base_delay = float(scanner_cfg.get("request_delay_seconds", 2.5))

    today = datetime.now(timezone.utc).date()
    logger.info(
        "Starting scan cycle across %d origins, %d destinations, and %d date windows (Threshold: %.0f%% off).",
        len(origins), len(destinations), len(offsets), evaluator.discount_threshold_percent,
    )

    alerts_triggered = 0

    for origin in origins:
        for dest in destinations:
            # Re-sync config before each airport so live dashboard edits apply immediately
            live_cfg = sync_evaluator_with_config(evaluator, cfg_path)
            live_codes = {str(d.get("code", "")).upper() for d in live_cfg.get("monitored_destinations", [])}

            dest_code = str(dest.get("code", "")).upper()
            dest_city = str(dest.get("city", ""))
            country_code = str(dest.get("country_code", "")).upper()

            if dest_code not in live_codes:
                logger.info("Skipping %s->%s (removed via dashboard)", origin, dest_code)
                continue

            if dest_code.lower() in evaluator.blocked_terms or dest_city.strip().lower() in evaluator.blocked_terms:
                logger.info("Skipping %s->%s (%s is in blocklist)", origin, dest_code, dest_city)
                continue

            logger.info("Scanning route %s -> %s (%s, %s)...", origin, dest_code, dest_city, country_code)

            for offset in offsets:
                outbound_dt = today + timedelta(days=int(offset))
                return_dt = outbound_dt + timedelta(days=trip_duration)
                outbound_str = outbound_dt.strftime("%Y-%m-%d")
                return_str = return_dt.strftime("%Y-%m-%d")

                aggregated_offers: List[FlightOffer] = []
                for fetcher in fetchers:
                    offers = fetcher.fetch_offers(
                        origin=origin,
                        destination=dest_code,
                        destination_city=dest_city,
                        country_code=country_code,
                        outbound_date=outbound_str,
                        return_date=return_str,
                    )
                    aggregated_offers.extend(offers)

                qualified_deals = evaluator.evaluate_offers(aggregated_offers)

                for deal in qualified_deals:
                    logger.info(
                        "QUALIFIED DEAL: %s->%s (%s to %s) at $%.0f vs $%.0f avg (-%.1f%%)",
                        deal.offer.origin,
                        deal.offer.destination,
                        deal.offer.outbound_date,
                        deal.offer.return_date,
                        deal.offer.price_usd,
                        deal.average_price_usd,
                        deal.discount_percent,
                    )
                    if not dry_run and notifier.send_deal_alert(deal):
                        db.log_alert(
                            offer=deal.offer,
                            average_price=deal.average_price_usd,
                            discount_percent=deal.discount_percent,
                        )
                        alerts_triggered += 1

                jitter = random.uniform(0.5, 1.5)
                time.sleep(base_delay + jitter)

    logger.info("Scan cycle complete. Total alerts dispatched: %d", alerts_triggered)


def main() -> None:
    parser = argparse.ArgumentParser(description="SLC & LAS Flight Deal Alerter")
    parser.add_argument("--config", default="config.yaml", help="Path to YAML configuration file")
    parser.add_argument("--run-once", action="store_true", help="Execute a single scan pass and exit")
    parser.add_argument("--dry-run", action="store_true", help="Scan and evaluate without sending emails")
    args = parser.parse_args()

    load_dotenv()
    config = load_config(args.config)

    db_url = os.getenv("DATABASE_URL", "sqlite:////app/data/flight_deals.db")
    db = DatabaseManager(db_url)

    currency = config["scanner"].get("currency", "USD")
    fetchers: List[FlightFetcher] = [
        FastFlightsScraper(currency=currency),
        GoogleFlightsFetcher(api_key=os.getenv("SERPAPI_API_KEY", ""), currency=currency),
        KiwiTequilaFetcher(api_key=os.getenv("KIWI_API_KEY", ""), currency=currency),
    ]

    filters_cfg = config.get("filters", {})
    evaluator = DealEvaluator(
        db=db,
        discount_threshold_percent=float(filters_cfg.get("discount_threshold_percent", 50.0)),
        deduplication_window_hours=int(filters_cfg.get("deduplication_window_hours", 48)),
        min_historical_samples=int(filters_cfg.get("min_historical_samples_for_db_avg", 5)),
        excluded_country_codes=filters_cfg.get("excluded_country_codes"),
        allowed_domestic_airports=filters_cfg.get("allowed_domestic_airports"),
        blocked_destinations=filters_cfg.get("blocked_destinations"),
        route_baselines=build_route_baselines(config.get("monitored_destinations", [])),
    )

    notifier = EmailNotifier(
        smtp_host=os.getenv("SMTP_HOST", "smtp.gmail.com"),
        smtp_port=int(os.getenv("SMTP_PORT", "587")),
        smtp_username=os.getenv("SMTP_USERNAME", ""),
        smtp_password=os.getenv("SMTP_PASSWORD", ""),
        sender_email=os.getenv("ALERT_SENDER_EMAIL", ""),
        recipient_email=os.getenv("ALERT_RECIPIENT_EMAIL", ""),
    )

    if args.run_once:
        run_scan_cycle(config, fetchers, evaluator, notifier, db, dry_run=args.dry_run)
        return

    interval_hours = int(config["scanner"].get("scan_interval_hours", 3))
    scheduler = BlockingScheduler()
    scheduler.add_job(
        run_scan_cycle,
        trigger="interval",
        hours=interval_hours,
        jitter=300,
        args=[config, fetchers, evaluator, notifier, db, args.dry_run],
        next_run_time=datetime.now(timezone.utc),
    )

    logger.info("Scheduler initialized (every %d hours).", interval_hours)
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("Shutting down flight alert scheduler.")


if __name__ == "__main__":
    main()
