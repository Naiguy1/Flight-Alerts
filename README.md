# Flight-Alerts

A single-host flight-deal monitor with a protected administrator dashboard,
provider quota controls, persistent scan jobs, verified email subscriptions,
per-recipient delivery retries, quiet hours, digests, and optional flight-status
change alerts.

The application uses Python 3.12, FastAPI, server-rendered escaped templates, and
local SQLite with versioned migrations. SQLite is supported on one host with
local persistent storage. PostgreSQL and shared multi-host database volumes are
not supported by this release.

## Start with a private deployment

1. Read [the deployment and migration runbook](docs/deployment.md).
2. Copy `.env.example` to `.env` and set the public HTTPS hostname and sender.
3. Create private secrets with `sudo python -m tools.init_secrets --uid 10001 --gid 10001` after installing the locked dependencies. Use a strong administrator password. Keep the generated secret files outside Git and image layers.
4. Configure provider and SMTP secret files privately. A provider without a key is disabled; the optional scraper is disabled by default.
5. Build and start with `docker compose up --build -d`. The migration service completes before web/worker startup. Caddy is the only public entry point.
6. Sign in over HTTPS, invite recipients, and have each confirm their subscription. Queue a scan, inspect provider health, and confirm per-recipient delivery results.
7. Complete [production readiness](docs/production-readiness.md) before public use.

No real recipient address or production credential belongs in `config.yaml`.
That file seeds settings once; subsequent dashboard edits are transactional and
stored in the database. An explicitly empty recipient list disables deliveries.

## Development and checks

```bash
python3.12 -m venv .venv
. .venv/bin/activate
python -m pip install --require-hashes -r requirements-dev.txt
python -m pytest -q
python -m ruff check .
python -m tools.verify_lock
python -m tools.backup self-test
```

The hash locks target CPython 3.12 on Linux amd64. Regenerate them on that target
using `python -m tools.lock_dependencies --scraper`, review the diff, then rerun
the complete CI/security checks. Production installation uses
`--require-hashes`; dependency changes require review. CI's lock-resolution
workflow can generate the files on an isolated GitHub runner. It runs with read-only repository permissions and uploads generated locks for
review; a maintainer commits those files after assessing the dependency diff.

CI runs regressions, lint, package auditing, full-history secret scanning,
CodeQL, a container build/vulnerability scan, and an image SBOM. CI success does
not verify deployment DNS, issued certificates, host encryption, provider plan
entitlements, or an actual production-data restore.

## Operations and behavior

- [Deployment, secrets, upgrade, rollback](docs/deployment.md)
- [Backup, restore, and incident response](docs/operations.md)
- [Architecture and delivery guarantees](docs/architecture.md)
- [Production readiness and external verification](docs/production-readiness.md)
- [Security policy](SECURITY.md)

Provider failures and searches with no offers are tracked separately. Booking
links are restricted to approved HTTPS hosts; discovered fares can expire and
are not reservations. Status integration requires an eligible Aviationstack
account and its supported flight/date coverage; it is disabled without a key.

SMTP delivery has an unavoidable uncertain-acknowledgment case. Persistent
leases and per-recipient states recover work, but a crash after SMTP acceptance
may result in a duplicate. This release provides recoverable, at-least-once
processing rather than an exactly-once email guarantee.
