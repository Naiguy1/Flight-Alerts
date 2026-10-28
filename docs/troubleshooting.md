# Troubleshooting

Run commands from the `Flight-Alerts` project directory on the hosting server.
If the installation already has data, make a [backup](operations.md#back-up-a-docker-installation)
before making repairs that change files, settings, images or database state.

## Start with these two checks

```bash
docker compose ps --all
docker compose logs --tail=100 migrate flight_web flight_worker proxy
```

`migrate` finishing with exit code 0 is normal. The web and worker should become
healthy; the proxy should keep running. Keep logs and screenshots private and
remove credentials, email addresses and signed subscription URLs before sharing
a problem report. Do not paste `.env` or secret-file contents into an issue.

## Installation and website problems

| Symptom | What to check |
|---|---|
| `docker: command not found` or `docker compose` is missing | Install Docker Engine and its Compose plugin; see the setup prerequisites. |
| Cannot connect to the Docker daemon | Confirm Docker is running and your account is authorized to use it. Docker access is administrative access. |
| Compose asks for `PUBLIC_HOST`, `PUBLIC_BASE_URL`, or `ACME_EMAIL` | Use the project directory, create `.env` from `.env.example`, and fill in real values. `docker compose config --quiet` should succeed. |
| Ports 80/443 are already in use | Another web server or proxy may be listening. Have the host administrator resolve the conflict; do not expose internal port 8000 as a workaround. |
| Docker subnet overlaps another network | Review the fixed subnet/proxy-IP instructions in the deployment guide before changing the network. |
| Secret file permission denied | Files should be mode 0600 and owned by UID/GID 10001 for this Compose setup. Use an administrator to inspect/repair named files; do not make them world-readable. |
| Secret directory cannot be listed from your normal shell | That is expected after the initialization tool gives the private directory to the container's user. Use `sudoedit` for specific files. |
| Migration exits with an error | Check the migration log, database-volume permissions and source version. Preserve the existing database; do not delete it to make startup pass. |
| HTTPS certificate fails | Check real DNS, any A/AAAA records, certificate-authority reachability, firewall/router forwarding and the proxy log. `example.com` placeholders will not work. |
| `HTTPS required` or `Invalid host` | Use the configured HTTPS hostname through Caddy. `PUBLIC_HOST` and `PUBLIC_BASE_URL` must match. Direct access to port 8000 or a different hostname is unsupported. |
| Browser asks for credentials repeatedly | Use `ADMIN_USERNAME` and the administrator password from initialization, not your SMTP password or API key. The initialization tool does not overwrite an existing hash when rerun. |
| `Invalid form token`, origin rejection or settings conflict | Reload the page, use the exact configured HTTPS origin, and retry with one current dashboard tab. Avoid resubmitting an old form. |
| Too many authentication attempts | Wait at least a minute before trying again; do not repeatedly submit guesses. |

The two built-in health probes can be run explicitly:

```bash
docker compose exec flight_web python -m tools.healthcheck web
docker compose exec flight_worker python -m tools.healthcheck worker
```

They normally print nothing; exit code 0 means the probe passed. A healthy
container does not prove that a provider is returning prices or mail is arriving.

## Scans and fare results

| Symptom / safe error | Meaning and next step |
|---|---|
| `providers_not_configured` | Configure at least one fare-provider key in its private file, then recreate the worker. Aviationstack alone does not provide fare searches. |
| `authentication_failed`, `authentication`, `permission_denied`, `account_access` | Check the key and the purchased plan's endpoint permissions privately. A key's existence is not proof of access. |
| `provider_quota_exhausted`, `budget_exhausted`, `quota_exhausted`, `rate_limited` | Review the provider's allowance, application caps and current usage. Stop repeated manual scans; waiting or reducing coverage may be necessary. |
| `scan_deadline_exceeded` or incomplete coverage | Review provider failures and search size. Use fewer routes/date samples and appropriate quotas; a partial result is not full successful coverage. |
| A scan is already queued/running or in cooldown | Let the worker finish and allow the cooldown to expire. Inspect job history instead of repeatedly clicking Queue a scan. |
| Fresh fares is empty | Check active destinations, a recent scan, provider success, future departure dates and freshness. A valid search may return no offers. |
| Fares appear but no alert exists | The price may not meet the discount threshold, route rules or reference requirements, alerts may be paused, or the cooldown may apply. |
| Edited YAML has no effect | Settings were already seeded into SQLite. Use dashboard controls; changing the bootstrap file or rebuilding does not overwrite saved settings. |

## Email problems

| Symptom / delivery state | Meaning and next step |
|---|---|
| Recipient says Awaiting confirmation | Check the invitation, including spam. Use Resend invitation if the 48-hour link expired. |
| Recipient says Disabled | The address was disabled or unsubscribed. The dashboard will not override it with an old confirmation link. |
| `smtp_not_configured` | Check the SMTP host and authorized sender; configure matching username/password if required. |
| `smtp_authentication_failed` | Verify the mail login and any required app password with the SMTP provider. |
| `smtp_tls_failed` | Check the SMTP hostname, TLS port and certificate support. Do not disable certificate verification. |
| `pending`, `retry` or `sending` | The worker may be processing or waiting for a retry, quota, quiet window or digest interval. Inspect attempts and the safe error category. |
| `failed` | A permanent error or retry limit was reached. Correct the cause and review the recipient's state; failed rows do not automatically restart forever. |
| `fare_expired`, `fare_unavailable` or `status_expired` | The queued content became stale or could not be confirmed. Cancellation is expected; look for a new eligible result. |
| `sent` but no inbox message | SMTP accepted the message. Check spam/quarantine, sender authorization and your mail provider's delivery information. |
| Duplicate mail after a crash/restore | SMTP acceptance may have been uncertain. Review recovered pending messages; exactly-once mail is not guaranteed. |
| Latest-alert resend reports no recent alert | It resends an existing real reserved alert; it does not generate a synthetic test fare. Test invitations with your own mailbox first. |

## Apply connection or credential changes

After editing a provider/SMTP file or relevant `.env` setting, recreate the
worker so it reads the new configuration:

```bash
docker compose up -d --force-recreate flight_worker
```

For hostname changes, coordinate DNS/certificates and recreate the affected web,
proxy and worker services. For administrator/signing-key rotation, follow
[credential maintenance](operations.md#credentials-and-incident-response): changing
the signing key invalidates existing links. A plain restart does not apply all
Compose environment changes. Do not rerun first-time setup against a live database
as a way to reset it.

## Ask for help with a useful report

Include the Git SHA (`git rev-parse HEAD`), Docker/Compose versions, the service
that failed, the safe error category, and sanitized log excerpts. State whether
this is a new install or an upgrade and which guide step you reached. Keep the
private database, recipient addresses, passwords, keys and signed links out of
public reports.
