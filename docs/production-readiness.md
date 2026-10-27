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

### Verified implementation candidate, 2026-10-27

**Decision: automated code gates passed; production release remains on hold for
independent approval, repository protection and operator sign-off.**

Reviewed implementation SHA: `dd2069cac8d8a0efc8aeca945787afd90102a82f`, delivered
in [PR #7](https://github.com/Naiguy1/Flight-Alerts/pull/7) from
`production-hardening` to `main`. The initial hardening PR #1 already merged at
`3c5b9e16bb89c3906b053bbab2a5d468303f2c8a`. The follow-up fixes notification expiry
and confirmation checks after SMTP waits, explicit digest pruning, the SQLite
WAL initialization race, vulnerable runtime installers and the proxy router.

| Gate / evidence | Reviewed result on the implementation SHA |
|---|---|
| [Tests](https://github.com/Naiguy1/Flight-Alerts/actions/runs/40603396990) / [test diagnostics](https://github.com/Naiguy1/Flight-Alerts/runs/122536992375) | 264 tests passed, zero failures/errors, zero skips; locked HTTP integration coverage, dependency consistency and isolated backup/restore executed |
| [Security diagnostics](https://github.com/Naiguy1/Flight-Alerts/runs/122537049748) | No known production or optional scraper package vulnerabilities; full-history secret scan passed |
| [Application / proxy image and SBOM evidence](https://github.com/Naiguy1/Flight-Alerts/runs/122537784879) | Application: zero vulnerabilities at every severity. Proxy: zero low/medium/high/critical findings; one UNKNOWN module-level advisory retained and assessed below |
| [CodeQL](https://github.com/Naiguy1/Flight-Alerts/actions/runs/40603396980) | Passed; no unsuppressed high/critical finding |
| Additional local recovery checks | 144 notification/database/configuration/operations tests passed; actual eight-way initialization passed 25 repetitions; deterministic WAL tests cover transient locks, bounded exhaustion and immediate rejection of unrelated errors |

The reviewed CycloneDX 1.7 inventory contains 62 distinct package/version entries.
Its SHA256 is
`5e71363393dc6086e231ae2346fa23982b226d3f8033117f9815a747bf972a5e`.
The complete inventory is visible in the linked diagnostic and downloadable as
`image-sbom` on the CI run. All application distributions match their exact lock;
both entire unused installer distributions are absent from the final image.

The remaining scanner observation is GO-2026-5932 for `golang.org/x/crypto`.
The [specific advisory review](supply-chain.md#follow-up-finding-review-2026-10-27)
limits the not-affected assessment to its deprecated OpenPGP packages. CI found
none of those packages in the static proxy's 1029 build dependencies. The graph
SHA256 is `69f4947107ce0367d3916c81855a5d60239123b7d94d10f0ef3eb82dd3c462b8`.
The module-level finding remains visible, and the independent approver must
review the assessment. No scanner suppression or severity downgrade was adopted.

Documentation-only evidence updates after the implementation SHA must also pass
the four required checks on their final PR head. The review record in PR #7
identifies that final head. After merging, wait for the resulting main SHA's
checks and rebuild from it; CI image inventory is not a deployed image digest.

### Outstanding repository and deployment gates

No approving independent review was recorded on the original hardening PR.
`main` had `protected=false` and no repository rulesets when inspected. An
administrator must arrange an eligible reviewer, then apply and verify the
supplied policy:

```bash
python -m tools.apply_branch_protection --repository Naiguy1/Flight-Alerts --apply
python -m tools.apply_branch_protection --repository Naiguy1/Flight-Alerts
```

These commands require a privately configured administration token. The repository
connector has no administration write operation. Keep the supplied one-reviewer
policy unless the maintainer deliberately documents a different review policy;
no bypass or review-policy exception was adopted.

No production database, prior image, host access or external-service verification
was supplied. The operator must complete the actual production-backup migration
and rollback rehearsal, encrypted storage/backups, DNS/TLS/firewall and subnet
checks, private secret provisioning/rotation, provider entitlements/quotas,
monitoring/retention, private staging and controlled confirmed-recipient delivery.
Synthetic tests do not complete those gates. Record non-sensitive evidence below
and keep settings, secrets and backups private.

- Deployed Git SHA / application and proxy image digests:
- Final merged SHA / Tests, Security, Image, CodeQL URLs:
- Eligible independent reviewer / approved candidate SHA / approval link:
- Application and proxy findings / deployed SBOM checksum and review:
- External TLS / firewall / auth / CSRF verification:
- Host, certificate-volume and off-host backup encryption verification:
- Production backup migration / restoration / prior-image rollback rehearsal:
- Provider entitlement / data rights / quota verification:
- Controlled confirmed-recipient delivery / quiet-hour and digest verification:
- GitHub branch-protection verification:
- Monitoring, retention, observation period and rollback owner:
