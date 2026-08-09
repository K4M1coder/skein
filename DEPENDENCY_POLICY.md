# Dependency and engineering policy

Skein prefers established, actively maintained libraries over custom implementations when a library materially improves security, interoperability, correctness, or maintainability.

## Selection criteria

A new dependency must satisfy the following checks before adoption:

1. It solves a real project requirement better than a small standard-library implementation.
2. It has active releases, maintained documentation, automated tests, and a clear security-reporting process.
3. Its license is compatible with Skein's MIT license.
4. Its transitive dependency and supply-chain cost are proportionate to the feature.
5. It supports the Python and operating-system versions used by Skein.
6. Its integration is covered by unit tests and, for user-visible behavior, an end-to-end test.
7. It can be upgraded or removed without corrupting persisted data.

Dependencies are version-bounded, reviewed before upgrades, and never loaded directly from an unpinned remote URL at runtime. Security-sensitive defaults must fail closed. Secrets must not be logged or returned by APIs.

## Current adoption plan

| Area | Preferred maintained component | Decision |
|---|---|---|
| Password hashing | `pwdlib[argon2]` with Argon2id | Adopt with transparent verification and rehashing of legacy PBKDF2 hashes. Do not invalidate existing accounts. |
| Email address validation | `email-validator` | Adopt for normalization and syntax validation; keep deliverability checks disabled during registration unless explicitly configured. |
| Async SMTP | `aiosmtplib` | Adopt when the HTTP layer becomes asynchronous; retain certificate validation and explicit STARTTLS/implicit TLS modes. |
| API and schemas | FastAPI and Pydantic | Planned migration from the prototype HTTP server, endpoint by endpoint, with OpenAPI schemas and request validation. |
| Persistence | SQLAlchemy 2 and Alembic | Planned migration from direct SQLite statements. Alembic becomes the only production schema migration path. |
| Authorization | PyCasbin | Evaluate through contract tests against the current permission matrix before replacing the existing RBAC evaluator. Domain-scoped roles are required for future multi-tenant pools. |
| Rate limiting | A maintained backend-aware limiter | Adopt before multi-node deployment; the current SQLite limiter remains acceptable only for the single-node prototype. |
| Frontend Markdown | GFM renderer, DOMPurify, Mermaid, syntax highlighter | Already uses specialized libraries; versions must be vendored or package-locked and covered by XSS regression tests. |

## Migration rules

- A library migration is a behavior-preserving change unless the product requirement explicitly changes.
- Persisted hashes, database rows, workflow reports, and artifacts must remain readable during a migration.
- Security changes require negative tests, not only happy-path tests.
- Large framework migrations are split into reviewable stages; no untested big-bang rewrite.
- Every dependency addition records its purpose and upgrade/removal path in this file or an architecture decision record.

## Definition of done

A feature is complete only when its implementation, authorization checks, error reporting, automated tests, and operator documentation agree. For workflow execution, a mocked response is not an end-to-end success: the selected model or runtime must execute and produce an inspectable result or an explicit actionable error.
