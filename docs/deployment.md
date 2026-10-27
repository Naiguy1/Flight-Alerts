# Deployment and migration

## Supported topology

Use one Linux amd64 host, CPython 3.12, and a local Docker named volume. Do not
mount SQLite through NFS or share it across hosts. The supplied Compose services
are a serialized migration job, one worker, one administrator web app, and a
Caddy HTTPS proxy. Only Caddy publishes ports. Application containers run as UID
10001 without Linux capabilities, with read-only image filesystems and bounded
memory/CPU/process counts. Temporary storage and the database volume are the
explicit writable locations.

The `app` network is internal and dedicated to this deployment. Caddy has fixed
address `172.30.80.2`, the only address Uvicorn trusts for forwarded headers.
Caddy overwrites the protocol/host headers and removes `Forwarded`. Check for a
host network/subnet conflict before deploying. If changing this subnet, change
Caddy's fixed address and `--forwarded-allow-ips` together. Never replace that
allowlist with `*`. Do not attach unrelated application networks.

Caddy binds unprivileged internal ports 8080/8443, mapped to host 80/443. Its
persistent data/config paths contain certificates and need host encryption and
restricted administrative access. Ensure DNS A/AAAA records point at the host,
that only intended ingress ports are allowed, and that the host can reach the
certificate authority. Use a real hostname and administrator-owned ACME email.

## Initial setup

```bash
cp .env.example .env
chmod 600 .env
python3.12 -m venv .venv
. .venv/bin/activate
python -m pip install --require-hashes -r requirements.txt
sudo .venv/bin/python -m tools.init_secrets --uid 10001 --gid 10001
```

Edit `.env` to set `PUBLIC_HOST`, matching `PUBLIC_BASE_URL=https://...`,
`ACME_EMAIL`, and `ALERT_SENDER_EMAIL`. Provider/SMTP values live in the named
files under `secrets/`; use a private editor and keep files mode 0600 owned by
10001:10001. Empty provider files disable those providers. Configure only keys
your account is entitled to use and set budgets below purchased quotas.

Compose file secrets are read-only mounts, not encrypted secret storage. With
local Compose, configured secret `uid`/`gid` overrides cannot be relied on for
bind-mounted files; correct host ownership is required. The web container gets
only its password hash and shared signing secret. Provider/SMTP files are
mounted exclusively in the worker. Never use `env_file: .env` to inject every
credential into every service.

```bash
docker compose config --quiet
docker compose build --pull
docker compose up -d
docker compose ps
```

Run the host on encrypted storage and restrict the Docker socket/group to
administrators: Docker control can read every container secret and volume. Do
not publish the Uvicorn port. Visit the dashboard via HTTPS and verify that an
anonymous request is rejected. Invite recipients and confirm their subscription
links before enabling alerts. Set quiet hours, alert cooldown/improvement rules,
and conservative provider budgets.

The optional scraper requires `INSTALL_SCRAPER=true` at build time and
`ENABLE_SCRAPER=true` at runtime, followed by image rebuilding. It is isolated by
an application deadline and uses its own locked environment; review provider
terms/availability before enabling. Flight-status coverage depends on the
Aviationstack account's plan and supported dates. No API key means no status
polling.

## Preserve existing deployments

Before changing an existing installation, stop its worker/web processes and
create a verified database backup with the old database path. Keep the previous
image and a private copy of existing settings/secrets. Existing SQLAlchemy-era
SQLite tables are preserved and upgraded by the versioned database migrations.
The application now explicitly supports SQLite only.

Import the verified backup into the new named volume **before** starting the
migration job. For a file mounted inside a one-off app container:

```bash
docker compose run --rm --no-deps -v /private/backups:/backups:ro migrate \
  python -m tools.backup restore --source /backups/pre-upgrade.db \
  --destination /app/data/flight_deals.db --confirm-services-stopped
```

The mount directory/file must be readable by UID 10001 and inaccessible to
unrelated users. Restore refuses an existing target or WAL/SHM sidecars.

If migrating private YAML settings, mount your private config read-only at
`/app/config.yaml` for the first migration invocation. Do not replace the
placeholder config in Git with private addresses. Settings seed once into the
database; later YAML edits do not silently overwrite dashboard changes. Review
recipient state after migration and confirm invitations before delivery.

```bash
docker compose run --rm --no-deps -v /private/config.yaml:/app/config.yaml:ro migrate
docker compose up -d
```

## Upgrade and rollback

1. Record the deployed Git SHA and image ID/digest. Complete CI and inspect package/image scan results.
2. Back up the live database using the SQLite backup command and verify the backup. Stop worker/web before an offline restore or schema rollback.
3. Build the new image, run the migration service once, then start worker/web/proxy.
4. Check readiness, worker heartbeat, provider coverage, and a controlled recipient delivery. Confirm no secrets appear in logs.
5. If rollback is needed, stop services. Restore the pre-upgrade backup into a new target with no WAL/SHM sidecars, and start the recorded previous image. Do not run old application code against a schema it does not understand.

Do not enable public access until `production-readiness.md` external checks are
recorded against the deployed SHA. The code change does not provision your DNS,
certificates, firewall, encrypted host storage, GitHub settings, or paid API plan.
