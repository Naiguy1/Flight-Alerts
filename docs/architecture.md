# Architecture and behavior

The administrator web process validates input and writes transactional settings,
recipient invitations, and scan jobs. It does not spawn workers, call providers,
or send SMTP. The worker claims persistent jobs under leases, takes a validated
configuration snapshot, fetches normalized offers with provider budgets, records
observations, and reserves qualified alerts atomically. Notification delivery
uses persistent per-recipient state. SQLite write transactions are short and
serialized; WAL permits dashboard reads while the worker writes.

## Boundaries

- `settings.py`: validated bootstrap configuration and runtime secret loading.
- `web/`, `dashboard.py`: administrator authentication, CSRF/origin/host policy, escaped HTML, and signed subscription actions.
- `services/`, `main.py`: scan orchestration, bounded concurrency, heartbeat, status watches, scheduling, and cleanup.
- `fetcher/`: provider transports, quota accounting per attempt, timeout/retry policy, payload parsing, normalized domain offers, and URL validation.
- `analyzer/`: side-effect-free eligibility/baseline/discount decisions.
- `db/`, `migrations/`: parameterized SQLite persistence, schema upgrades, configuration versions, scan leases, alert reservations, recipient/outbox state, and bounded read models.
- `notifier/`: verified SMTP transport, escaped messages, expiring purpose-bound signed links, quiet hours/digests, and recipient retries.

## Delivery semantics

An alert intent is reserved before sending. A unique delivery identity and
compare-and-swap lease ensure that independent workers cannot concurrently
complete the same delivery as their own. Failed recipients are retried
individually; a successful recipient does not erase another recipient's failure.
Unverified/disabled recipients are excluded from ordinary deal/status delivery.
An explicit empty list means no recipients. Pausing alerts, quiet-hour deferral,
and digest batching are visible operator settings.

SMTP acceptance followed by a process crash can leave acknowledgment uncertain.
Lease recovery can resend an accepted message. Operators must expect occasional
duplicates in that failure mode; exactly-once email is not promised. Choose an
idempotent external delivery API if a stronger guarantee becomes necessary.

## Price and freshness semantics

Amounts are validated as finite positive USD values. Airport/country/date
formats and HTTPS booking-host allowlists are enforced at normalization.
Historical comparisons require adequate samples and display baseline provenance;
configured targets remain operator assumptions, not verified market averages.
Observed fares can expire. The dashboard filters old/past-departure offers and
has no deals when there are no active destinations. Material price improvements
can create a new alert during the cooldown according to configured thresholds.

Query fanout is bounded by provider budgets, concurrency, request/scan deadlines,
and typed failure outcomes. Missing credentials disable providers. Healthy
searches with no results are tracked separately from failures. Status watches
require an optional Aviationstack account; the adapter honors its supported
flight/date coverage and records meaningful status/delay/gate changes.

## Scaling

This release targets one host. Keep one scheduled worker and use persistent
leases for manual/scheduled overlap. Local SQLite and container restart policies
are sufficient for a personal deployment; they are not a multi-region queue.
Before multi-host scaling, introduce a transactional database and queue backend,
versioned cross-database migrations, deployment coordination, and stronger
identity/authorization appropriate to multiple administrators or tenants.

## Runtime supply chain

The application image uses a digest-pinned official CPython 3.12 Alpine runtime.
Exact Python dependency versions retain reviewed glibc and musllinux wheel hashes;
no source-package build is permitted during runtime dependency installation. The
proxy is rebuilt from the fixed Caddy v2.11.7 module using a pinned Go 1.27.3
builder and x/net v0.62.0. Go verifies module downloads with the public checksum
database. Runtime proxy layers contain the static binary, certificates and timezone
data rather than the upstream image's unrelated curl/brotli tools. Actual image
SBOMs and strict high/critical vulnerability gates remain mandatory after any
change; base selection alone is not evidence of a clean scan.

The final application image contains no package installer. Dependency consistency
is checked during the locked build, and CI verifies the final installed
distributions against that lock. Dependency updates require an image rebuild.

Caddy access logging is disabled and its global runtime error logger removes the
entire request object. This also protects token-bearing URLs when an upstream
fails. CI deliberately causes a loopback upstream outage and checks that the error
is recorded without the synthetic token, Authorization header or cookies.
