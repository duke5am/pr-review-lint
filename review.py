#!/usr/bin/env python3
"""review.py -- first-pass pull-request review from your own markdown rules.

This wrapper exists so `python3 review.py --diff pr.diff --dry-run` keeps
working from a clone. The same CLI is installed as the `pr-review-lint`
console script; the implementation lives in `pr_review_lint/cli.py` so the
installed package and the checkout are the same code, not two versions of it.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from pr_review_lint.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
