# Production readiness

This maps every audit checklist item to implemented controls and the evidence
still required on the actual deployment. A committed control is not proof that
an external service/host setting has been configured. Record the deployed SHA,
reviewer, date, and verification result before release.

| Audit checklist item | Repository implementation | Deployment evidence still required |
|---|---|---|
| Protect administrative routes | Fail-closed Basic authentication, scrypt password hashes, protected administrator handlers, authentication regressions | Strong private credential; anonymous/incorrect-auth rejection through public HTTPS |
| HTTPS, CSRF, trusted origins/hosts/proxy | Caddy automatic HTTPS, fixed trusted proxy IP, signed CSRF forms, exact origin/host checks, security headers | DNS/ACME issuance; external certificate/redirect test; firewall and subnet isolation |
| Escape HTML and validate booking URLs | Cached autoescaped templates, approved HTTPS booking hosts, injection tests | Inspect rendered deployment and security headers |
| Verify SMTP certificates/hostnames | Default verified SSL context for STARTTLS/SMTPS and rejection tests | Provider's real certificate chain and supported authentication, controlled confirmed recipient test |
| Remove credential logging and inspect historical exposure | Sanitized structured provider/delivery failures, no proxy/Uvicorn access logs, CI secret scan | Inspect old logs/image layers privately; rotate any exposed keys at their issuers |
| Exclude secrets/build state and provide placeholders | `.gitignore`, `.dockerignore`, explicit Docker source copies, `.env.example`, private `_FILE` secret mounts, image checks | Restrictive secret ownership/modes; no real secrets in local build contexts or registries |
| Validate offers/settings/recipients | Domain/schema constraints, finite positive USD prices, bounded dates/strings/list sizes, recipient validation and confirmation | Review purchased provider account/currency/coverage and operator baselines |
| Coordinate scans and deliver per recipient | Transactional jobs/leases, atomic alert reservations, outbox leases/retries, recovery/concurrency tests | One local persistent host/volume; review uncertain SMTP acknowledgment behavior |
| Provider quotas/retries/deadlines/isolation | Persistent per-attempt budgets, bounded concurrency/deadlines, typed outcomes, retry/backoff, provider regression tests | Budgets below purchased quotas; real 429/error monitoring |
| Fix paths/empty recipients/staleness/dedup | Explicit bootstrap path, DB settings versions, empty means none, fresh/future dashboard filters, material-improvement rules | Review migrated settings and confirmed recipients; check old offers disappear |
| Restrict containers/mounts/networks/credentials | UID10001, read-only images, cap_drop ALL, no-new-privileges, resource limits, dedicated networks, worker-only SMTP/API secrets | Docker/host administrator access; correct volume/secret ownership; only proxy ingress |
| Storage permissions/encryption/restore | Private SQLite/backup files, consistent backup/integrity/restore commands, optional age backup encryption, synthetic restore test | Encrypted host volumes/certificate storage and off-host backups; actual production restore drill |
| Lock/scan dependencies and base image | Hash-locked runtime/test/optional scraper, verified base/proxy digests, pinned Action SHAs, pip-audit, Trivy, SBOM, Dependabot | Review scanner results for the deployed SHA; rebuild/update images when fixes ship |
| CI and protected main | Required test/security/image/CodeQL workflows, reviewed protection JSON and apply/check script | Apply GitHub admin policy; enable secret scanning/push protection/private vulnerability reporting where available |
| Regression tests | Auth/CSRF/XSS/TLS/payload/budget/lease/partial-send/recovery and backup tests | CI green on final SHA and controlled staging checks without production mail/API side effects |
| Documentation, health, monitoring, rollback | Deployment/architecture/operations runbooks; process/readiness/heartbeat probes; provider/delivery views; backup-based rollback procedure | Configure external alerts, log/backup retention, record previous image, and rehearse rollback |

## Additional features and architecture changes

The administrator can inspect scan/provider freshness and delivery state, pause
alerts, invite verified recipients, and manage expiring confirmation/unsubscribe
actions. Notification policies support quiet hours, digests, and material price
improvements. Optional flight-status watches extend monitoring to supported
status/delay/gate transitions with an eligible provider account.

Settings, jobs, budgets, and outbox state are durable. Provider transport/parsing,
evaluation, orchestration, persistence, web security, and notification delivery
have explicit boundaries. Queries are bounded and indexed; history cleanup
limits growth. See the architecture runbook for single-host/SMTP limitations.

## Release record

- Deployed Git SHA / image digest:
- CI and security scan URLs:
- External TLS / firewall / auth verification:
- Host and backup encryption verification:
- Production backup restoration drill:
- Provider entitlement / quota verification:
- Controlled verified-recipient delivery:
- GitHub branch-protection verification:
- Monitoring and rollback owner:
