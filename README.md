# pr-review-lint

A first-pass pull-request review driven by **your team's own written rules** — and
a dry-run mode that shows you exactly what it would post before it posts anything.

No dependencies. Standard library only.

```bash
python3 review.py --diff pr.diff --rules 'rules/*.md' --dry-run
```

```
[review] 10 file(s) in diff, 5 to review, 5 skipped, 1 chunk(s)
  reviewed paths : src/api/handlers.py, src/web/Dashboard.tsx, …
  skipped:
    - package-lock.json  [lockfile]
    - assets/logo.png  [binary file]
    - gen/proto/users_pb2.py  [generated or minified artifact]
    - web/dist/app.bundle.min.js  [generated / vendored directory]
    - src/api/query.sql  [deleted file]
  threshold : severity >= medium, max 8 finding(s)
========================================================================
DRY RUN: no API call was made and nothing was posted.
```

## The design decision that matters

Most automated reviewers fail the same way: they comment forty times on a PR,
people mute them, and the tool becomes decoration. So the two features that matter
here are **severity thresholds** and a **noise cap**.

```
--min-severity medium      # nothing below this is reported
--max-findings 8           # hard ceiling per PR
```

A finding below the threshold is withheld and *counted* in the footer
(`0 point(s) below the threshold were withheld`), so nothing is hidden silently.

## What it skips, on purpose

Lockfiles, binary files, minified and generated artifacts, vendored directories,
and deleted files. Reviewing a lockfile diff wastes the model's attention and
produces findings nobody can act on.

## Rules are yours, in markdown

`rules/` holds four example rule files — scope discipline, security red flags,
testing expectations, operational readiness. They are plain markdown, so changing
what the reviewer cares about is a documentation edit, not a code change.

This is the difference between a linter and a reviewer: a linter knows syntax, and
your rules know your architecture.

## Dry-run is the default workflow

`--dry-run` and `--plan-only` make **no network call at all** — verified. Use them
to tune the threshold and the cap against a real PR before enabling it, because a
reviewer that is too noisy on day one never gets a second chance.

## Malformed output is rejected, not posted

If the model replies with prose instead of the expected delimited format, the run
**fails loudly and posts nothing** rather than putting garbage on the PR. There are
18 rejection cases covering prose, JSON, missing keys, unknown severities,
truncated messages and merged blocks.

## Security, stated plainly

- **The diff is sent to a third-party API.** That is a data-governance decision
  only you can make. `docs/SECURITY.md` in the full pack covers the options.
- **Prompt injection is not solved.** A malicious PR can make the reviewer *say*
  anything. It cannot make it *do* anything — the tool posts one comment and exits
  — and findings name only files in the diff. Never act on findings automatically.
- Do not run it with secrets on untrusted fork PRs. The full pack's workflow shows
  the same-repository gate.

## Not verified here

**The GitHub Actions workflow has never run on live GitHub**, and no LLM API was
contacted — both request shapes were tested against an injected fake transport.
Confirm field names against your provider's reference before enabling it.

## The full pack

The paid kit adds the GitHub Actions workflow, `SETUP.md` and `SECURITY.md`, and
102 tests covering diff parsing, chunking, severity filtering, the noise cap,
idempotent comment updating and malformed-response rejection.

→ More developer tooling like this: **[duke5am.gumroad.com](https://duke5am.gumroad.com)** <!-- GUMROAD-LINK -->
