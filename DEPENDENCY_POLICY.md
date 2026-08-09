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

Dependencies are version-bounded and reviewed before upgrades. Browser dependencies loaded from a pinned CDN URL must also declare a SHA-384 Subresource Integrity hash and anonymous CORS mode; vendoring remains preferred for fully offline deployments. Security-sensitive defaults must fail closed. Secrets must not be logged or returned by APIs.

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
| Frontend Markdown | Marked, DOMPurify, Mermaid, Highlight.js | Adopted at pinned versions with SRI. DOMPurify remains mandatory before generated Markdown enters the DOM; retain XSS regression coverage. |
| Interface icons | Lucide | Adopted at a pinned version for consistent, accessible SVG navigation icons. |
| Internationalization | i18next | Adopted with embedded resources, explicit English fallback, and a lightweight local fallback when the library is unavailable. |

## Migration rules

- A library migration is a behavior-preserving change unless the product requirement explicitly changes.
- Persisted hashes, database rows, workflow reports, and artifacts must remain readable during a migration.
- Security changes require negative tests, not only happy-path tests.
- Large framework migrations are split into reviewable stages; no untested big-bang rewrite.
- Every dependency addition records its purpose and upgrade/removal path in this file or an architecture decision record.

## Code and localization policy

- Source code, identifiers, comments, commit messages, technical documentation, logs, and API error codes are written in English.
- The user interface supports English and French through the localization layer; new user-visible text must not be hard-coded in feature logic.
- Automatic language selection remains the default and falls back to English when the browser language is unsupported.
- English is the canonical translation key set. French coverage must be tested whenever user-visible text changes.
- A change is not complete if one supported language exposes untranslated keys or prevents the same workflow from being completed.

## Definition of done

A feature is complete only when its implementation, authorization checks, error reporting, automated tests, and operator documentation agree. For workflow execution, a mocked response is not an end-to-end success: the selected model or runtime must execute and produce an inspectable result or an explicit actionable error.
