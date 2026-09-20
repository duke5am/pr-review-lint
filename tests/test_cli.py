#!/usr/bin/env python3
"""Contract tests for the pr-review-lint CLI.

What packaging can break, and what nothing else covers: the no-network dry run,
the bundled rule files being found from inside the installed package, and bad
input exiting with a message instead of a traceback. No API call is made and no
API key is needed by anything here.

    python3 -m unittest discover -s tests
    python3 tests/test_cli.py -v
"""

from __future__ import annotations

import os
import subprocess
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "review.py"
SAMPLE = REPO / "fixtures" / "sample.diff"
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

EXPECTED_RULES = [
    "operational-readiness.md",
    "scope-discipline.md",
    "security-red-flags.md",
    "testing-expectations.md",
]


def run_cli(*args):
    env = dict(os.environ)
    # A real key in the environment would change nothing here (nothing calls an
    # API), but removing it keeps the tests honest about that.
    for name in ("AI_REVIEW_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY",
                 "GITHUB_TOKEN", "RULES_GLOB"):
        env.pop(name, None)
    return subprocess.run(
        [sys.executable, "-B", str(SCRIPT), *args],
        cwd=str(REPO), capture_output=True, text=True, env=env, timeout=300,
    )


def combined(proc):
    return proc.stdout + proc.stderr


class DocumentedBehaviourTests(unittest.TestCase):
    def test_help(self):
        proc = run_cli("--help")
        self.assertEqual(proc.returncode, 0, combined(proc))
        self.assertIn("usage:", proc.stdout)

    def test_version(self):
        proc = run_cli("--version")
        self.assertEqual(proc.returncode, 0, combined(proc))
        self.assertIn("0.1.0", proc.stdout)

    def test_plan_only_on_the_bundled_fixture(self):
        proc = run_cli("--diff", str(SAMPLE), "--plan-only")
        self.assertEqual(proc.returncode, 0, combined(proc))
        self.assertIn("files in diff   : 10", proc.stderr)
        self.assertIn("will review     : 5", proc.stderr)
        self.assertIn("will skip       : 5", proc.stderr)
        self.assertIn("package-lock.json  [lockfile]", proc.stderr)

    def test_dry_run_makes_no_api_call(self):
        proc = run_cli("--diff", str(SAMPLE), "--dry-run")
        self.assertEqual(proc.returncode, 0, combined(proc))
        self.assertIn("DRY RUN: no API call was made and nothing was posted.",
                      proc.stdout + proc.stderr)

    def test_no_rules_argument_uses_the_bundled_rules(self):
        # This is the packaging claim: the four example rule files live inside
        # pr_review_lint/rules/ and are the last resort in the default lookup.
        proc = run_cli("--diff", str(SAMPLE), "--plan-only")
        self.assertEqual(proc.returncode, 0, combined(proc))
        self.assertNotIn("no rule files matched", proc.stderr)


class BadInputTests(unittest.TestCase):
    def test_missing_diff_file(self):
        proc = run_cli("--diff", "/root/no-such-file.diff", "--dry-run")
        self.assertEqual(proc.returncode, 3, combined(proc))
        self.assertIn("diff file not found", proc.stderr)
        self.assertNotIn("Traceback", combined(proc))

    def test_directory_as_diff(self):
        # Used to escape as an unhandled IsADirectoryError and exit 1.
        empty = REPO / "tests" / "_tmp_empty_dir"
        empty.mkdir(exist_ok=True)
        try:
            proc = run_cli("--diff", str(empty), "--dry-run")
        finally:
            empty.rmdir()
        self.assertEqual(proc.returncode, 3, combined(proc))
        self.assertIn("is a directory, not a diff file", proc.stderr)
        self.assertNotIn("Traceback", combined(proc))

    def test_input_that_is_not_a_diff(self):
        # Used to produce a confident "No findings" comment body for a change
        # that was never read.
        bad = REPO / "tests" / "_tmp_not_a_diff.diff"
        bad.write_text("this is not a unified diff\njust prose\n", encoding="utf-8")
        try:
            proc = run_cli("--diff", str(bad), "--dry-run")
        finally:
            bad.unlink()
        self.assertEqual(proc.returncode, 3, combined(proc))
        self.assertIn("not a unified diff", proc.stderr)
        self.assertNotIn("Traceback", combined(proc))

    def test_empty_diff_is_not_an_error(self):
        empty = REPO / "tests" / "_tmp_empty.diff"
        empty.write_text("", encoding="utf-8")
        try:
            proc = run_cli("--diff", str(empty), "--dry-run")
        finally:
            empty.unlink()
        self.assertEqual(proc.returncode, 0, combined(proc))
        self.assertIn("the diff is empty; nothing to review", proc.stderr)

    def test_rules_glob_that_matches_nothing(self):
        proc = run_cli("--diff", str(SAMPLE), "--rules", "/root/nowhere/*.md", "--plan-only")
        self.assertEqual(proc.returncode, 3, combined(proc))
        self.assertIn("no rule files matched", proc.stderr)
        self.assertNotIn("Traceback", combined(proc))

    def test_invalid_severity(self):
        proc = run_cli("--diff", str(SAMPLE), "--min-severity", "nonsense")
        self.assertEqual(proc.returncode, 2, combined(proc))
        self.assertIn("invalid choice", proc.stderr)
        self.assertNotIn("Traceback", combined(proc))


class PackageDataTests(unittest.TestCase):
    def test_rule_files_ship_inside_the_package(self):
        from pr_review_lint import cli

        rules_dir = Path(cli._HERE) / "rules"
        self.assertTrue(rules_dir.is_dir(), "not found: %s" % rules_dir)
        self.assertEqual(rules_dir.parent.name, "pr_review_lint")
        self.assertEqual(sorted(p.name for p in rules_dir.glob("*.md")), EXPECTED_RULES)

    def test_default_rule_lookup_includes_the_packaged_glob(self):
        from pr_review_lint import cli

        found = cli.discover_rule_files([cli.BUNDLED_RULES_GLOB])
        self.assertEqual(sorted(p.name for p in found), EXPECTED_RULES)


class OfflineProofTests(unittest.TestCase):
    def test_prove_offline_passes(self):
        """The repo's own proof harness: --dry-run and --plan-only open no
        socket, and malformed model output is rejected with zero GitHub calls."""
        proc = subprocess.run(
            [sys.executable, "-B", str(REPO / "prove_offline.py")],
            cwd=str(REPO), capture_output=True, text=True, timeout=600,
        )
        self.assertEqual(proc.returncode, 0, combined(proc))
        self.assertIn("All claims demonstrated", proc.stdout)
        self.assertIn("RESULT   : PASS", proc.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
