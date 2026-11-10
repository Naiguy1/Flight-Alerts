# Flight-Alerts

Flight-Alerts watches flight prices and emails verified subscribers when fares
meet your configured deal threshold. It runs on one host with a password-protected
HTTPS dashboard, a worker, and a persistent SQLite database.

Each subscriber receives at most two live alert emails per destination in any
rolling seven days, spaced at least 72 hours apart, across all origins and travel
dates. Price improvements do not bypass this limit. Live digests count once for
each destination they contain. The optional weekly recap is separate and keeps
the best observed prices even when individual alerts are suppressed.

The allowance survives restarts and upgrades, which count previously sent
alerts. Suppressed pending fares are canceled; later scans can supply fresh
offers after the cooldown. An uncertain SMTP acknowledgment also spends a slot
to prevent duplicate retries; definite rejection releases it.

## Set up or upgrade

- **New installation:** follow the [beginner setup guide](docs/getting-started.md).
  It covers the supported Linux amd64 host, DNS/TLS, `.env`, mounted secrets,
  provider credentials, and `docker compose up -d --build`.
- **Existing installation:** use the [upgrade guide](docs/deployment.md#preserve-existing-deployments)
  to preserve your database and verified subscribers.
- **Existing Caddy:** follow the [external Caddy guide](docs/existing-caddy.md).
- **Use the dashboard:** see [settings, subscriptions, and scans](docs/using-flight-alerts.md).

The repository already includes `docker-compose.yml`. The dashboard is served at
`PUBLIC_BASE_URL` over HTTPS. SMTP credentials and `SERPAPI_API_KEY` / `KIWI_API_KEY`
are mounted as secrets; the optional direct scraper requires both its build and
runtime flags. Configure an API fallback when enabling the scraper.

Mutable settings live in SQLite after the first startup. Startup warns if YAML
and SQLite differ. Review existing dashboard settings before intentionally
importing YAML with `docker compose exec flight_worker python -m tools.sync_config`.

## Validation and launch

Use Python 3.12 with the locked development environment:

```bash
python -m pip install --require-hashes -r requirements-dev.txt
python -m tools.verify_lock
python -m ruff check .
python -m pytest -q
python -m tools.backup self-test
python -m pip check
```

All final-commit CI gates and the production sign-offs in the
[report resolution and launch checklist](docs/report-resolution.md) must pass
before launch. Production credentials, TLS operation, and recovery of the real
production database require checks on the deployment host.

## Further reading

- [Weekly best deals newsletter](docs/weekly-newsletter.md)
- [Architecture and limitations](docs/architecture.md)
- [Production readiness](docs/production-readiness.md)
- [Operations and recovery](docs/operations.md)
- [Troubleshooting](docs/troubleshooting.md)
- [Maintainer merge procedure](docs/merging.md)
- [Security policy](SECURITY.md)
