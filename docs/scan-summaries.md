# Destination summaries and historical references

Searches collect their cheapest eligible offer for each origin, destination and
outbound/return date pair in SQLite. Delivery does not see these candidates until
all query work stops. At that point the worker evaluates the saved offers against
the current policy and a historical snapshot fixed before the scan's first
request. One transaction publishes at most one alert per destination and one
email per confirmed recipient for that destination. A durable batch identity
prevents duplicate publishing after restart or lease recovery.

The card highlights the cheapest qualifying fare across origins, then lists each
option's origin, dates, price and provider booking link. Freshness and revalidation
apply to every option. An unavailable option is removed and the best remaining
fare becomes the headline. A quota/deadline-limited scan may publish its completed
results; the email says that some dates could not be checked. Shutdown cancellation
does not publish a summary. An unexpected scan exception retains its candidates
for diagnosis but does not publish them automatically.

An email contains at most 100 options and stays below the outbox payload-size
limit. If bounded, its card reports how many qualifying options were found and
how many are shown; every candidate remains in the database until retention cleanup.
The optional delay setting waits the configured interval after summary creation.
Quiet hours and pausing continue to defer delivery.

## Repeated destination prices

Immediately before SMTP, the worker atomically checks the recipient's actual
sent live-deal payloads from the preceding seven days. A previous cheapest fare
less than or equal to the new destination minimum suppresses that destination,
even for different origins and travel dates. The refreshed, filtered payload is
persisted so later checks use the fare actually advertised. Weekly historical
recaps are excluded. The existing maximum two emails per destination per rolling
seven days and 72-hour spacing remain hard limits, including ambiguous SMTP sends.

The seven-day price comparison applies to legacy sent deals and live digests as
well as new summaries. The old per-itinerary cooldown/improvement settings remain
accepted for compatibility with older integrations, but new scan summaries use
destination-wide sent-price suppression and the fixed destination limits.

## Historical reference method

1. Consider positive, finite USD observations from the preceding 90 days for
   upcoming departures. Exclude future observation timestamps.
2. Take the minimum fare for each UTC observation day and outbound/return date
   pair, merging repeated queries, providers and expensive alternatives.
3. Take the median of those minimum fares within each day, then the median of
   daily medians. Each day has equal weight.
4. Require `historical_min_observation_days` (default 14),
   `historical_min_date_pairs` (default 30), and
   `min_historical_samples_for_db_avg` distinct date/day samples. Existing values
   of the last setting are preserved; 500 now means 500 deduplicated samples,
   rather than 500 raw returned fares.

The scan stores its cutoff timestamp, maximum history row ID and calculated
route references before requesting fares. Saved references survive history cleanup. Evaluation and delivery revalidation use that same snapshot,
so current-scan insertions cannot change the baseline or cross the coverage gate.
Current provider typical-price estimates retain priority when present; otherwise
the robust historical reference, then the labeled operator reference are used.
None of these references establishes future availability or a guaranteed market
price. The dashboard uses the same historical method with the current cutoff;
it requires coverage and does not use operator/provider fallbacks for its cards.

## Upgrade and inspect every monitored route

Follow the existing backup and upgrade procedure. Schema 6 adds scan batches,
candidates and indexes. It preserves price history, configuration, subscribers,
sent delivery records and destination allowances. It cancels previously pending
or retrying live per-itinerary notifications with `superseded_by_scan_summary`;
the next scan supplies fresh destination summaries. Invitations, status messages
and weekly recaps are preserved. Do not run an older image against schema 6;
rollback requires the compatible pre-upgrade backup.

Existing SQLite settings remain authoritative. The new coverage settings acquire
their defaults during validation; importing YAML is not required. Do not overwrite
your existing destination references or other settings just to add the new keys.

After building the updated image, inspect all configured routes without triggering
a scan, consuming provider quota or writing the database:

```bash
docker compose exec flight_worker python -m tools.audit_baselines --database /app/data/flight_deals.db
```

The CSV reports raw quote count/average, deduplicated sample and coverage counts,
robust reference, configured reference, historical eligibility and the selected
historical-or-configured reference. It reads one consistent SQLite snapshot in
read-only mode and excludes subscriber data. It can also inspect a schema-5 copy
before migration. Provider typical-price estimates are per-offer and cannot be
reconstructed from historical rows. Review reference changes before unpausing
delivery; routes with insufficient independent coverage may fall back longer.
