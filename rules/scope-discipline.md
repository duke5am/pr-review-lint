# Rule: Scope discipline

**Applies to:** every pull request, every language.

## Why this rule exists

A review comment that says "while you were here, you should also..." is the most
expensive kind of comment a team can write. It restarts review on code nobody
asked to change, and it turns a two-day-old branch into a two-week-old branch.
This rule exists to keep pull requests reviewable, not to be bureaucratic.

## Rules

### S1 — One logical change per pull request

A pull request must do one thing. Refactors, dependency bumps, formatting runs,
renames, and behaviour changes must be separate pull requests unless the change
is meaningless without them.

Report a finding when a diff mixes two or more of these categories:

- a behaviour change *and* a pure refactor of unrelated code
- a feature *and* a dependency upgrade
- a bug fix *and* a rename of public symbols
- a feature *and* a fix to a pre-existing typo, comment, or lint warning in an
  unrelated file

Severity: `medium`. Raise to `high` when the unrelated change touches
authentication, authorisation, payment, data migration, or the build pipeline.

### S2 — No drive-by edits

Do not report formatting-only changes on their own; a formatter handles those.
Report edits to files that are clearly unrelated to the stated purpose of the
pull request — for example, a change to `terraform/` inside a pull request about
a React component.

Severity: `medium`.

### S3 — Tests ship with the behaviour they cover

New behaviour that is covered by the team's test suite conventions must arrive
with a test in the same pull request. A test that asserts nothing, or that
asserts `True`, does not count and must be reported.

Report a finding when a diff adds or changes production behaviour in a file that
has a sibling test file, and no test file is touched in the same diff.

Severity: `high` when the behaviour involves money, permissions, or data
deletion. Otherwise `medium`.

### S4 — No dead code left behind

Report a finding for code the diff adds that nothing reads: an exported helper
with no caller, a constant that is written and never read, a feature flag that
is never checked, a `TODO`-guarded block that can never execute.

A single unused import is below the reporting threshold — the linter owns that.
An unused *function*, *module*, or *state variable* is in scope.

Severity: `low` for a small unused helper. `medium` when the dead code is a
whole module, an exported public API, or configuration that appears load-bearing
but is not.

### S5 — Comment-only changes must not be smuggled in as fixes

Report a finding when a diff changes only comments, docstrings, or
documentation *and* the pull request claims to change behaviour. Either the
claim is wrong, or the code change is missing.

Severity: `medium`.

### S6 — Public interface changes are announced

Report a finding when a diff changes a public function signature, an HTTP route,
a CLI flag, an exported type, or a database column, and the same diff does not
update the callers, the docs, or a changelog in this repository.

Severity: `high` for a breaking removal or rename. `medium` for an addition.

## What is out of scope for this rule

- Style, formatting, and naming preferences.
- Test coverage of code the diff does not touch.
- Requests to rewrite a working module "properly".
- Anything the team has not agreed in writing here. If you want a new rule, add
  it to this file in its own pull request.
