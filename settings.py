"""Validated configuration and runtime secrets; mutable settings live in SQLite."""

from __future__ import annotations

import copy
import math
import os
import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml


def _number(value, name: str, low: float, high: float, *, integer=False):
    if isinstance(value, bool):
        raise ValueError(f"{name} must be numeric")
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        raise ValueError(f"{name} must be numeric") from None
    if not math.isfinite(result) or not low <= result <= high:
        raise ValueError(f"{name} is outside its permitted range")
    if integer and result != int(result):
        raise ValueError(f"{name} must be an integer")
    return int(result) if integer else result


def _boolean(value, name: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{name} must be a boolean")
    return value


def airport_code(value) -> str:
    result = str(value).strip().upper()
    if not re.fullmatch(r"[A-Z]{3}", result):
        raise ValueError("Airport code must contain three letters")
    return result


def validate_config(value: dict) -> dict:
    """Return a normalized copy; reject invalid data before any state is changed."""
    if not isinstance(value, dict):
        raise ValueError("Configuration must be an object")
    cfg = copy.deepcopy(value)
    for name in ("scanner", "filters"):
        if name in cfg and not isinstance(cfg[name], dict):
            raise ValueError(f"{name} must be an object")
    scanner = cfg.setdefault("scanner", {})
    origins = scanner.get("origins", ["SLC", "LAS"])
    if not isinstance(origins, list) or not 1 <= len(origins) <= 10:
        raise ValueError("Configure between one and ten origins")
    scanner["origins"] = list(dict.fromkeys(airport_code(code) for code in origins))
    if scanner.get("currency", "USD") != "USD":
        raise ValueError("Only USD is supported until currency conversion is implemented")
    scanner["currency"] = "USD"
    for name, default, low, high, integer in (
        ("scan_interval_hours", 6, 1, 168, True),
        ("request_delay_seconds", 2.5, 0, 60, False),
        ("provider_requests_per_minute", 10, 1, 120, True),
        ("provider_daily_budget", 1000, 1, 100000, True),
        ("concurrency", 3, 1, 8, True),
        ("request_timeout_seconds", 20, 1, 60, True),
        ("scan_deadline_seconds", 7200, 30, 21600, True),
    ):
        scanner[name] = _number(scanner.get(name, default), name, low, high, integer=integer)
    window = scanner.setdefault("search_window", {})
    if not isinstance(window, dict):
        raise ValueError("search_window must be an object")
    window["min_days_ahead"] = _number(window.get("min_days_ahead", 30), "min_days_ahead", 1, 365, integer=True)
    window["max_days_ahead"] = _number(window.get("max_days_ahead", 180), "max_days_ahead", window["min_days_ahead"], 365, integer=True)
    offsets = window.get("sample_departure_offsets_days", [35, 65, 95, 130, 165])
    if not isinstance(offsets, list) or not 1 <= len(offsets) <= 20:
        raise ValueError("Configure between one and twenty departure offsets")
    window["sample_departure_offsets_days"] = sorted(set(
        _number(item, "departure offset", window["min_days_ahead"], window["max_days_ahead"], integer=True)
        for item in offsets
    ))
    window["trip_duration_days"] = _number(window.get("trip_duration_days", 7), "trip_duration_days", 1, 60, integer=True)
    filters = cfg.setdefault("filters", {})
    for name, default, low, high, integer in (
        ("discount_threshold_percent", 50, 1, 99, False),
        ("deduplication_window_hours", 48, 1, 720, True),
        ("min_historical_samples_for_db_avg", 15, 2, 10000, True),
        ("price_improvement_usd", 35, 1, 1000, False),
        ("price_improvement_percent", 10, 1, 90, False),
        ("freshness_hours", 24, 1, 168, True),
        ("retention_days", 180, 30, 730, True),
        ("digest_interval_hours", 6, 1, 168, True),
    ):
        filters[name] = _number(filters.get(name, default), name, low, high, integer=integer)
    for name in ("alerts_paused", "digest_enabled"):
        filters[name] = _boolean(filters.get(name, False), name)
    if filters["digest_enabled"] and filters["digest_interval_hours"] >= filters["freshness_hours"]:
        raise ValueError("Digest interval must be shorter than fare freshness")
    countries = filters.get("excluded_country_codes", ["US"])
    if not isinstance(countries, list) or len(countries) > 250:
        raise ValueError("Invalid excluded country list")
    filters["excluded_country_codes"] = []
    for country in countries:
        code = str(country).strip().upper()
        if not re.fullmatch(r"[A-Z]{2}", code):
            raise ValueError("Countries must be two-letter codes")
        if code not in filters["excluded_country_codes"]:
            filters["excluded_country_codes"].append(code)
    allowed = filters.get("allowed_domestic_airports", [])
    if not isinstance(allowed, list) or len(allowed) > 200:
        raise ValueError("Invalid allowed airport list")
    filters["allowed_domestic_airports"] = list(dict.fromkeys(airport_code(code) for code in allowed))
    blocked = filters.get("blocked_destinations", [])
    if not isinstance(blocked, list) or len(blocked) > 200:
        raise ValueError("Invalid destination blocklist")
    terms = []
    for entry in blocked:
        term = str(entry).strip()
        if not term or len(term) > 100 or any(ord(c) < 32 for c in term):
            raise ValueError("Invalid blocklist entry")
        if term.lower() not in {item.lower() for item in terms}:
            terms.append(term)
    filters["blocked_destinations"] = terms
    recipients = filters.get("recipient_emails", [])
    if not isinstance(recipients, list) or len(recipients) > 100:
        raise ValueError("At most 100 recipient addresses may be imported")
    normalized = []
    for entry in recipients:
        from notifier.emailer import normalize_email
        address = normalize_email(entry)
        if address not in normalized:
            normalized.append(address)
    filters["recipient_emails"] = normalized
    quiet = filters.get("quiet_hours")
    if quiet is not None:
        if not isinstance(quiet, dict):
            raise ValueError("quiet_hours must be an object or null")
        start = _number(quiet.get("start", 22), "quiet hour start", 0, 23, integer=True)
        end = _number(quiet.get("end", 7), "quiet hour end", 0, 23, integer=True)
        zone = str(quiet.get("timezone", "America/Denver"))
        try:
            ZoneInfo(zone)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError("Invalid quiet-hours timezone") from None
        filters["quiet_hours"] = {"start": start, "end": end, "timezone": zone}
    destinations = cfg.setdefault("monitored_destinations", [])
    if not isinstance(destinations, list) or len(destinations) > 200:
        raise ValueError("At most 200 destinations may be monitored")
    seen = set()
    for destination in destinations:
        if not isinstance(destination, dict):
            raise ValueError("Each destination must be an object")
        code = airport_code(destination.get("code", ""))
        if code in seen:
            raise ValueError("Destination codes must be unique")
        seen.add(code)
        city = str(destination.get("city", "")).strip()
        country = str(destination.get("country_code", "")).strip().upper()
        if not city or len(city) > 100 or any(ord(c) < 32 for c in city):
            raise ValueError("Invalid destination city")
        if not re.fullmatch(r"[A-Z]{2}", country):
            raise ValueError("Invalid country code")
        baselines = destination.get("baseline_avg_usd", destination.get("baseline_avg_USD", {}))
        if not isinstance(baselines, dict) or len(baselines) > 10:
            raise ValueError("Invalid baseline mapping")
        destination.update(code=code, city=city, country_code=country)
        destination["baseline_avg_usd"] = {
            airport_code(origin): _number(price, "baseline", 1, 50000)
            for origin, price in baselines.items()
        }
        destination.pop("baseline_avg_USD", None)
    return cfg


def load_config(path: str = "config.yaml") -> dict:
    source = Path(path)
    if source.stat().st_size > 256 * 1024:
        raise ValueError("Configuration file is too large")
    with source.open(encoding="utf-8") as handle:
        return validate_config(yaml.safe_load(handle) or {})


def validate_watched_flight(flight_iata: str, flight_date: str) -> tuple[str, str]:
    code = str(flight_iata).strip().upper()
    if not re.fullmatch(r"[A-Z0-9]{2}[0-9]{1,4}[A-Z]?", code):
        raise ValueError("Invalid flight number")
    parsed = date.fromisoformat(flight_date)
    if parsed.isoformat() != flight_date:
        raise ValueError("Invalid flight date")
    return code, flight_date


def _secret(name: str, default: str = "") -> str:
    value = os.getenv(name)
    filename = os.getenv(f"{name}_FILE")
    if value is not None and filename:
        raise ValueError(f"Configure only one of {name} and {name}_FILE")
    if filename:
        path = Path(filename)
        if path.stat().st_size > 65536:
            raise ValueError(f"{name} secret file is too large")
        return path.read_text(encoding="utf-8").rstrip("\r\n")
    return value if value is not None else default


@dataclass(frozen=True)
class RuntimeSettings:
    database_url: str = "sqlite:///data/flight_deals.db"
    config_path: str = "config.yaml"
    public_base_url: str = "https://localhost"
    admin_username: str = "admin"
    admin_password_hash: str = field(default="", repr=False)
    token_secret: str = field(default="", repr=False)
    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = 587
    smtp_username: str = field(default="", repr=False)
    smtp_password: str = field(default="", repr=False)
    sender_email: str = ""
    serpapi_api_key: str = field(default="", repr=False)
    kiwi_api_key: str = field(default="", repr=False)
    aviationstack_api_key: str = field(default="", repr=False)
    enable_scraper: bool = False

    @classmethod
    def from_env(cls) -> "RuntimeSettings":
        from web.security import canonical_https_origin
        base_url = canonical_https_origin(os.getenv("PUBLIC_BASE_URL", "https://localhost"))
        database_url = os.getenv("DATABASE_URL", "sqlite:///data/flight_deals.db")
        if not database_url.startswith("sqlite:///"):
            raise ValueError("Only single-host SQLite databases are supported")
        enabled = os.getenv("ENABLE_SCRAPER", "false").lower()
        if enabled not in {"true", "false"}:
            raise ValueError("ENABLE_SCRAPER must be true or false")
        return cls(
            database_url=database_url,
            config_path=os.getenv("CONFIG_PATH", "config.yaml"),
            public_base_url=base_url,
            admin_username=os.getenv("ADMIN_USERNAME", "admin"),
            admin_password_hash=_secret("ADMIN_PASSWORD_HASH"),
            token_secret=_secret("TOKEN_SECRET"),
            smtp_host=os.getenv("SMTP_HOST", "smtp.gmail.com"),
            smtp_port=_number(os.getenv("SMTP_PORT", "587"), "SMTP_PORT", 1, 65535, integer=True),
            smtp_username=_secret("SMTP_USERNAME"),
            smtp_password=_secret("SMTP_PASSWORD"),
            sender_email=os.getenv("ALERT_SENDER_EMAIL", ""),
            serpapi_api_key=_secret("SERPAPI_API_KEY"),
            kiwi_api_key=_secret("KIWI_API_KEY"),
            aviationstack_api_key=_secret("AVIATIONSTACK_API_KEY"),
            enable_scraper=enabled == "true",
        )
