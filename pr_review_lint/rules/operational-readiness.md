# Rule: Operational readiness

**Applies to:** pull requests that change a service, a job, a migration, or a
scheduled task.

A service that works on a laptop and cannot be debugged at 03:00 is not done.
This rule is the list of things that must be true before a change is safe to
deploy — the parts a compiler and a linter cannot check.

## Rules

### O1 — Failures must be visible

Report a finding when a diff adds an operation that can fail (a network call, a
file read, a query, a subprocess, a deserialisation) and the failure is
swallowed: a bare `except: pass`, a `catch {}` with no logging, a promise with
no rejection handler, or an error ignored by discarding the return value.

Severity: `high` for a data-mutating path, otherwise `medium`.

The fix is a log line with enough context to reconstruct the input, plus a
deliberate decision about whether to retry, degrade, or propagate.

### O2 — New configuration must fail closed and be documented

Report a finding when a diff reads configuration that has no default and no
startup validation, so a missing value surfaces as a confusing `undefined`
error at request time instead of a clear failure at boot.

Also report when a new environment variable is read in code but is not
documented in this repository's `.env.example`, README, or deployment config,
and not added to the deployment manifests.

Severity: `medium`, or `high` when the missing value is a security-relevant
setting such as an allow-list or a feature kill-switch.

### O3 — Migrations must be reversible and safe to run while deployed

Report a finding when a diff:

- adds a column or table with no way to roll back (no `down` migration)
- drops or renames a column that the currently deployed version of the code
  still reads (breaking change during a rolling deploy)
- adds a `NOT NULL` column with no default to a table that already has rows
- rewrites a large table in a single statement with no batching or timeout
- takes an exclusive lock on a table that receives live traffic

Severity: `critical` for a destructive, irreversible migration. Otherwise
`high`.

### O4 — Timeouts and bounds on every outbound call

Report a finding when a diff adds an HTTP call, a database query, a queue
publish, or a subprocess with no timeout, no retry policy, and no bound on the
response size it will accept.

Severity: `medium`. `high` when it sits in a request path that a user is waiting
on, or in a loop.

### O5 — Retries must be safe to repeat

Report a finding when a diff adds a retry around a non-idempotent operation (a
charge, a POST that creates a row, a message publish) without an idempotency key
or a deduplication guard.

Severity: `high`.

### O6 — Resource leaks

Report a finding when a diff opens a connection, file, cursor, stream, thread,
or lock and does not close or release it on every path, including the error
path.

Severity: `medium`.

### O7 — New failure modes need a rollback story

Report a finding when a diff introduces a change that cannot be turned off
without a code deploy — a new always-on behaviour with no flag — where the
change alters data, money, or user-visible output.

Severity: `medium`.

## What is out of scope for this rule

- Preference for one observability vendor over another.
- Requests to add metrics or tracing to code this diff does not touch.
- Infrastructure design that is not visible in the diff.
