# Security

Do not open a public issue containing credentials, recipient addresses, signed
subscription links, or vulnerability details. Use GitHub's private vulnerability
reporting on this repository when enabled. Otherwise contact the repository owner
privately before sharing a reproducer.

This project supports one administrator and one deployment host using local
SQLite. Public self-registration, multi-tenant isolation, arbitrary webhooks,
and multi-host SQLite volumes are not supported. The dashboard must be served
through the provided HTTPS proxy or an equivalently isolated trusted proxy.

Production deployment requires completing `docs/production-readiness.md`,
including branch protection, external TLS validation, provider entitlement/quota
checks, encryption at rest, and a backup restoration drill. Application code and
CI cannot attest to those external controls.

Never send production messages or call paid providers from tests. Tests use
synthetic credentials and mocked network transports. Dependency/image scanner
findings must be assessed and fixed before release; the workflows do not hide
unfixed high/critical image findings or known Python package vulnerabilities.

Credential rotation must invalidate exposed provider/SMTP secrets, not merely
remove them from Git history. Rotating TOKEN_SECRET invalidates all outstanding
confirmation, unsubscribe, and CSRF tokens; arrange replacement links for active
recipients. Administrator password hashes and secret files must not be committed.
