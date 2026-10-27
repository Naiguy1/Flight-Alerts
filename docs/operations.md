# Operations

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
