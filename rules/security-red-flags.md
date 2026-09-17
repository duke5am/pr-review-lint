# Rule: Security red flags

**Applies to:** every pull request, every language.

These are the patterns that have caused real incidents on real teams. They are
worth a review comment every single time, because the cost of a false positive
is one sentence of discussion, and the cost of a false negative is an incident.

## Rules

### SEC1 — Injection: query built by string assembly

Report a finding when SQL, HQL, Cypher, or a shell command is built by
concatenating, interpolating, or formatting values that come from outside the
process. This includes f-strings, `+`, `%`, `.format()`, template literals, and
`String.format`.

The fix is a parameterised query or an argument vector — never "escape the
input".

Severity: `critical`.

### SEC2 — Secrets in the repository

Report a finding for any credential, token, password, private key, or
connection string with a real-looking value appearing in a diff, including in
test fixtures, example configs, documentation, and CI workflow files. Default
values such as `"dev-token-please-change"`, `"changeme"`, or a hardcoded
fallback for a secret environment variable count.

The fix is an environment variable or secret store with **no** insecure
fallback: fail closed at startup if the value is missing.

Severity: `critical`.

### SEC3 — Secret or personal data written to logs, errors, or responses

Report a finding when a token, session identifier, password, full
authorisation header, or personal data field is logged, included in an error
message, or returned in an API response payload.

Returning a credential in a response body is `critical`; logging it is `high`.

### SEC4 — Missing authorisation check on a new endpoint

Report a finding when a diff adds a route, handler, RPC method, queue consumer,
or GraphQL resolver and the same diff does not show an authentication and
authorisation check, or a call to the project's standard guard / middleware.

Severity: `critical` when the endpoint reads or mutates another user's data.
Otherwise `high`.

### SEC5 — Disabled security controls

Report a finding when a diff disables, weakens, or skips a security control:

- `verify=False`, `verify_ssl=False`, `rejectUnauthorized: false`
- `--insecure`, `-k`, `curl -k`
- `Access-Control-Allow-Origin: *` on an endpoint that carries credentials
- a `# nosec`, `# noqa: S...`, or `// eslint-disable` on a security rule
- deletion or loosening of a validation function, a rate limit, or a timeout
- `chmod 777`, world-writable files, or containers running as root

Severity: `high`, or `critical` when it removes a control from an
internet-facing path.

### SEC6 — Unsafe deserialisation or dynamic execution

Report a finding for `pickle`, `yaml.load` without `SafeLoader`, `eval`,
`exec`, `new Function`, `vm.runInNewContext`, `unserialize`, `Marshal.load`, or
a dynamic `require`/`import` of a value that comes from a request.

Severity: `high`. `critical` when the input is attacker-controlled.

### SEC7 — Untrusted input used to build a path or a URL

Report a finding when a request value is joined into a filesystem path, a
redirect target, a proxy URL, or an outbound webhook without an allow-list.

Severity: `high` (path traversal, SSRF, open redirect).

### SEC8 — Dependency added without a pinned version

Report a finding when a diff adds a direct dependency to a manifest with a
floating range (`*`, `latest`, `>=1.0.0` with no upper bound in a project that
otherwise pins).

Severity: `medium`.

### SEC9 — Randomness and comparison for security values

Report a finding when a token, nonce, or identifier that must be unguessable is
generated with `random`, `Math.random`, or a timestamp; or when a secret is
compared with `==` in a language where a constant-time comparison exists.

Severity: `high`.

## A note on not being annoying

Report each red flag once, at its first occurrence, with the concrete fix. Do
not list every line of a pattern repeated ten times — one finding naming the
pattern and the fix is the point. Do not report test-only or example-only code
at `critical` unless the value is a live credential.
