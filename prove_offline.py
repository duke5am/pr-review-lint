#!/usr/bin/env python3
"""
prove_offline.py -- proof harness: no network on --dry-run, and malformed model
output is rejected.

Two claims in the documentation are worth being able to demonstrate rather than
assert:

  1. `--dry-run` and `--plan-only` make NO network request at all.  This matters
     if you are evaluating whether the bot can be run safely on a laptop, or if
     you need to show an auditor that the diff was not transmitted.
  2. When the model replies with prose instead of the required format, the tool
     refuses to post anything, exits with code 4, and says what was wrong.

This script proves both by running the real `review.py` as a subprocess with
`socket.socket` replaced by a function that raises.  Any attempt to open a
connection -- including by a library that review.py does not know about --
aborts the child process loudly.

It is a demonstration tool, not a test.  The automated assertions live in
scripts/test_review.py.

Usage:
    python3 scripts/prove_offline.py             # uses the bundled fixture
    python3 scripts/prove_offline.py my.diff     # or any diff of your own

It needs no third-party packages and no API key.  It never contacts an LLM.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
REVIEW = HERE / "review.py"
CONTENT_ROOT = HERE.parent

#: Injected into the child process with -c.  It installs the block, then runs
#: review.py as __main__ with the right sys.argv.
#:
#: Note the technique: `socket.socket` is replaced with a *subclass* rather than
#: a plain function. `ssl.SSLSocket` is defined as `class SSLSocket(socket)`, so
#: replacing the name with a function breaks `import ssl` outright. A subclass
#: keeps the class relationship intact and still lets us block every connect.
BLOCKER = r"""
import runpy
import socket
import sys

MARKER = "NETWORK ACCESS ATTEMPTED"


class NetworkBlocked(RuntimeError):
    pass


class BlockedSocket(socket.socket):
    def connect(self, *args, **kwargs):
        raise NetworkBlocked(MARKER + ": BlockedSocket.connect was called")

    def connect_ex(self, *args, **kwargs):
        raise NetworkBlocked(MARKER + ": BlockedSocket.connect_ex was called")


def blocked_getaddrinfo(*args, **kwargs):
    raise NetworkBlocked(MARKER + ": socket.getaddrinfo was called")


socket.socket = BlockedSocket
socket.getaddrinfo = blocked_getaddrinfo

