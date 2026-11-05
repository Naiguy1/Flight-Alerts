# Audit report resolution and launch checks

Compared against main `3719f8775ccb5717f8b338eeab35b6695f43ced9` on 2026-11-05.
Code changes do not constitute evidence that a production deployment was changed.

## Report findings

| Finding | Resolution / remaining verification |
|---|---|
| Scraper has no `close()` | Idempotent no-op cleanup now satisfies scanner and revalidator lifecycle calls. |
| A small fare increase cancels a valid deal | Revalidation accepts a fresh offer for the same source, route, and dates when the current evaluator still qualifies it. The email payload receives the actual new fare, baseline, and discount. |
| YAML changes silently ignored | Worker, web, and migration startup warn with the active SQLite version and explicit sync command. Dashboard changes remain authoritative until an intentional import. Sync now works on a fresh database and repeated identical imports do not increment versions. The deployed database's TPA/duration settings still need host verification. |
| Static sampled departure dates | One date rotates within each calendar partition across successive persisted jobs, keeping the same number of queries and covering gaps between the original offsets. Phase zero preserves the original sample dates. Travel dates remain inside the configured window. |
| Cheapest-only historical baseline | Every eligible returned price is stored, after deal evaluation, so the current search cannot qualify itself. The dashboard displays the cheapest result of the newest search batch. Existing minimum-only history ages out under retention; it cannot be reconstructed. Historical references remain observed averages, without relabeling a larger operator assumption as a measured market price. |
| Different travel dates deduplicated | Cooldown and improvement thresholds now match origin, destination, departure, and return date. Different date options remain available to existing opt-in digest delivery. |
| Quota starvation | Scraper-first provider waterfall uses a paid fallback for errors, empty results, or no eligible offers. Healthy fallback restores query coverage while provider errors remain visible. Circuits and job-order rotation remain active. A capacity warning accounts for scan interval, date matrix, request rate, and deadline before requests. |
| Monitored domestic airport silently rejected | Config normalization opts monitored US airports into the domestic allowlist, including dashboard edits. Explicit blocks and foreign-country exclusions still apply; excluded routes are skipped before requesting providers. |
| Docker logs unbounded | Every bundled service inherits a `json-file` limit of 20 MB per file and five files per service. Recreate deployed containers to apply the change. Separately managed external Caddy logs require their own host settings. |
| Proxy dependency vulnerability found during validation | Rebuild Caddy v2.11.9 with the pinned upstream Smallstep JOSE v4 API migration; build and CI reject JOSE v3 packages. The certificates migration is a merged commit awaiting a tagged release. Final Image CI and independent dependency review remain required; see [supply-chain.md](supply-chain.md#jose-dependency-migration-candidate-2026-11-05). |
| Inaccurate setup instructions | README links the actual setup/upgrade guides, existing Compose file, HTTPS origin, and mounted provider secrets. |

Provider budgets are keyed by provider in SQLite. A second provider does not
consume the first provider's daily allowance; the report's combined-budget
calculation was inaccurate. Retries, earlier scans, status queries, and fare
rechecks still consume their respective allowances. Capacity warnings are
planning estimates, not reservations. Budget/deadline exhaustion still produces
an incomplete scan; no code can guarantee finding every fare with a finite
query budget. Calendar rotation expands sampling over successive scans, not all
dates in every scan. Broad range/grid provider APIs would require separately
validated adapter contracts; this change retains exact-date round-trip queries.

## Required automated evidence

Check the final PR commit, not an earlier commit or historical readiness entry:

1. `Tests`: full locked pytest suite, zero skips, compilation, Ruff, dependency
   consistency, dependency pins, and isolated backup/restore.
2. `Security`: production and optional scraper dependency audits and full-history
   secret scan.
3. `Image`: application/proxy builds, restricted runtime, installed dependency
   inventory, proxy privacy probe, vulnerability gates, and SBOM.
4. `CodeQL`: Python security analysis and high/critical gate.

## Production sign-offs still requiring deployment access

| Gate | Action / evidence |
|---|---|
| Branch protection and independent review | Apply `.github/branch-protection.json` with `python -m tools.apply_branch_protection --repository Naiguy1/Flight-Alerts --apply`, using a privately supplied repository-administration token. Verify the policy and required final-head checks. The connected GitHub tools do not expose branch-protection writes. |
| Real provider redundancy | Mount a valid SerpApi or Kiwi key in production, verify the account permits this endpoint, then exercise scraper failure with successful fallback. No credentials are embedded in code. |
| Effective config | Inspect destination codes and durations in the running dashboard. If YAML is the intended replacement, back up first, review all settings, then run `docker compose exec flight_worker python -m tools.sync_config`. Sync replaces the full config using version checks; it is not an automatic merge. |
| HTTPS and proxy identity | Follow [existing-caddy.md](existing-caddy.md): the reviewed dedicated trusted address is `172.30.80.2`, not a transient `172.18.0.3`. Verify the actual peer address, HTTPS redirect, subscription links, and certificate issuance/renewal on the host. |
| Production backup recovery | Use the drill below on a real production backup. Record backup time, integrity/foreign-key checks, recovered subscriptions/configuration, and recovery duration. Keep backups encrypted off-host. |
| Rollout | Back up, build and recreate the relevant application services using the existing host override, check health, complete a scan, confirm a verified recipient receives a current fare, and verify log limits with container inspection. |

Never paste admin tokens, API keys, SMTP passwords, or subscriber data into an
issue or PR. Follow [operations.md](operations.md) and [deployment.md](deployment.md)
for the host's backup locations and service commands.

## Production backup drill

Run `tools.backup` on the host or in an authorized maintenance container with
access to the existing data volume. Substitute the actual mounted paths. Use a
new scratch destination for the drill; never overwrite the live database.

```bash
python -m tools.backup create --source /mounted-data/flight_deals.db --destination /private-backups/release-backup.db
python -m tools.backup verify --source /private-backups/release-backup.db
python -m tools.backup restore --source /private-backups/release-backup.db --destination /private-drill/restored.db --confirm-services-stopped
python -m tools.backup verify --source /private-drill/restored.db
```

The confirmation applies to all services that could open the restore target;
the isolated destination must not be mounted into running services. Compare
restored configuration versions and verified-recipient counts privately against
the backup, then start an isolated instance without production network credentials
to verify database readiness. Do not delete the source to simulate host failure.
