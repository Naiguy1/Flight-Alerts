# Merge and release guide

## Branches and prerequisites

Merge **`production-hardening` into `main`** through a pull request. All hardening,
feature, and architecture changes are consolidated on that branch; there are no
separate agent branches requiring ordered merges. `main` remains the production
source until a reviewed merge. Branch publication alone is not deployment.

Before merging, require the same final head commit to pass **Tests**, **Security**,
**Image**, and **CodeQL**. The separate dependency-lock workflow only generates
reviewable hash locks; its success is not a release approval. Check the PR's
Files changed and Checks tabs, resolve review conversations, and obtain the
required independent approval.

The reviewed `.github/branch-protection.json` policy enforces these checks,
up-to-date branches, stale-review dismissal, resolved conversations, linear main
history, and administrator protection. Applying that policy requires a GitHub
credential with repository administration access; the repository connector
cannot configure those settings. The policy requires one approving reviewer,
which the PR author cannot supply themselves. Arrange another eligible reviewer
or adopt an explicitly documented solo-maintainer policy before enabling a rule
that would prevent all merges. Do not bypass a failing test/security gate.

## Inspect locally

Use a clean checkout; keep any unrelated working changes safe before switching.
No command below embeds credentials.

```bash
git clone https://github.com/Naiguy1/Flight-Alerts.git
cd Flight-Alerts
git fetch origin
git switch --track origin/production-hardening
git log --oneline origin/main..HEAD
git diff --stat origin/main...HEAD
git rev-parse HEAD
```

If the repository/branch already exists locally, use `git switch
production-hardening` and `git pull --ff-only origin production-hardening`
instead. Compare the printed SHA with the PR's checked commit. Install the locked
Linux amd64 / Python 3.12 test environment and run the README verification
commands. Provider and SMTP calls remain mocked in tests; never populate real
credentials just to run the suite.

If `main` advances, update the feature branch without rewriting published history:

```bash
git fetch origin
git switch production-hardening
git merge origin/main
# Resolve any conflicts, then run the complete tests and review the resolved diff.
git push origin production-hardening
```

Wait for the new head's checks and renewed approval. A feature-branch merge commit
is compatible with a final squash merge, which leaves `main` linear. Do not
force-push either branch. Avoid copying selected security files manually: the
worker, persistence, settings, notification, and web changes depend on each other.

## Merge in GitHub

1. Open the delivered pull request, or compare
   [main against production-hardening](https://github.com/Naiguy1/Flight-Alerts/compare/main...production-hardening?expand=1)
   and create one with base `main` and compare `production-hardening`.
2. If the PR is a draft, mark it ready only after validation and review are
   complete. Confirm the final commit, required checks, approval, and conversations.
3. Choose **Squash and merge** to preserve linear main history. Use a description
   summarizing the security fixes, durable jobs/outbox, migration, feature changes,
   validation, and deployment limitations. Do not enable a bypass to merge red
   checks.
4. Wait for CI on the resulting `main` commit. A squash merge creates a different
   SHA from the feature branch; record and deploy the merged SHA.
5. Delete the merged remote feature branch only after the release's code and
   validation links are recorded. Keep production backup/image retention separate
   from branch cleanup.

A GitHub merge does not install secrets, migrate the live database, issue TLS
certificates, or restart your host. Follow the release steps below deliberately.

## Deploy the merged revision

Read [deployment](deployment.md) and [operations](operations.md) before upgrading.
Create and verify a consistent pre-upgrade backup, retain the previous image and
private settings/secrets, and rehearse migrations on an isolated copy.

```bash
git switch main
git pull --ff-only origin main
git rev-parse HEAD
docker compose config --quiet
docker compose build --pull
docker compose up -d
docker compose ps
```

For an existing installation, first follow the runbook's backup/import steps for
the named volume. The migration service must finish successfully before web and
worker startup. Check HTTPS authentication, readiness, current worker heartbeat,
provider freshness/coverage, and per-recipient delivery. Record the merged Git
SHA, application/proxy image IDs or registry digests, CI results, and external
verification in `production-readiness.md`.

## If a check fails or rollback is needed

A failed check means the branch is not ready to release. Inspect the workflow and
bounded `Validation report: Tests`, `Security`, or `Image` check output. Missing
scanner/test reports are failures, not evidence of safety. Fix the issue on
`production-hardening`, push a normal commit, and rerun all required gates.

For application rollback, stop web and worker, quarantine the current database
and its WAL/SHM sidecars privately, restore the verified pre-upgrade backup into a
new empty target, and start the recorded compatible previous image. Do not point
old code at the migrated database. Review recovered pending delivery state before
allowing SMTP again; a message accepted before a crash may be retried. A source
revert alone is not a database rollback.

After the release, use [the next-steps roadmap](next-steps.md) to complete the
observation period, monitoring, restore drills, and ongoing maintenance.
