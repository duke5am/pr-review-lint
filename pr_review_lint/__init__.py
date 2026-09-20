"""pr_review_lint -- rule-driven first-pass review of a pull request diff.

The implementation lives in :mod:`pr_review_lint.cli`; the `pr-review-lint`
console script and the repository-root `review.py` wrapper both call
``pr_review_lint.cli.main``, so there is one copy of the code, not two.

The four example rule files ship as package data in ``pr_review_lint/rules/``
and are the last resort in the default ``--rules`` lookup, so the installed
command works from any directory.
"""
