# Rule: Testing expectations

**Applies to:** every pull request that changes production behaviour.

## Why this rule exists

Tests are the only part of a codebase that tells you what the code is *supposed*
to do. A test that cannot fail is worse than no test, because it produces
confidence that is not earned. This rule covers the two failure modes we keep
seeing: tests that assert nothing, and error paths that were never exercised.

## Rules

### T1 — No vacuous tests

Report a finding for a test that cannot fail:

- a body of `assert True`, `pass`, `expect(true).toBe(true)`, or an empty body
- a test whose only assertion is on a mock's own return value
- a test marked `skip` or `xfail` without a linked issue in a comment
- a whole test file commented out
- a test that catches the assertion error it just raised

Severity: `high`. A vacuous test is treated as a missing test, so it also counts
as a violation of S3 (tests ship with behaviour).

### T2 — Error paths need coverage

Report a finding when a diff adds a validation branch, an exception handler, or
a failure return, and the same diff adds no test that takes that branch.

A new `try/except` with no test that triggers the exception is the most common
form of this. A new `if not valid: return 400` with no test that sends invalid
input is the second most common.

Severity: `medium`. `high` when the branch handles payment, authentication, or
data loss.

### T3 — Tests must not depend on wall-clock time, real network, or ordering

Report a finding when a test:

- calls `sleep()` to wait for something to become true
- compares against `datetime.now()` / `Date.now()` without freezing time
- makes a real network or database call instead of using the project's fixture
  or a fake
- depends on the order of other tests, or on shared mutable state left by them
- uses a fixed port number

Severity: `medium`. These are the tests that fail once a week in CI and get
retried instead of fixed.

### T4 — A fixed bug needs a regression test

Report a finding when a diff fixes a defect (the message says "fix", the code
adds a null check, bounds check, or a guard for a previously unhandled case) and
no test in the diff reproduces the old failure.

Severity: `high`. Without the test, nothing stops the bug from coming back.

### T5 — Mocks must not replace the thing under test

Report a finding when a test mocks the very function or module the diff
changed, so that the assertion only proves the mock was called.

Severity: `medium`.

### T6 — Fixtures must be realistic enough to be meaningful

Report a finding when a test asserts against an empty input, a single
zero-value, or `{}` where the behaviour under test is about shape, ordering,
size, or formatting.

Severity: `low`.

## What is out of scope for this rule

- Line coverage percentage, and any demand for a specific number.
- Requests for tests of code this diff does not touch.
- Unit-versus-integration taste. Both are acceptable; the rules above apply to
  either.
- Test naming, structure, or framework choice.
