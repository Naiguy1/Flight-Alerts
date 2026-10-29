# Use an existing Caddy with a migrated installation

This procedure keeps your existing Caddy, TLS certificates and other sites, and
uses the already-restored Flight-Alerts database volume. Back up your current
Caddyfile and local Compose overrides before editing. Do not restore the old
database again or rerun first-time configuration seeding to apply this change.

## Connect Caddy using a stable, dedicated address

The default web command trusts forwarded HTTPS headers only from `172.30.80.2`.
Joining the web container to `jellyfin_default` does not give Caddy this address.
Instead, join the existing Caddy to Flight-Alerts' dedicated internal network.
It can keep its other networks for Jellyfin and other sites.

1. In the Flight-Alerts directory, inspect `docker compose config` privately and
   identify the local override you already use. Merge the `proxy` service from
   [the external Caddy override](../deploy/compose.external-caddy.yml) into that
   file, preserving unrelated settings. Its profile keeps the bundled proxy from
   starting during normal `docker compose up`. Do not enable `bundled-proxy`.
2. Remove the local `flight_web` command override that trusts `172.18.0.3` and
   remove its attachment to `jellyfin_default`. Keep the default `app` network;
   restore the repository command with `--forwarded-allow-ips 172.30.80.2`.
3. If a bundled proxy container exists, stop only that service:
   `docker compose stop proxy`. Inspect `docker network inspect flight-alerts_app`
   and confirm the subnet is `172.30.80.0/24` and address `172.30.80.2` is free.
   Resolve any subnet/address conflict before proceeding.
4. Attach the existing Caddy without restarting its other sites:

   ```bash
   docker network connect --ip 172.30.80.2 flight-alerts_app caddy
   ```

   If it is already connected, inspect its address before changing anything.
   Never assign the same IP to both Caddy containers.

5. Make that attachment survive future Caddy recreation. Locate its owning
   Compose files rather than guessing:

   ```bash
   docker inspect caddy --format '{{index .Config.Labels "com.docker.compose.project.config_files"}}'
   ```

   Merge the following into that Compose configuration. `default` represents its
   existing network; retain all other existing networks and service settings.
   Do not recreate Caddy yet just to apply this file: step 4 applied the live
   attachment. This configuration makes the next planned recreation persistent.

   ```yaml
   services:
     caddy:
       networks:
         default: {}
         flight_alerts:
           ipv4_address: 172.30.80.2
   networks:
     flight_alerts:
       external: true
       name: flight-alerts_app
   ```

6. Validate both Compose configurations from their own project directories.
   Recreate only Flight-Alerts' web service after checking its effective command
   and networks:

   ```bash
   cd /home/bowerbank-admin/Flight-Alerts
   docker compose config --quiet
   docker compose up -d --no-deps --force-recreate flight_web
   docker compose ps --all
   ```

## Let Flight-Alerts authenticate the flights site

For this installation the mounted Caddyfile is
`/home/bowerbank-admin/jellyfin/Caddyfile`. Preserve other sites and certificate
settings. Remove the flights site's old `basic_auth`/`basicauth` section or its
authentication snippet import. The flights site should proxy to the new service:

```caddyfile
flights.bowerbanks.com {
    reverse_proxy flight_web:8000 {
        header_up -Forwarded
        header_up X-Forwarded-Proto https
        header_up X-Forwarded-Host {host}
    }
}
```

Keep access logging disabled for this site: subscription URLs contain signed
tokens. Retain the repository's other proxy privacy controls where applicable.
If a shared response-header policy sets `Referrer-Policy: no-referrer`, remove
that override for this site so the application's `strict-origin` header reaches
the browser. Never force, remove or fabricate the browser's `Origin` header.

Validate and reload the existing Caddy:

```bash
docker exec caddy caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile &&
docker exec caddy caddy reload --config /etc/caddy/Caddyfile --adapter caddyfile
```

If an editor replaced the host Caddyfile inode, a single-file Docker bind mount
can still expose the previous contents. Compare the host and container file
hashes with `sha256sum`; do not publish the full file with credential hashes.
If they differ, reconcile the mount in a planned Caddy maintenance window.

## Verify before resuming delivery

```bash
curl -q -sS -D - -o /dev/null https://flights.bowerbanks.com/ \
  | grep -Ei '^(HTTP/|WWW-Authenticate:|Referrer-Policy:)'
```

An anonymous request must return `401` with realm `Flight Alerts` and policy
`strict-origin`. Realm `restricted` indicates Caddy's old authentication is still
active. `HTTPS required` indicates the proxy's source address or forwarded HTTPS
headers are wrong. A browser may silently reuse cached Basic Auth credentials;
the anonymous curl check establishes that authentication is enforced.

After deploying the form fix, reload the dashboard and use the new form token.
Confirm that Save delivery settings and Queue a scan work. Keep alerts paused
while checking migrated routes, scan history and recipients. The displayed
`No recipients invited` means there are no subscriptions available for delivery;
having addresses in a YAML file alone does not confirm subscriptions. Invite
the intended addresses through the dashboard and have their owners confirm.
Only run an email trial when sending to those recipients is intended.

Historical prices are preserved, but old scan job history may not exist in the
legacy schema. A `scan_canceled` job can follow an intentional worker stop;
`budget_exhausted` requires checking request caps before queuing another scan.

After PR #9 is merged and checks pass, update source and rebuild with the same
scraper option used on this server:

```bash
cd /home/bowerbank-admin/Flight-Alerts
git pull --ff-only
docker compose build --build-arg INSTALL_SCRAPER=true flight_web
docker compose up -d --no-deps --force-recreate flight_web
```

No database migration or signing-key rotation is needed for that dashboard fix.
The worker continues using its existing running image until a planned worker
update. Preserve the local overrides and existing named volume.
