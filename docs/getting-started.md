# Beginner setup guide

This guide installs a **new** Flight-Alerts deployment with Docker. Run the
commands in a terminal on the Linux computer that will host it. Keep that
computer running when you want scans and email delivery to continue.

If you already have data, use the [upgrade and migration guide](deployment.md#preserve-existing-deployments).
Do not delete a database or Docker volume to repeat this guide.

## 1. Check what you have

You need a Linux Intel/AMD 64-bit host, Git, Docker Engine, and the Compose plugin.
Check them:

```bash
uname -m
git --version
docker version
docker compose version
```

Expect `x86_64`, a Git version, both a Docker client and server, and a Compose
version. If a command is missing, install it before continuing. Use Docker's
[official Linux installation instructions](https://docs.docker.com/engine/install/)
and [Compose plugin instructions](https://docs.docker.com/compose/install/linux/).
Docker access gives control over the host; use an authorized administrator
account. This guide assumes a standard Docker Engine installation.

Prepare these external services too:

| You need | Why |
|---|---|
| A real hostname, such as `alerts.example.com` | The dashboard requires HTTPS. Replace the example with a hostname you control. |
| DNS pointing that hostname to your server | Caddy uses the hostname to obtain a certificate. Any A/AAAA records you publish must lead to the correct server. |
| A firewall/router plan for ports 80 and 443 | Compose publishes these host ports. They must be available, and certificate validation must be able to reach the service. |
| An SMTP server and authorized sender address | SMTP is the connection used to send invitations and alerts. |
| A supported fare-provider account | Configure SerpApi or Kiwi Tequila with an eligible account. Aviationstack is optional and provides flight status, not fare searches. |

Check that the Docker subnet `172.30.80.0/24` does not conflict with a network
already used by your host or VPN; an administrator can help with that check.
See [network details](deployment.md#supported-topology) before changing it.

Use a test hostname and only your own recipient during the first trial. The
supplied Compose file publishes ports; it does not create a VPN or firewall.
If you require network-private staging, arrange that access and certificate
validation with your administrator before starting the services.

## 2. Download the project

```bash
git clone https://github.com/Naiguy1/Flight-Alerts.git
cd Flight-Alerts
git switch main
git rev-parse HEAD
```

The last command prints the exact source revision. Keep it with your deployment
notes and check its **Tests, Security, Image, and CodeQL** results on GitHub.
Run the remaining commands from this `Flight-Alerts` directory.

## 3. Set the website and email connection details

```bash
cp .env.example .env
chmod 600 .env
nano .env
```

`nano` is a text editor; another private editor is fine. In nano, save with
Ctrl+O, Enter, then exit with Ctrl+X. Do not type a shell prompt such as `$` when
copying commands from this guide.

Update the following values in `.env`:

| Setting | What to enter |
|---|---|
| `PUBLIC_HOST` | Your hostname only, for example `alerts.example.com`; no `https://` or path |
| `PUBLIC_BASE_URL` | The matching address with HTTPS: `https://alerts.example.com` |
| `ACME_EMAIL` | An email address you own for certificate notices |
| `ADMIN_USERNAME` | The dashboard login name; `admin` is the default |
| `SMTP_HOST` | Your mail provider's SMTP hostname |
| `SMTP_PORT` | Your provider's TLS mail port, commonly 587 for STARTTLS or 465 for SMTPS |
| `ALERT_SENDER_EMAIL` | The sender address that the SMTP account is allowed to use |

Leave `ENABLE_SCRAPER=false`, `INSTALL_SCRAPER=false`, and the default secret-file
paths for this first setup. Do not paste passwords or API keys into `.env`.
Never use `example.com` as your actual service or sender domain.

## 4. Choose a small first search

The supplied configuration monitors many destinations. Start smaller to avoid
using an unfamiliar provider account's allowance too quickly.

Create a private, Git-ignored bootstrap file:

```bash
mkdir -p data
chmod 700 data
nano data/bootstrap.yaml
```

Paste this complete example, keeping the indentation:

```yaml
scanner:
  origins: [SLC]
  scan_interval_hours: 24
  provider_requests_per_minute: 1
  provider_daily_budget: 10
  concurrency: 1
  search_window:
    min_days_ahead: 30
    max_days_ahead: 60
    sample_departure_offsets_days: [35]
    trip_duration_days: 7
filters:
  alerts_paused: true
  recipient_emails: []
monitored_destinations:
  - code: LHR
    city: London
    country_code: GB
    baseline_avg_usd:
      SLC: 1000
```

This example searches Salt Lake City to London for a seven-day trip leaving
35 days after a scan. Change `SLC` and `LHR` to your intended three-letter airport
codes, update the country/city, and keep the baseline's origin key aligned with
`origins`. The $1000 reference is an example you choose, not a researched market
average. Prices are in USD.

Choose limits below your provider plan. Budgets are per provider; retries and
fare rechecks can use additional requests. Pausing alerts prevents ordinary
alert delivery, **but scans still use provider requests** and invitation emails
can still be sent. Leaving a provider key empty disables that provider.

Set the container-readable file ownership:

```bash
sudo chown 10001:10001 data/bootstrap.yaml
sudo chmod 600 data/bootstrap.yaml
```

`10001` is the service's Linux user ID inside the container, not your dashboard
username. The file stays private. Use `sudoedit data/bootstrap.yaml` if you need
to edit it again.

## 5. Build the application and create private credentials

Builds can take several minutes and need internet access:

```bash
docker build --pull --platform linux/amd64 --tag flight-alerts:local .
mkdir -p secrets
chmod 700 secrets
docker run --rm -it --network none --user 0:0 --entrypoint python   --mount "type=bind,src=$PWD/secrets,dst=/run/init-secrets"   flight-alerts:local -m tools.init_secrets   --directory /run/init-secrets --uid 10001 --gid 10001
```

Enter a new dashboard password of at least 16 characters, then confirm it. No
characters may appear while typing a password; that is expected. Store it in
your password manager. The tool writes a password hash and a random signing
secret without printing their contents, plus empty files for external services.

This command uses the Python already inside the image. It runs with no network
connection and mounts only the secret directory. Existing secret files are never
overwritten; rerunning it does not reset an existing password.

## 6. Fill in SMTP and provider credentials

For a first deployment with SerpApi, edit these files privately:

```bash
sudoedit secrets/smtp_username secrets/smtp_password secrets/serpapi_api_key
```

Put only the raw value in each file: no `NAME=`, quotation marks, or YAML.
For example, the username file contains the SMTP login and the password file
contains the password or app password required by that provider. Never put a
real credential into a terminal command, Git commit, screenshot, or support post.

| Secret file | Contents |
|---|---|
| `smtp_username` | SMTP account login, if authentication is required |
| `smtp_password` | Matching SMTP password/app password |
| `serpapi_api_key` | SerpApi key for fare searches, if using that account |
| `kiwi_api_key` | Kiwi Tequila key instead of, or in addition to, SerpApi |
| `aviationstack_api_key` | Optional flight-status account key |
| `admin_password_hash` | Generated automatically; do not paste your plain password here |
| `token_secret` | Generated automatically; leave it unchanged for normal use |

Configure both SMTP username and password together, or leave both empty only if
your mail service intentionally supports the configured unauthenticated setup.
An API key alone does not guarantee access to an endpoint: check account plans,
quotas, supported coverage and permitted data use with the provider.

Files created by the tool already have private ownership and modes. If your
editor changes them, restore the edited files explicitly, for example:

```bash
sudo chown 10001:10001 secrets/smtp_username secrets/smtp_password secrets/serpapi_api_key
sudo chmod 600 secrets/smtp_username secrets/smtp_password secrets/serpapi_api_key
```

The directory is deliberately owned by user 10001, so a normal `ls secrets` may
report permission denied. Use an administrator for maintenance; do not make the
files world-readable to solve a permissions problem.

## 7. Prepare the database and start

Validate Compose, build the HTTPS proxy, and load your small first configuration:

```bash
docker compose config --quiet
docker compose build --pull proxy
docker compose run --rm --no-deps   --volume "$PWD/data/bootstrap.yaml:/app/bootstrap.yaml:ro"   --env CONFIG_PATH=/app/bootstrap.yaml migrate
docker compose up -d
docker compose ps --all
```

The validation command normally prints nothing on success. The explicit
migration command should print `Database schema and configuration initialized`.
It seeds settings from your private YAML before the web/worker processes start.
The later normal `migrate` service can finish with exit code 0; it is a one-time
job, not a server that must keep running.

Expect the web and worker containers to become healthy, and the proxy to stay
running. Allow time for the first image build and certificate issuance. Inspect
failures with:

```bash
docker compose logs --tail=100 migrate flight_web flight_worker proxy
```

Keep logs private. See [troubleshooting](troubleshooting.md) if a container exits,
a certificate fails, or a provider returns an error.

The database lives in a Docker named volume, **not** `data/bootstrap.yaml` or a
host file at `data/flight_deals.db`. The YAML seeds settings only once. Later
edits to that file, a restart, or an image rebuild do not replace database
settings. Do not delete the volume to change settings.

## 8. Sign in and run a controlled first trial

Open your actual `https://...` address in a browser. Use `ADMIN_USERNAME` and
the password entered in step 5. An unauthenticated request should be rejected;
an HTTP visit should redirect to HTTPS. Do not expose the internal web port 8000.

1. Keep **Pause alerts** selected while checking the configuration.
2. In **Recipient subscriptions**, invite your own address.
3. Open the invitation and confirm it. The dashboard should show **Confirmed**.
4. Choose **Queue a scan**. Check **Provider health** and **Scan jobs** for results.
5. Review **Fresh fares**. A healthy search can legitimately return no fares.
6. Uncheck **Pause alerts**, save, and allow the next eligible scan to run when
   you are ready to receive alerts. Scans may be refused during the short manual
   cooldown; do not repeatedly click the button.
7. Check each recipient's delivery result. No email is expected unless a fare
   meets the discount and other rules.

The [dashboard guide](using-flight-alerts.md) explains those rules and the real
behavior of the latest-alert resend button. Finish the
[production checklist](production-readiness.md) with your operator before
relying on this deployment or opening it to other users. Create and test a
[backup](operations.md#back-up-a-docker-installation) before making upgrades.
