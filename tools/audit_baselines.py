"""Report every monitored historical reference without migrating or writing SQLite."""
from __future__ import annotations

import argparse
import csv
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
import sys

from db.crud import reference_ctes
from settings import validate_config


def report(database: Path, output=sys.stdout) -> None:
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    with sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        connection.execute("BEGIN")  # One consistent view while the worker writes.
        row = connection.execute("SELECT value_json FROM app_config WHERE id=1").fetchone()
        if row is None:
            raise ValueError("No active database configuration")
        config = validate_config(json.loads(row[0]))
        history = ("WITH history AS (SELECT * FROM price_history WHERE "
            "julianday(COALESCE(observed_at,recorded_at)) BETWEEN julianday(?) AND julianday(?) "
            "AND recorded_at<=? AND outbound_date>=? AND currency='USD' "
            "AND price_usd>0 AND price_usd<=1000000) ")
        parameters = ((now-timedelta(days=90)).isoformat(), now.isoformat(), now.isoformat(), now.date().isoformat())
        references = {(r["origin"], r["destination"]): dict(r) for r in connection.execute(
            history + reference_ctes() + "SELECT * FROM route_averages", parameters)}
        raw = {(r["origin"], r["destination"]): dict(r) for r in connection.execute(
            history + "SELECT origin,destination,COUNT(*) AS quotes,AVG(price_usd) AS raw_average "
            "FROM history GROUP BY origin,destination", parameters)}
        writer = csv.writer(output)
        writer.writerow(["origin", "destination", "raw_quotes", "date_day_samples", "observation_days",
            "travel_date_pairs", "raw_average_usd", "robust_reference_usd", "configured_reference_usd",
            "historical_eligible", "selected_reference_usd"])
        filters = config["filters"]
        for origin in config["scanner"]["origins"]:
            for destination in config["monitored_destinations"]:
                key = origin, destination["code"]
                stats, quotes = references.get(key, {}), raw.get(key, {})
                fallback = destination["baseline_avg_usd"].get(origin)
                eligible = (stats.get("samples", 0) >= filters["min_historical_samples_for_db_avg"]
                    and stats.get("observation_days", 0) >= filters["historical_min_observation_days"]
                    and stats.get("date_pairs", 0) >= filters["historical_min_date_pairs"])
                writer.writerow([*key, quotes.get("quotes", 0), stats.get("samples", 0),
                    stats.get("observation_days", 0), stats.get("date_pairs", 0),
                    quotes.get("raw_average", ""), stats.get("avg_price", ""), fallback or "", eligible,
                    stats.get("avg_price") if eligible else fallback or ""])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", required=True, type=Path, help="Existing SQLite file (read only)")
    report(parser.parse_args().database)


if __name__ == "__main__":
    main()
