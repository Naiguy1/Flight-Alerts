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

### Repository verification, 2026-10-27

**Decision: hold production release pending the follow-up review and operator
gates below.** CI success alone does not authorize public deployment.

The initial hardening change was merged through
[PR #1](https://github.com/Naiguy1/Flight-Alerts/pull/1). The verified merged SHA is
`3c5b9e16bb89c3906b053bbab2a5d468303f2c8a`:

| Evidence | Result |
|---|---|
| [Tests / Security / Image](https://github.com/Naiguy1/Flight-Alerts/actions/runs/40599887085) | All passed; 249 tests, zero failures/errors, zero skips; locked HTTP integration tests executed; isolated backup/restore passed |
| [Package and secret diagnostics](https://github.com/Naiguy1/Flight-Alerts/runs/122525288738) | Production and optional scraper audits found no known package vulnerabilities; full-history secret scan passed |
| [Application/proxy diagnostics](https://github.com/Naiguy1/Flight-Alerts/runs/122526224211) | Neither image had a release-blocking finding; image-sbom artifact was generated |
| [CodeQL](https://github.com/Naiguy1/Flight-Alerts/actions/runs/40599887083) | Passed; no unsuppressed high/critical finding |
| Independent approval on PR #1 | No approving review recorded; automated comment does not satisfy independent approval |
| Main branch protection | `protected=false` when inspected; required policy has not been applied |

The follow-up candidate corrects expiry checks after revalidation/SMTP pacing
and suppresses an invitation confirmed while waiting. Image diagnostics now
include the SBOM package/version inventory, its SHA256, and every severity count.
Review the follow-up's exact SHA and checks separately; the baseline results in
this table do not validate new commits. Local tests used available dependencies;
locked installation was unavailable in the editing environment. CI is required
to execute the complete locked suite without skips.

Follow-up [PR #7](https://github.com/Naiguy1/Flight-Alerts/pull/7), implementation
SHA `b7c111e580e39e3298fbb34e3d092c72bbb8ac9b`, passed
[CI](https://github.com/Naiguy1/Flight-Alerts/actions/runs/40601322955) with 260 tests
and no skips, and [CodeQL](https://github.com/Naiguy1/Flight-Alerts/actions/runs/40601322959).
The [expanded image report](https://github.com/Naiguy1/Flight-Alerts/runs/122530735740)
showed no high/critical findings, but exposed old installer/router versions and
GO-2026-5932. This SHA is superseded by remediation on the same PR; do not release
it based on its green severity gate. The [specific finding review](supply-chain.md#follow-up-finding-review-2026-10-27)
records fixes for every available patch and the evidence required to assess the
remaining OpenPGP-only advisory. Rerun all four gates on the remediation commit.

The intermediate remediation SHA `8dc5b2b3efca2426e89f3196990809a7663d4ed9`
passed 262 tests with no skips, package/secret audits and CodeQL, but its
[image gate failed](https://github.com/Naiguy1/Flight-Alerts/runs/122533991245)
on vulnerable libraries bundled in the newer installer. This candidate is also
superseded. Both unused installer distributions are removed from the shipped
image; build-time dependency checking, final locked distribution inventory and
full image scanning remain mandatory. The same report confirmed the router fix
and absence of affected OpenPGP packages from 1029 proxy build dependencies.

An administrator must arrange an eligible reviewer and use
`python -m tools.apply_branch_protection --repository Naiguy1/Flight-Alerts --apply`,
then run the same command without `--apply` to verify. This requires a privately
configured administration token; the repository connector has no administration
write operation. Keep the one-reviewer policy unless the maintainer deliberately
documents a different review policy. No bypass or policy exception was adopted.

### Deployment sign-off (operator must complete)

No production database, prior image, host credentials or external-service
verification were provided for this review. The following fields are pending;
synthetic migration/recovery tests do not complete these requirements. Record
non-sensitive results and evidence locations here; keep backups/settings/secrets
private.

- Deployed Git SHA / image digest:
- CI and security scan URLs:
- External TLS / firewall / auth verification:
- Host and backup encryption verification:
- Production backup restoration drill:
- Provider entitlement / quota verification:
- Controlled verified-recipient delivery:
- GitHub branch-protection verification:
- Monitoring and rollback owner:
- Eligible independent reviewer / approved candidate SHA / approval link:
- Final merged SHA / Tests, Security, Image, CodeQL URLs:
- Application and proxy image findings / SBOM checksum and review:
