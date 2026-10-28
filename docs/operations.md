# Operations

This page is for the person running the server. For dashboard controls, see
[Using Flight-Alerts](using-flight-alerts.md); for failures, see
[Troubleshooting](troubleshooting.md). Run Compose commands from the project folder.

## Everyday Docker commands

| Task | Command | What to expect |
|---|---|---|
| Check services | `docker compose ps --all` | Web/worker become healthy; proxy runs; migrate can be Exited (0) |
| Read recent logs | `docker compose logs --tail=100 flight_worker flight_web proxy` | Recent status/error messages; keep them private |
| Stop searches and email | `docker compose stop flight_worker` | Dashboard stays available; queued work remains saved |
| Resume the worker | `docker compose up -d flight_worker` | Saved work resumes under the usual consent, freshness and quota rules |
| Stop all services | `docker compose stop` | Containers stop; the database volume stays |
| Start existing services | `docker compose up -d` | Services start using existing saved data |

**Do not use `docker compose down -v` for a restart or troubleshooting.** The `-v`
option removes named volumes, including database and certificate storage. The
Docker database is not a host file at `data/flight_deals.db`.

## Back up a Docker installation

A backup is a separate, consistent copy of your database. Create one before an
upgrade and keep an encrypted copy away from this server. The local folder below
is private and ignored by Git; creating the folder does not encrypt it.

```bash
sudo install -d -m 700 -o 10001 -g 10001 backups
backup_name="flight-deals-$(date -u +%Y%m%dT%H%M%SZ).db"
docker compose run --rm --no-deps --volume "$PWD/backups:/backups" migrate \
  python -m tools.backup create --source /app/data/flight_deals.db \
  --destination "/backups/$backup_name"
docker compose run --rm --no-deps --volume "$PWD/backups:/backups:ro" migrate \
  python -m tools.backup verify --source "/backups/$backup_name"
```

Run these commands in the same terminal so `backup_name` stays set. Creation
prints a SHA256 checksum; verification should report success. Existing files are
never overwritten. The backup tool safely snapshots a running SQLite database;
copying just its live `.db` file can miss changes still in its journal.

Save the current source revision and image IDs in your private deployment notes:

```bash
git rev-parse HEAD
docker image inspect flight-alerts:local --format '{{.Id}}'
docker image inspect flight-alerts-proxy:local --format '{{.Id}}'
```

Also retain the compatible previous images and private `.env`/secret files;
those files are not part of the database backup. Arrange encrypted off-host
storage and a retention schedule with the operator. Test restoring a real backup
to a separate location that cannot send production mail or call providers.

For a synthetic check that never touches production data:

```bash
docker compose run --rm --no-deps migrate python -m tools.backup self-test
```

That checks backup mechanics only. It does not prove that your actual deployment
can be restored. Restoring a live installation requires stopping web/worker,
preserving the old database plus its journal sidecars, and restoring into an
empty target with the correct compatible image. Use the restore procedure below
with your operator; do not overwrite a running database.

## Update an existing installation

1. Read the new version's changes and [migration notes](deployment.md#preserve-existing-deployments).
   Check the four required CI results for the revision you plan to install.
2. Make and verify a backup as above. Retain the previous images, settings and
   secrets, and rehearse migration/rollback on an isolated copy.
3. Inspect local changes with `git status --short`. Preserve any private local
   configuration; do not discard changes or commit secrets to make an update work.
4. Stop the worker and web app, update source, build, migrate, then restart:

```bash
docker compose stop flight_worker flight_web
git switch main
git pull --ff-only origin main
docker compose config --quiet
docker compose build --pull
docker compose run --rm --no-deps migrate
docker compose up -d
docker compose ps --all
```

If a command fails, stop and inspect it before continuing. Verify HTTPS login,
worker/provider health and per-recipient delivery afterward. Keep the previous
image/backup until the observation period succeeds. A source-code rollback alone
does not roll back a migrated database.

The sections below provide more detail for operators. Their host-managed path
examples are not the location of the Docker named volume; use the Compose backup
commands above for the standard installation.

## Health and monitoring

`/health/live` checks process availability; `/health/ready` checks database
readiness without exposing paths or credentials. The worker writes a private
heartbeat at least every few seconds, including during scans. Its container
probe fails when that heartbeat is stale for 120 seconds. A fresh heartbeat does
not prove that external providers are succeeding: inspect last successful
provider data, route coverage, quota use, outbox age, and per-recipient failures.

Configure your monitoring system to alert on stale provider observations, absent
scan completions, rising 429/timeout rates, repeatedly failed recipients, old
pending deliveries, a failing container probe, disk exhaustion, and failed
backups. The repository does not create an external monitoring account or send
unsolicited operational messages.

Application and proxy access logs omit signed subscription URLs and raw provider
errors. Keep logs private with bounded retention. Never add raw exception text,
request query strings, provider response bodies, SMTP passwords, or recipient
addresses to routine logs. Keep authenticated audit histories accessible only
to the administrator.

## Consistent backup and restore

Use SQLite's backup API rather than copying only the main `.db` while WAL writes
are active. Choose an encrypted destination with mode 0700 directory and 0600
files. Example inside the worker container, using a private backup mount:

```bash
python -m tools.backup create --source /app/data/flight_deals.db \
  --destination /backups/flight-deals-YYYYMMDD.db
python -m tools.backup verify --source /backups/flight-deals-YYYYMMDD.db
```

The command prints a SHA256 and verifies integrity/foreign keys. Copy backups
off-host using encrypted transport and storage, restrict access, and define a
retention policy. To encrypt a host-created backup for an age recipient:

```bash
python -m tools.backup create --source data/flight_deals.db \
  --destination backups/flight-deals-YYYYMMDD.db.age --age-recipient age1YOUR_PUBLIC_RECIPIENT
```

This requires the `age` executable. The command creates a mode-0600 temporary
plaintext SQLite backup, encrypts it, and removes the temporary file. Host disk
encryption is still required; deletion is not guaranteed secure erasure on SSDs.
Keep the age private identity separate from the backup destination.

A restore drill uses a separate empty directory/database target. Decrypt to a
private location first if applicable, verify integrity, restore, then run the
current schema/application smoke tests against that copy:

```bash
python -m tools.backup self-test
python -m tools.backup restore --source backups/flight-deals-YYYYMMDD.db \
  --destination restore-check/flight_deals.db --confirm-services-stopped
```

`self-test` proves backup/restore mechanics on a temporary synthetic database. It
does not establish recoverability of your production backups. Record an actual
production-backup restore drill, including row counts, recipients/settings,
alert history, migration status, and the deployed application SHA. Ensure a
restored test worker cannot reach SMTP/provider endpoints.

For a real replacement, stop web/worker first, preserve the current database and
its `-wal`/`-shm` sidecars in a private quarantine directory, then restore into a
new target. The command refuses overwrites or existing sidecars. Bring services
back only after integrity/schema checks and review of any recovered pending
outbox messages, which may represent uncertain prior SMTP delivery.

## Credentials and incident response

If a provider/SMTP key appeared in a historical log or image, revoke it at the
provider, issue a replacement, update the private secret file, and recreate only
the affected service. Removing `.env` from a later image does not remove it from
older images. Restrict/remove exposed logs/images according to retention policy.
Do not rewrite public Git history without a separate reviewed migration plan.

Changing `TOKEN_SECRET` invalidates outstanding signed links and CSRF tokens.
Changing the administrator password hash revokes the old Basic credential.
Confirm subscription privacy and issue replacement confirmation/unsubscribe
links when rotating signing keys. Preserve sanitized incident metadata; avoid
copying secrets into tickets or public discussions.

## GitHub release controls

After the CI checks exist on the branch, apply the reviewed policy with a token
that has repository administration permission:

```bash
# Supply GITHUB_TOKEN securely in the environment; do not put it in shell history.
python -m tools.apply_branch_protection --repository Naiguy1/Flight-Alerts --apply
python -m tools.apply_branch_protection --repository Naiguy1/Flight-Alerts
```

The policy requires Tests, Security, Image, and CodeQL, an approving review,
stale-review dismissal, resolved conversations, linear history, and protection
for administrators. Enable GitHub secret scanning/push protection where available
and private vulnerability reporting. This repository's connector cannot apply
administrative GitHub settings; the script reports failure rather than claiming
success when access is unavailable.
