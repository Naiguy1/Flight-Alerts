# Merge and release guide for maintainers

New users should [install from main](getting-started.md), not merge branches.
[PR #1](https://github.com/Naiguy1/Flight-Alerts/pull/1) and
[PR #7](https://github.com/Naiguy1/Flight-Alerts/pull/7) are already merged.
PR #7's resulting main SHA is `82537e8a53390b08b230eaf721ee5b62664a9d53`.
This procedure applies to **future changes**.

A pull request proposes source changes; a merge adds them to `main`. Neither
operation installs files or restarts a deployment server. Deployment follows the
[setup](getting-started.md) or [upgrade](operations.md#update-an-existing-installation)
guide after the merged revision's checks pass.

## Prepare and review a change

Use a clean working directory and preserve any unrelated local work. For example:

```bash
git fetch origin
git switch -c my-change origin/main
# Make and validate the intended changes.
git diff --stat
git diff
git add README.md
# Stage only the intended files; README.md is an example above.
git commit -m "Describe the change"
git push -u origin my-change
```

Open a pull request with base `main` and your new branch as the comparison.
Use the README's developer checks for code changes. Never add real provider/SMTP
credentials or private recipient data to a test run or commit.

Before merging, require the **same final head** to pass **Tests, Security, Image,
and CodeQL**. A dependency-lock generation run alone is not a release gate.
Review the Files changed and Checks tabs, resolve conversations, and obtain an
eligible independent approval. The author cannot supply their own approving review.

The reviewed `.github/branch-protection.json` describes required checks,
up-to-date branches, stale-review dismissal, resolved conversations, linear main
history and administrator protection. An administrator must actually apply and
verify it; committing the JSON does not enable GitHub protection. Arrange another
eligible reviewer, or deliberately document an appropriate solo-maintainer review
policy before enabling a requirement nobody can satisfy. Do not bypass red gates.

If main advances while a change is under review, update the feature branch
without force-pushing, resolve conflicts, and wait for fresh validation/approval:

```bash
git fetch origin
git switch my-change
git merge origin/main
git push origin my-change
```

## Merge and record the result

1. Check the exact head SHA, all required checks, approval and resolved conversations.
2. Mark a draft ready when its prerequisites are complete. Choose **Squash and
   merge** when applying the supplied linear-history policy.
3. Wait for all four checks on the resulting `main` SHA. The merge produces a
   different SHA from the feature branch.
4. Record the reviewed/merged SHAs and CI links in
   [production readiness](production-readiness.md#release-record).
5. Rebuild from that main commit. Record actual application/proxy image IDs or
   registry digests and complete external staging/host/provider checks before use.

Delete an old feature branch only after its review/evidence links are recorded.
Keep rollback images and backups according to their own retention policy.

## If validation or deployment fails

A failed or missing check means the change is not ready. Read its failed step
and any available bounded validation report. Fix the change and rerun checks;
do not remove a gate to make the display green. Documentation-only commits also
need the checks required by the repository policy.

For a deployment rollback, stop web/worker, preserve the current database and
WAL/SHM sidecars privately, restore the verified pre-upgrade backup into a new
empty target, and start its recorded compatible previous image. Do not run old
code against a newly migrated database. Review recovered pending messages before
enabling SMTP, since earlier mail acceptance may be uncertain.

See [operations](operations.md), [deployment](deployment.md), and
[the remaining checklist](next-steps.md) for the detailed operator work.
