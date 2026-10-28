# Flight-Alerts

Flight-Alerts watches flight prices and emails you when a fare meets your deal
settings. You run it on your own Linux computer or server and manage it through
a password-protected website.

It finds fares; it does not buy tickets or hold a price. You still book with the
flight provider. Searches use USD, and provider access may require a paid plan.

## Start here

| What you want to do | Read this |
|---|---|
| Install it for the first time | [Beginner setup guide](docs/getting-started.md) |
| Learn the dashboard and receive your first alert | [Using Flight-Alerts](docs/using-flight-alerts.md) |
| Fix a setup, login, scan, or email problem | [Troubleshooting](docs/troubleshooting.md) |
| Back up, stop, or update an existing installation | [Operations](docs/operations.md) |
| Move an older installation to this version | [Deployment and migration](docs/deployment.md#preserve-existing-deployments) |

The first-time guide uses Docker, so you do not need to install Python on the
server. It walks through the hostname, private credentials, a small starter
configuration, startup checks, and your first confirmed email subscription.
**An existing installation should use the upgrade instructions instead.**

## What you need

- One Linux server with a 64-bit Intel/AMD processor (`uname -m` shows `x86_64`).
- Git, Docker Engine, and the Docker Compose plugin (`docker compose`).
- A real hostname pointing to that server, with a plan for HTTPS and ports 80/443.
- At least one supported fare-provider account for searches, and an SMTP account
  for sending mail. API keys and mail credentials stay in private files.
- Local disk space for the database and a separate backup plan.

This release supports one administrator and one server. The database is SQLite,
which stores the project's settings and history in a local persistent volume.
Shared network storage and multiple database hosts are not supported.

## How it works

| Part | What it does |
|---|---|
| Dashboard (`flight_web`) | Lets the administrator change settings and inspect results |
| Worker (`flight_worker`) | Searches providers and sends queued messages |
| Database volume (`flight_data`) | Keeps settings, subscriptions, prices, and delivery history across restarts |
| HTTPS proxy (`proxy`) | Provides the secure website and certificate |
| Setup job (`migrate`) | Prepares or upgrades the database before the other services start |

Recipients must accept their invitation before receiving fare or flight-status
alerts. Quiet hours delay delivery; digests group fare alerts. A scan with no
qualifying deal can finish successfully without sending an email.

## Current project status

[PR #7](https://github.com/Naiguy1/Flight-Alerts/pull/7) has merged into `main` at
`82537e8a53390b08b230eaf721ee5b62664a9d53`. Install from `main`; you do not need to
merge the old `production-hardening` branch yourself. The
[release record](docs/production-readiness.md#release-record) tracks automated
checks and the host/provider checks still needed before production use.
A merged pull request does not start or upgrade your server.

## For developers

Use CPython 3.12 on Linux amd64 for the supported dependency locks. These
commands test the code without real flight-provider or SMTP credentials:

```bash
python3.12 -m venv .venv
. .venv/bin/activate
python -m pip install --require-hashes -r requirements-dev.txt
python -m pytest -q
python -m ruff check .
python -m tools.verify_lock
python -m tools.backup self-test
```

The production image has no package installer. Change dependencies through a
reviewed lock update and rebuild the image. See [supply-chain verification](docs/supply-chain.md)
for lock generation, image scanning, and SBOM details. The required automated
checks are **Tests, Security, Image, and CodeQL**.

## Further reading

- [Deployment details and upgrades](docs/deployment.md)
- [Backups, monitoring, and recovery](docs/operations.md)
- [Architecture and limitations](docs/architecture.md)
- [Production readiness](docs/production-readiness.md)
- [Maintainer merge procedure](docs/merging.md)
- [Security policy](SECURITY.md)

SMTP can occasionally produce a duplicate when a server accepts a message but
its acknowledgment is lost. Delivery state survives restarts, but exactly-once
email is not guaranteed. Provider coverage, quotas, and data rights depend on
your account; successful software tests do not verify those external services.