target = sys.argv[1]
sys.argv = sys.argv[1:]
runpy.run_path(target, run_name="__main__")
"""


def run_blocked(args) -> subprocess.CompletedProcess:
    """Run review.py in a child process whose network is disabled."""
    # -B keeps the child from writing a __pycache__ directory next to the engine.
    child_argv = [sys.executable, "-B", "-c", BLOCKER, str(REVIEW)] + list(args)
    return subprocess.run(
        child_argv,
        capture_output=True,
        text=True,
        cwd=str(CONTENT_ROOT),
    )


def banner(title: str) -> None:
    print()
    print("=" * 72)
    print(title)
    print("=" * 72)


def main() -> int:
    if len(sys.argv) > 1:
        fixture = Path(sys.argv[1]).resolve()
    else:
        fixture = CONTENT_ROOT / "tests" / "fixtures" / "sample.diff"
    if not fixture.is_file():
        print(f"diff not found: {fixture}", file=sys.stderr)
        return 2

    rules = str(CONTENT_ROOT / "rules-example" / "*.md")

    # Make sure no credentials are inherited from the environment, so this run
    # cannot be accused of having used one.
    env_clean = {
        k: v
        for k, v in os.environ.items()
        if k
        not in {
            "AI_REVIEW_API_KEY",
            "OPENAI_API_KEY",
            "ANTHROPIC_API_KEY",
            "LLM_API_KEY",
        }
    }
    saved_environ = os.environ.copy()
    os.environ.clear()
    os.environ.update(env_clean)
    try:
        failures = 0

        banner("CLAIM 1: --dry-run makes no network request")
        dry = run_blocked(["--diff", str(fixture), "--rules", rules, "--dry-run"])
        print(f"command  : python3 scripts/review.py --diff {fixture.name} "
              f"--rules 'rules-example/*.md' --dry-run")
        print(f"exit code: {dry.returncode}")
        if "NETWORK ACCESS ATTEMPTED" in (dry.stdout + dry.stderr):
            print("RESULT   : FAIL -- a network call was attempted")
            failures += 1
        elif dry.returncode == 0:
            print("RESULT   : PASS -- completed the whole dry run with the network "
                  "blocked at the socket layer")
        else:
            print("RESULT   : FAIL -- exited non-zero with the network blocked")
            print(dry.stderr[-2000:])
            failures += 1

        banner("CLAIM 1b: --plan-only makes no network request")
        plan = run_blocked(["--diff", str(fixture), "--rules", rules, "--plan-only"])
        print(f"exit code: {plan.returncode}")
        if "NETWORK ACCESS ATTEMPTED" in (plan.stdout + plan.stderr):
            print("RESULT   : FAIL -- a network call was attempted")
            failures += 1
        elif plan.returncode == 0:
            print("RESULT   : PASS")
        else:
            print("RESULT   : FAIL")
            print(plan.stderr[-2000:])
            failures += 1

        banner("CLAIM 2: malformed model output is rejected, nothing is posted")
        # Drive main() with a stubbed transport that returns prose instead of
        # the delimited format.  The stub records every call, so we can also
        # show that no comment was posted to any GitHub URL.
        driver = r'''
import importlib.util, io, json, sys
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

review_path = Path(sys.argv[1])
rules = sys.argv[2]
fixture = sys.argv[3]

spec = importlib.util.spec_from_file_location("rev", review_path)
rev = importlib.util.module_from_spec(spec)
sys.modules["rev"] = rev
spec.loader.exec_module(rev)

CALLS = []

def fake_transport(url, method="GET", headers=None, body=None, timeout=None,
                   max_retries=1):
    CALLS.append((method, url))
    # The model replies with plausible-looking prose. Exactly the failure mode
    # the strict parser exists to catch.
    return 200, json.dumps({"choices": [{"message": {"content":
        "Here are the issues I found:\n"
        "1. The SQL query is built with string concatenation.\n"
        "2. There is an unused helper in Dashboard.tsx.\n"}}]})

rev.http_request = fake_transport

import os
os.environ["AI_REVIEW_API_KEY"] = "not-a-real-key"
os.environ["GITHUB_TOKEN"] = "not-a-real-token"

out, err = io.StringIO(), io.StringIO()
with redirect_stdout(out), redirect_stderr(err):
    code = rev.main(["--diff", fixture, "--rules", rules,
                     "--pr-url", "https://github.com/acme/widgets/pull/42"])
print("EXIT_CODE:", code)
print("--- stderr ---")
print(err.getvalue().strip())
github_calls = [c for c in CALLS if "api.github.com" in c[1]]
print("--- calls made ---")
for method, url in CALLS:
    print(f"  {method} {url}")
print("GITHUB_CALLS:", len(github_calls))
'''
        driver_file = Path(tempfile.mkstemp(suffix="_driver.py")[1])
        try:
            driver_file.write_text(driver, encoding="utf-8")
            proc = subprocess.run(
                [sys.executable, "-B", str(driver_file), str(REVIEW), rules, str(fixture)],
                capture_output=True,
                text=True,
                cwd=str(CONTENT_ROOT),
            )
        finally:
            driver_file.unlink(missing_ok=True)
        print(proc.stdout.strip())
        if proc.stderr.strip():
            print("--- driver stderr ---")
            print(proc.stderr.strip()[-1500:])

        ok = (
            "EXIT_CODE: 4" in proc.stdout
            and "GITHUB_CALLS: 0" in proc.stdout
            and "REJECTED output" in proc.stdout
        )
        print()
        if ok:
            print("RESULT   : PASS -- exit code 4, malformed output rejected, "
                  "zero GitHub calls, so no comment could be posted")
        else:
            print("RESULT   : FAIL -- expected exit code 4, a rejection message, "
                  "and zero GitHub calls")
            failures += 1

        banner("SUMMARY")
        if failures == 0:
            print("All claims demonstrated. No LLM API was contacted by this script.")
            return 0
        print(f"{failures} claim(s) failed.")
        return 1
    finally:
        os.environ.clear()
        os.environ.update(saved_environ)


if __name__ == "__main__":
    sys.exit(main())
