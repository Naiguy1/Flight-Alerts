# Next steps and release roadmap

The hardening implementation is consolidated on `production-hardening`, targeting
`main`. Agent work is part of that branch; there are no separate agent branches
to merge in a particular order. Use [the merge guide](merging.md) for the release.
Repository controls and synthetic tests do not establish that a real host or
external provider is production-ready.

## 1. Finish the review and release gates

Owner: repository maintainer and an independent reviewer.

- Review the complete `main...production-hardening` diff, especially migrations,
  authentication/CSRF, token handling, provider credentials, quota accounting,
  and SMTP retry/uncertain-acknowledgment behavior.
- Require the final commit's **Tests**, **Security**, **Image**, and **CodeQL**
  checks to pass. A green dependency-lock resolution run is insufficient.
- Inspect package findings for production and optional scraper environments,
  full-history secret scanning, application/proxy image findings, and the image
  SBOM. Fix findings or document a specific reviewed policy exception before
  release; do not turn off a gate to hide a failure.
- Check that HTTP integration tests ran with the locked dependencies. Local
  skips caused by unavailable packages do not substitute for CI execution.
- Apply and verify the reviewed `main` protection policy using repository admin
  access. The supplied policy requires one approving reviewer; a sole maintainer
  must arrange an eligible reviewer or deliberately adopt a documented review
  policy before applying a rule they cannot satisfy.
- Record the exact reviewed Git SHA and CI links in the release record in
  [production readiness](production-readiness.md).

## 2. Rehearse migration on an isolated copy

Owner: deployment operator.

1. Preserve the current deployment's Git SHA, image ID/digest, settings, and
   secret files privately. Create and verify a consistent SQLite backup.
2. Restore that backup to a separate directory or named volume. Keep the test
   deployment unable to reach production SMTP/provider endpoints.
3. Run the new migration tool against the copy, then verify settings, recipient
   consent state, price history, alert/delivery history, and integrity checks.
4. Exercise queued scan/delivery recovery with mocked or nonproduction services.
   Confirm that a successful recipient does not suppress a failed recipient.
5. Rehearse restoring the pre-upgrade backup with the prior compatible image.
   Do not run the old program against a newly migrated database.

`python -m tools.backup self-test` checks backup mechanics on synthetic data. An
actual production-data restore rehearsal remains a separate requirement.

## 3. Configure the host and external services

Owner: deployment operator.

- Provision Linux amd64, local persistent SQLite storage, encrypted host/certificate
  volumes and encrypted off-host backups. Do not use NFS or multi-host SQLite.
- Confirm that the dedicated `172.30.80.0/24` Docker subnet does not conflict with
  the host. Keep the exact trusted proxy address aligned in Compose/Uvicorn.
- Set matching DNS, `PUBLIC_HOST`, and HTTPS `PUBLIC_BASE_URL`; allow only intended
  ingress. Verify certificate issuance and the HTTP-to-HTTPS redirect externally.
- Generate a strong administrator hash and signing secret with the initialization
  tool. Set private secret ownership/modes so UID 10001 can read the mounted
  files. Keep SMTP/provider credentials exclusively in the worker.
- Inspect historical logs and images for credentials; revoke any exposed keys at
  the issuer and replace their secret files. Merely excluding `.env` from new
  builds does not remove it from old layers.
- Verify SMTP certificate/authentication settings and provider plan access,
  quotas, coverage, and data rights. Missing keys disable providers; the optional
  scraper requires both a locked-image build option and runtime opt-in.
- Configure monitoring for provider freshness/coverage, quota exhaustion,
  incomplete scans, pending/failed deliveries, worker heartbeat, disk usage, and
  backup failures. Set log and backup retention explicitly.

## 4. Stage, merge, and release gradually

Owner: maintainer and deployment operator.

- Deploy privately first and complete the external checks in the runbook.
- Verify anonymous access rejection, CSRF protection, TLS/security headers,
  subscription confirmation/unsubscribe, pause/quiet/digest behavior, and bounded
  scanning. Use only a deliberately controlled, confirmed recipient for a real
  delivery test.
- Merge the reviewed branch after final checks pass, rebuild from the merged
  commit, and record the resulting image ID/digest. A squash merge creates a new
  `main` SHA, so wait for its checks before deployment.
- Begin with conservative quotas and a small route/watch set. Inspect successful
  provider data and recipient delivery outcomes before increasing coverage.
- Keep the prior image and verified pre-upgrade backup until the observation
  period succeeds. Review recovered pending messages before re-enabling SMTP
  after any restore because their previous acceptance may be uncertain.

## 5. Ongoing maintenance and later scaling

- Review Dependabot changes and scanner results; regenerate locks only on the
  supported Python/platform target, review the dependency diff, and rerun all
  gates. Update pinned image digests rather than relying on mutable tags.
- Test restoration regularly and after schema/storage changes. Rotate secrets
  according to a defined policy and issue replacement signed links when rotating
  `TOKEN_SECRET`.
- Improve baseline cohorts and confidence using fresh comparable observations;
  an operator-configured reference remains an assumption rather than a verified
  market average.
- Before multiple administrators/tenants or multiple hosts, introduce appropriate
  identity/authorization, a transactional server database/queue, cross-database
  migrations, and deployment coordination. This release does not promise those
  capabilities.
- If exactly-once delivery becomes a requirement, replace ordinary SMTP with a
  provider that offers usable idempotency semantics. SMTP's ambiguous acceptance
  case can still produce a duplicate after a crash.
