#!/usr/bin/env python3
"""
pr-review-lint -- rule-driven first-pass code review for pull requests.

This is the implementation of the `pr-review-lint` console script. The
repo-root `review.py` is a thin wrapper around `main()`, so
`python3 review.py ...` keeps working from a clone and the installed script
runs exactly the same code.

This is the engine behind the ".github/workflows/ai-review.yml" workflow in this
pack, but it is deliberately a normal command line program: it reads a diff from
a file or from stdin, so you can run it locally and iterate on your rules
without pushing anything to GitHub.

Design priorities, in order:

  1. NOISE CONTROL.  A bot that posts forty nitpicks gets muted, and then it has
     negative value because nobody reads the one comment that mattered.  So the
     engine applies a severity floor (`--min-severity`) and a hard cap on the
     number of findings per run (`--max-findings`).  Everything below the floor
     or past the cap is dropped before anything is posted.

  2. FAIL LOUDLY.  A missing API key, a malformed model response, or an HTTP
     error is a hard, visible failure.  It never degrades into "post a comment
     saying the code looks fine".

  3. IDEMPOTENCE.  The posted comment carries a hidden HTML marker.  A later run
     finds its own previous comment by that marker and edits it in place,
     instead of appending another comment to the thread.

Dependencies: Python 3.8+ standard library only (urllib, json, re, argparse,
dataclasses, difflib, pathlib).  No pip install required, so it runs on a bare
GitHub-hosted runner with no setup step.

Exit codes:
  0  success (including "nothing to review", which is not an error)
  2  usage error
  3  configuration error (missing API key, missing rules, etc.)
  4  the model response could not be parsed (malformed output was rejected)
  5  transport / HTTP error
  6  GitHub API error
"""

from __future__ import annotations

import argparse
import json
import os
import re
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

VERSION = "0.1.0"

#: Where the package, and therefore the bundled example rule files, live.
_HERE = Path(__file__).resolve().parent
BUNDLED_RULES_GLOB = str(_HERE / "rules" / "*.md")

#: Hidden marker written into every comment this tool posts.  It is an HTML
#: comment, so GitHub renders the comment without showing it, but the string is
#: present in the raw body returned by the API -- which is how a later run finds
#: *its own* comment and updates it instead of adding another one.
COMMENT_MARKER = "<!-- ai-code-review-bot:v1 -->"

SEVERITIES: Tuple[str, ...] = ("critical", "high", "medium", "low", "nit")
SEVERITY_RANK: Dict[str, int] = {name: i for i, name in enumerate(SEVERITIES)}

#: Response format the model is instructed to emit.  Parsed strictly: exactly
#: these seven keys, in exactly this order, once per finding.
FINDING_BEGIN = "@@@FINDING"
FINDING_END = "@@@END"
FINDING_FIELDS: Tuple[str, ...] = (
    "SEVERITY",
    "FILE",
    "LINE",
    "RULE",
    "MESSAGE",
    "SUGGESTION",
    "CONFIDENCE",
)

#: File names / suffixes that are never worth spending review criteria on.
LOCKFILE_NAMES = frozenset(
    {
        "package-lock.json",
        "npm-shrinkwrap.json",
        "yarn.lock",
        "pnpm-lock.yaml",
        "pnpm-lock.yml",
        "bun.lockb",
        "bun.lock",
        "composer.lock",
        "Gemfile.lock",
        "Pipfile.lock",
        "poetry.lock",
        "uv.lock",
        "Cargo.lock",
        "go.sum",
        "packages.lock.json",
        "packages.lock.yaml",
        "pubspec.lock",
        "gradle.lockfile",
        "mix.lock",
        "flake.lock",
        "Podfile.lock",
        "Cartfile.resolved",
        "paket.lock",
        "project.assets.json",
    }
)

#: Path fragments that mark build output, vendored code, or caches.  Matching is
#: done on "/"-separated path segments so that "dist" does not match "distance".
GENERATED_PATH_SEGMENTS = frozenset(
    {
        "node_modules",
        "vendor",
        "vendors",
        "dist",
        "build",
        "out",
        "target",
        "coverage",
        ".next",
        ".nuxt",
        ".svelte-kit",
        ".venv",
        "venv",
        "__pycache__",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".tox",
        ".terraform",
        "site-packages",
        "bower_components",
        "jspm_packages",
        "obj",
        "bin",
    }
)

#: Generated-code filename markers (protobuf, ORM migrations autogen, etc).
GENERATED_NAME_MARKERS: Tuple[str, ...] = (
    ".pb.go",
    "_pb2.py",
    "_pb2_grpc.py",
    ".pb.cc",
    ".pb.h",
    ".generated.",
    ".designer.cs",
    ".g.cs",
    ".g.dart",
    ".freezed.dart",
    ".min.js",
    ".min.css",
    ".bundle.js",
    ".map",
    ".snap",
)

#: Extensions that are binary or otherwise not reviewable as text.
BINARY_SUFFIXES = frozenset(
    {
        ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".ico", ".webp", ".tiff", ".svgz",
        ".pdf", ".zip", ".tar", ".gz", ".tgz", ".bz2", ".xz", ".7z", ".rar",
        ".jar", ".war", ".ear", ".class", ".pyc", ".pyo", ".so", ".dylib", ".dll",
        ".exe", ".o", ".a", ".obj", ".lib", ".wasm", ".bin", ".dat", ".db",
        ".sqlite", ".sqlite3", ".mp3", ".mp4", ".mov", ".avi", ".webm", ".ogg",
        ".wav", ".flac", ".ttf", ".otf", ".woff", ".woff2", ".eot", ".psd", ".ai",
        ".sketch", ".fig", ".parquet", ".avro", ".pkl", ".npy", ".npz", ".h5",
    }
)

DEFAULT_ENDPOINT = "https://api.openai.com/v1/chat/completions"
DEFAULT_MODEL = "gpt-4o-mini"
DEFAULT_API_STYLE = "openai"
DEFAULT_MIN_SEVERITY = "medium"
DEFAULT_MAX_FINDINGS = 8
DEFAULT_CHUNK_CHARS = 24000
DEFAULT_TIMEOUT = 120
DEFAULT_MAX_RETRIES = 3

#: Total diff size above which we refuse to review rather than silently
#: truncating the middle of a large change.  Override with --max-total-chars.
DEFAULT_MAX_TOTAL_CHARS = 400000

#: Sentinel file path used for findings that are not tied to one file (for
#: example a rule about the shape of the PR as a whole).
GLOBAL_FILE = "(pull request)"


class ReviewError(Exception):
    """Base class for errors this tool reports as a clean message."""

    exit_code = 1


class ConfigError(ReviewError):
    exit_code = 3


class ResponseFormatError(ReviewError):
    """The model returned something we refuse to post."""

    exit_code = 4


class TransportError(ReviewError):
    exit_code = 5


class GitHubError(ReviewError):
    exit_code = 6


# --------------------------------------------------------------------------- #
# Data types
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Finding:
    """One review point, as returned by the model and after filtering."""

    severity: str
    file: str
    line: int
    rule: str
    message: str
    suggestion: str
    confidence: str

    @property
    def rank(self) -> int:
        return SEVERITY_RANK.get(self.severity, len(SEVERITIES))

    def dedupe_key(self) -> Tuple[str, str, int, str]:
        """Key used to drop exact repeats the model sometimes emits."""
        normalised = re.sub(r"\s+", " ", self.message.strip().lower())
        return (
            self.severity,
            self.file.strip(),
            self.line,
            normalised[:200],
        )


@dataclass
class DiffFile:
    """One file's section of a unified diff."""

    path: str
    text: str
    added: int = 0
    removed: int = 0
    skipped_reason: Optional[str] = None

    @property
    def added_lines(self) -> List[str]:
        out = []
        for raw in self.text.splitlines():
            if raw.startswith("+") and not raw.startswith("+++"):
                out.append(raw[1:])
        return out


@dataclass
class Chunk:
    """A group of files sent to the model in a single request."""

    index: int
    files: List[DiffFile] = field(default_factory=list)

    @property
    def text(self) -> str:
        return "\n".join(f.text for f in self.files)

    @property
    def paths(self) -> List[str]:
        return [f.path for f in self.files]

    @property
    def char_count(self) -> int:
        return len(self.text)


# --------------------------------------------------------------------------- #
# Diff parsing
# --------------------------------------------------------------------------- #

#: Splits the `diff --git a/x b/x` header into two paths. Written by hand rather
#: than with a two-group regex because git quotes paths that contain spaces,
#: producing headers such as:  diff --git "a/my docs/read me.md" "b/my docs/read me.md"
_DIFF_HEADER_PREFIX = "diff --git "
_HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+\d+(?:,\d+)? @@")
_PLUS_PATH_RE = re.compile(r"^\+\+\+ (?P<path>.+?)\s*$")
_MINUS_PATH_RE = re.compile(r"^--- (?P<path>.+?)\s*$")


def _split_header_paths(line: str) -> Tuple[str, str]:
    """Return the (a, b) paths from a `diff --git` header line."""
    rest = line[len(_DIFF_HEADER_PREFIX):].strip()
    if rest.startswith('"'):
        # Quoted form: read up to the closing quote, then the remainder.
        end = rest.find('"', 1)
        if end == -1:
            return "", ""
        first = rest[: end + 1]
        remainder = rest[end + 1:].strip()
        return first, remainder
    # Unquoted form: git only omits quotes when there is no whitespace.
    parts = rest.split(" ", 1)
    if len(parts) == 1:
        return parts[0], parts[0]
    return parts[0], parts[1].strip()


def is_diff_header(line: str) -> bool:
    return line.startswith(_DIFF_HEADER_PREFIX)


def _clean_diff_path(raw: str) -> str:
    """Turn a diff header path into a repo-relative path.

    Handles the forms git emits: `b/src/app.py`, `"b/odd name.py"`, and
    `/dev/null` for added or deleted files.
    """
    path = raw.strip()
    if path.startswith('"') and path.endswith('"') and len(path) >= 2:
        path = path[1:-1]
    if path == "/dev/null":
        return ""
    if path.startswith("a/") or path.startswith("b/"):
        path = path[2:]
    return path


def split_diff_sections(diff_text: str) -> List[str]:
    """Split a unified diff into one string per file.

    Splitting is line based and only recognises a header when it starts at
    column zero, so text *inside* a hunk that happens to contain the words
    "diff --git" cannot start a new section.
    """
    sections: List[str] = []
    current: List[str] = []
    for line in diff_text.splitlines():
        if is_diff_header(line):
            if current:
                sections.append("\n".join(current))
            current = [line]
        else:
            if current:
                current.append(line)
    if current:
        sections.append("\n".join(current))
    return sections


def parse_diff(diff_text: str) -> List[DiffFile]:
    """Parse a unified diff into DiffFile objects, preserving file order.

    Renames are collapsed to the new path.  Sections with no `+++`/`---` header
    (for example a bare mode change) are kept with the path from the
    `diff --git` line so that they can still be reported as skipped.
    """
    files: List[DiffFile] = []
    for section in split_diff_sections(diff_text):
        lines = section.splitlines()
        if not lines:
            continue

        header_path = ""
        if is_diff_header(lines[0]):
            _a, b_path = _split_header_paths(lines[0])
            header_path = _clean_diff_path(b_path)

        new_path = ""
        old_path = ""
        added = 0
        removed = 0
        in_hunk = False
        for line in lines[1:]:
            if _HUNK_RE.match(line):
                in_hunk = True
                continue
            if line.startswith("+++ ") and not new_path:
                new_path = _clean_diff_path(_PLUS_PATH_RE.match(line).group("path"))
                continue
            if line.startswith("--- ") and not old_path:
                old_path = _clean_diff_path(_MINUS_PATH_RE.match(line).group("path"))
                continue
            # Only count changes that are inside a hunk. Metadata such as
            # "index ...", "new file mode ..." and "--- a/x" must not be counted.
            if not in_hunk:
                continue
            if line.startswith("+"):
                added += 1
            elif line.startswith("-"):
                removed += 1

        path = new_path or header_path or old_path
        files.append(
            DiffFile(path=path, text=section, added=added, removed=removed)
        )
    return files


def classify_skip(df: DiffFile) -> Optional[str]:
    """Return a human readable reason to skip this file, or None to review it.

    Skipping is what keeps the review focused.  Lockfiles, vendored trees,
    build output, minified bundles and binaries are all things a human reviewer
    would scroll past, so the bot should not spend tokens or attention on them.
    """
    path = df.path
    if not path:
        return "no file path in diff header"
    if path == "/dev/null":
        return "deleted file"
    if "+++ /dev/null" in df.text or "\n+++ /dev/null" in df.text:
        return "deleted file"

    lowered = path.lower()
    name = lowered.rsplit("/", 1)[-1]
    segments = [seg for seg in lowered.split("/") if seg]

    if name in LOCKFILE_NAMES or name.endswith(".lock"):
        return "lockfile"
    if any(seg in GENERATED_PATH_SEGMENTS for seg in segments[:-1]):
        return "generated / vendored directory"
    if any(marker in lowered for marker in GENERATED_NAME_MARKERS):
        return "generated or minified artifact"
    if any(name.endswith(suffix) for suffix in BINARY_SUFFIXES):
        return "binary file"
    # Git reports binary content two ways: a "Binary files ... differ" line in a
    # normal diff, or a "GIT binary patch" block when --binary is in effect.
    if "Binary files " in df.text or "GIT binary patch" in df.text:
        return "binary file"
    # A NUL byte in what is presented as a text diff means it is not text.
    if "\x00" in df.text:
        return "binary file"
    if lowered.endswith(".ipynb"):
        return "notebook output"
    # Sourcemaps and other single-line monsters are useless to review.
    longest = 0
    for line in df.text.splitlines():
        if len(line) > longest:
            longest = len(line)
    if longest > 20000:
        return "contains an extremely long line (minified or data blob)"
    return None


def split_oversized(text: str, budget: int) -> List[str]:
    """Split one file's diff text into pieces of at most `budget` characters.

    Cuts happen on line boundaries so the diff handed to the model is never
    truncated mid-line.  A single line longer than the budget is emitted whole
    (it is already unusual, and truncating it would be misleading).
    """
    if budget <= 0 or len(text) <= budget:
        return [text]
    pieces: List[str] = []
    current: List[str] = []
    size = 0
    for line in text.splitlines():
        line_len = len(line) + 1
        if current and size + line_len > budget:
            pieces.append("\n".join(current))
            current = []
            size = 0
        current.append(line)
        size += line_len
    if current:
        pieces.append("\n".join(current))
    return pieces


def chunk_files(files: Sequence[DiffFile], chunk_chars: int = DEFAULT_CHUNK_CHARS) -> List[Chunk]:
    """Group reviewable files into chunks, splitting any file that is too big.

    Files are never reordered, and a file is preferred to stay whole: chunk
    boundaries fall between files unless a single file exceeds the budget.
    """
    chunks: List[Chunk] = []
    pending: List[DiffFile] = []
    pending_size = 0

    def flush() -> None:
        nonlocal pending, pending_size
        if pending:
            chunks.append(Chunk(index=len(chunks), files=pending))
            pending = []
            pending_size = 0

    for df in files:
        if df.skipped_reason is not None:
            continue
        pieces = split_oversized(df.text, chunk_chars)
        if len(pieces) == 1:
            size = len(df.text) + 1
            if pending and pending_size + size > chunk_chars:
                flush()
            pending.append(df)
            pending_size += size
        else:
            # Large file: flush what we have, then emit each piece on its own.
            flush()
            for piece_index, piece in enumerate(pieces):
                part = DiffFile(
                    path=df.path,
                    text=piece,
                    added=df.added,
                    removed=df.removed,
                )
                if piece_index > 0:
                    part.path = f"{df.path} (part {piece_index + 1})"
                chunks.append(Chunk(index=len(chunks), files=[part]))
    flush()
    return chunks


def prepare_files(diff_text: str) -> List[DiffFile]:
    """Parse a diff and annotate every file with a skip reason where relevant."""
    files = parse_diff(diff_text)
    for df in files:
        df.skipped_reason = classify_skip(df)
    return files


# --------------------------------------------------------------------------- #
# Rules
# --------------------------------------------------------------------------- #


def load_rules(rule_paths: Sequence[Path]) -> List[Tuple[str, str]]:
    """Read the team's rule files.  Returns (name, text) pairs, in order."""
    loaded: List[Tuple[str, str]] = []
    for path in rule_paths:
        if not path.exists():
            raise ConfigError(f"rule file not found: {path}")
        if path.is_dir():
            raise ConfigError(f"rule path is a directory, expected a file: {path}")
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise ConfigError(f"rule file is not valid UTF-8: {path} ({exc})") from exc
        if not text.strip():
            raise ConfigError(f"rule file is empty: {path}")
        loaded.append((path.name, text))
    if not loaded:
        raise ConfigError(
            "no rule files were loaded. The whole point of this bot is reviewing "
            "against *your* written rules; pass at least one file via "
            "RULES_GLOB / --rules."
        )
    return loaded


def discover_rule_files(patterns: Sequence[str]) -> List[Path]:
    """Expand glob patterns (as given on the command line or in RULES_GLOB)."""
    found: List[Path] = []
    for pattern in patterns:
        if not pattern.strip():
            continue
        p = Path(pattern)
        if p.is_file():
            found.append(p)
            continue
        matches = sorted(Path().glob(pattern)) if not p.is_absolute() else sorted(
            Path(p.anchor).glob(str(p)[len(p.anchor):])
        )
        if not matches:
            matches = sorted(Path(".").glob("**/" + pattern))
        found.extend(m for m in matches if m.is_file())
    # De-duplicate while preserving order.
    seen = set()
    unique: List[Path] = []
    for path in found:
        key = str(path.resolve())
        if key not in seen:
            seen.add(key)
            unique.append(path)
    return unique


def build_system_prompt(rules: Sequence[Tuple[str, str]], min_severity: str) -> str:
    """Compose the review criteria from the team's own rule files."""
    parts = [
        "You are a senior engineer performing a focused FIRST-PASS review of a "
        "pull request diff. A human reviewer will read your output and decide "
        "what to act on.",
        "",
        "You review ONLY against the team's written rules below. Do not invent "
        "house style, do not ask for a different framework, and do not comment "
        "on things no rule covers.",
        "",
        "Rules for your output:",
        f"- Report only findings of severity {min_severity} or worse.",
        f"- Allowed severities, most to least severe: {', '.join(SEVERITIES)}.",
        "- Do not report whitespace, formatting, or import ordering. Assume a "
        "formatter and a linter already run in CI; repeating them wastes the "
        "reviewer's attention.",
        "- Every finding must cite the rule it comes from and include a "
        "concrete suggestion. If you cannot name the rule, do not report it.",
        "- Prefer a few findings that matter over a long list. If nothing in "
        "the diff violates a rule, report nothing.",
        "- Do not repeat the same finding for every occurrence; report it once, "
        "at the first occurrence.",
        "",
        f"Output format. Emit zero or more findings. Each finding is exactly "
        f"seven lines, starting with {FINDING_BEGIN} and ending with "
        f"{FINDING_END}, in this key order:",
        "",
        FINDING_BEGIN,
        "SEVERITY: <one of " + "|".join(SEVERITIES) + ">",
        "FILE: <path exactly as it appears in the diff>",
        "LINE: <line number in the new file, or 0 if not a single line>",
        "RULE: <the name of the rule file or rule section you are applying>",
        "MESSAGE: <one or two sentences: what is wrong and why it matters>",
        "SUGGESTION: <the concrete change you want>",
        "CONFIDENCE: <high|medium|low>",
        FINDING_END,
        "",
        "Output nothing else: no preamble, no summary, no markdown fences, no "
        "trailing notes. If there are no findings, output exactly this one "
        "line and nothing else:",
        "NO_FINDINGS",
    ]
    prompt = "\n".join(parts)

    prompt += "\n\n" + "=" * 72 + "\nTHE TEAM'S RULES\n" + "=" * 72 + "\n"
    for name, text in rules:
        prompt += f"\n--- BEGIN RULE FILE: {name} ---\n{text.strip()}\n--- END RULE FILE: {name} ---\n"
    return prompt


# --------------------------------------------------------------------------- #
# Response parsing (strict)
# --------------------------------------------------------------------------- #

_BLOCK_RE = re.compile(
    r"^" + re.escape(FINDING_BEGIN) + r"\n"
    + r"SEVERITY: (?P<severity>[^\n]*)\n"
    + r"FILE: (?P<file>[^\n]*)\n"
    + r"LINE: (?P<line>[^\n]*)\n"
    + r"RULE: (?P<rule>[^\n]*)\n"
    + r"MESSAGE: (?P<message>[^\n]*)\n"
    + r"SUGGESTION: (?P<suggestion>[^\n]*)\n"
    + r"CONFIDENCE: (?P<confidence>[^\n]*)\n"
    + re.escape(FINDING_END) + r"$",
    re.MULTILINE,
)

NO_FINDINGS_TOKEN = "NO_FINDINGS"

#: Phrases that indicate the model ignored the format and wrote prose.
_PROSE_SMELLS = (
    "here are",
    "here is",
    "i found",
    "summary:",
    "```",
    "as an ai",
    "let me know",
)


def _strip_fences(text: str) -> str:
    """Remove a markdown code fence if the model wrapped its whole answer."""
    stripped = text.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        if len(lines) >= 2:
            lines = lines[1:]
            if lines and lines[-1].strip().startswith("```"):
                lines = lines[:-1]
            return "\n".join(lines).strip()
    return stripped


def parse_findings(response_text: str) -> List[Finding]:
    """Parse the model's response into Findings, or raise ResponseFormatError.

    This is intentionally unforgiving.  A fuzzy parser that guesses what the
    model meant is how a review bot ends up posting nonsense onto a pull
    request, so anything that is not exactly the documented format is rejected
    with a message that says what was wrong and shows a snippet of the raw text.
    """
    if response_text is None or not response_text.strip():
        raise ResponseFormatError(
            "the model returned an empty response; nothing can be posted from it"
        )

    text = _strip_fences(response_text)

    if text.strip() == NO_FINDINGS_TOKEN:
        return []

    body = text.strip()
    if not body.startswith(FINDING_BEGIN):
        lowered = body.lower()
        for smell in _PROSE_SMELLS:
            if smell in lowered:
                raise ResponseFormatError(
                    "the model replied with prose instead of the delimited "
                    "format, so the output was rejected. First 300 characters "
                    f"of the response:\n{body[:300]!r}"
                )
        raise ResponseFormatError(
            "the response does not start with " + FINDING_BEGIN + ". Output was "
            f"rejected. First 300 characters:\n{body[:300]!r}"
        )

    # Every block must be matched exactly; deleting matched spans must leave no
    # residue. Any leftover means stray text, a missing key, a reordered key, or
    # an unknown key -- all rejected.
    residue = _BLOCK_RE.sub("", body)
    if residue.strip():
        raise ResponseFormatError(
            "the response contained text outside the expected finding blocks, or "
            "a block did not use the exact key order "
            f"({' -> '.join(FINDING_FIELDS)}). Rejected. Residue after removing "
            f"well-formed blocks:\n{residue.strip()[:300]!r}"
        )

    findings: List[Finding] = []
    for match in _BLOCK_RE.finditer(body):
        severity = match.group("severity").strip().lower()
        if severity not in SEVERITY_RANK:
            raise ResponseFormatError(
                f"finding used an unknown severity {severity!r}; allowed values "
                f"are {', '.join(SEVERITIES)}"
            )
        raw_line = match.group("line").strip()
        if not re.fullmatch(r"\d+", raw_line or ""):
            raise ResponseFormatError(
                f"finding LINE must be a non-negative integer, got {raw_line!r}"
            )
        confidence = match.group("confidence").strip().lower()
        if confidence not in ("high", "medium", "low"):
            raise ResponseFormatError(
                "finding CONFIDENCE must be high, medium or low, got "
                f"{confidence!r}"
            )
        message = match.group("message").strip()
        if len(message) < 8:
            raise ResponseFormatError(
                f"finding MESSAGE is too short to be useful ({message!r})"
            )
        findings.append(
            Finding(
                severity=severity,
                file=match.group("file").strip(),
                line=int(raw_line),
                rule=match.group("rule").strip(),
                message=message,
                suggestion=match.group("suggestion").strip(),
                confidence=confidence,
            )
        )
    return findings


# --------------------------------------------------------------------------- #
# Noise control
# --------------------------------------------------------------------------- #


def filter_findings(
    findings: Iterable[Finding],
    min_severity: str = DEFAULT_MIN_SEVERITY,
    max_findings: int = DEFAULT_MAX_FINDINGS,
    changed_paths: Optional[Sequence[str]] = None,
) -> Tuple[List[Finding], List[Finding]]:
    """Apply the severity floor and the noise cap.

    Returns (kept, dropped).  This is the single most important function in the
    product: it is what keeps the bot's comment short enough that people read
    it.

    Order of operations:
      1. drop findings that do not name a rule or a file
      2. drop findings whose severity is below the floor
      3. drop findings pointing at a file that is not in this diff
         (guards against a model inventing paths)
      4. de-duplicate exact repeats
      5. sort most severe first, then cap the total
    """
    if min_severity not in SEVERITY_RANK:
        raise ConfigError(
            f"min severity {min_severity!r} is not one of {', '.join(SEVERITIES)}"
        )
    if max_findings < 0:
        raise ConfigError("max findings cannot be negative")

    # SEVERITY_RANK is ordered most severe first, so rank 0 == "critical" and a
    # LARGER rank means LESS severe. A finding is reported when its rank is at
    # most the floor's rank -- that is, when it is at least as severe as the
    # configured minimum.
    floor = SEVERITY_RANK[min_severity]
    known = None
    if changed_paths is not None:
        known = set()
        for path in changed_paths:
            known.add(path)
            # chunked large files carry a " (part N)" suffix
            known.add(re.sub(r" \(part \d+\)$", "", path))

    kept: List[Finding] = []
    dropped: List[Finding] = []
    seen = set()

    for finding in findings:
        if not finding.rule:
            dropped.append(finding)
            continue
        if not finding.file:
            dropped.append(finding)
            continue
        if finding.rank > floor:
            dropped.append(finding)
            continue
        if known is not None:
            normalised = finding.file[2:] if finding.file.startswith("./") else finding.file
            if (
                normalised not in known
                and finding.file not in known
                and finding.file != GLOBAL_FILE
            ):
                dropped.append(finding)
                continue
        key = finding.dedupe_key()
        if key in seen:
            dropped.append(finding)
            continue
        seen.add(key)
        kept.append(finding)

    kept.sort(key=lambda f: (f.rank, f.file, f.line))
    if len(kept) > max_findings:
        dropped.extend(kept[max_findings:])
        kept = kept[:max_findings]
    return kept, dropped


# --------------------------------------------------------------------------- #
# Comment rendering
# --------------------------------------------------------------------------- #

_SEVERITY_BADGE = {
    "critical": "CRITICAL",
    "high": "HIGH",
    "medium": "MEDIUM",
    "low": "LOW",
    "nit": "NIT",
}


def render_comment(
    findings: Sequence[Finding],
    marker: str = COMMENT_MARKER,
    dropped_count: int = 0,
    files_reviewed: int = 0,
    files_skipped: int = 0,
    min_severity: str = DEFAULT_MIN_SEVERITY,
    max_findings: int = DEFAULT_MAX_FINDINGS,
    header: str = "Automated first pass",
    report_url: Optional[str] = None,
) -> str:
    """Render the markdown body of the review comment.

    The marker is always the first line so that a later run can find this
    comment again.
    """
    lines: List[str] = [marker]
    lines.append(f"### {header}")
    lines.append("")

    if not findings:
        lines.append(
            "No findings at or above the `" + min_severity + "` threshold "
            f"across {files_reviewed} changed file(s)."
        )
        if dropped_count:
            lines.append("")
            lines.append(
                f"_{dropped_count} lower-severity point(s) were filtered out by "
                f"the noise cap; nothing here needs a decision._"
            )
    else:
        lines.append(
            f"**{len(findings)} finding(s)** needing a decision, ordered most "
            "severe first. These come from this repository's own rule files, "
            "not from a generic linter."
        )
        lines.append("")
        for finding in findings:
            badge = _SEVERITY_BADGE.get(finding.severity, finding.severity.upper())
            location = finding.file
            if finding.line:
                location += f":{finding.line}"
            lines.append(f"#### `{badge}` `{location}`")
            lines.append("")
            lines.append(finding.message)
            lines.append("")
            if finding.suggestion:
                lines.append(f"**Suggested change:** {finding.suggestion}")
                lines.append("")
            lines.append(
                f"<sub>rule: {finding.rule} &middot; confidence: "
                f"{finding.confidence}</sub>"
            )
            lines.append("")

    lines.append("---")
    footer = (
        f"<sub>First pass only &mdash; this is not a substitute for human review. "
        f"Threshold: `{min_severity}` and above, maximum {max_findings} "
        f"finding(s) per run, {files_reviewed} file(s) reviewed, "
        f"{files_skipped} skipped (lockfiles, generated, binary). "
        f"{dropped_count} point(s) below the threshold were withheld."
    )
    if report_url:
        footer += f" Full report: {report_url}."
    footer += (
        " Do not act on these findings automatically: repository content is "
        "untrusted input to the reviewer.</sub>"
    )
    lines.append(footer)
    return "\n".join(lines)


def find_existing_comment(
    comments: Sequence[dict], marker: str = COMMENT_MARKER
) -> Optional[dict]:
    """Return the newest comment authored by this tool, or None.

    Matching is by marker substring, which is why the marker must be injected
    into the body by this program and never be something a human would type.
    """
    for comment in reversed(list(comments)):
        body = comment.get("body") or ""
        if marker in body:
            return comment
    return None


# --------------------------------------------------------------------------- #
# GitHub client
# --------------------------------------------------------------------------- #

_PR_URL_RE = re.compile(
    r"^https?://[^/]+/(?P<owner>[^/]+)/(?P<repo>[^/]+)/pull/(?P<number>\d+)/?$"
)


def parse_pr_url(url: str) -> Tuple[str, str, int]:
    match = _PR_URL_RE.match(url.strip())
    if not match:
        raise ConfigError(
            f"cannot parse pull request URL {url!r}; expected "
            "https://github.com/<owner>/<repo>/pull/<number>"
        )
    return match.group("owner"), match.group("repo"), int(match.group("number"))


def _ssl_context() -> ssl.SSLContext:
    return ssl.create_default_context()


def http_request(
    url: str,
    method: str = "GET",
    headers: Optional[Dict[str, str]] = None,
    body: Optional[bytes] = None,
    timeout: int = DEFAULT_TIMEOUT,
    max_retries: int = DEFAULT_MAX_RETRIES,
    retry_statuses: Sequence[int] = (429, 500, 502, 503, 504),
) -> Tuple[int, str]:
    """Minimal HTTPS helper on top of urllib, with bounded retries.

    Returns (status_code, response_text).  Raises TransportError for network
    level failures; HTTP error statuses are returned to the caller so that each
    API can interpret them (for example 404 versus 403 on the GitHub API).
    """
    request_headers = {"User-Agent": f"ai-code-review-bot/{VERSION}"}
    if headers:
        request_headers.update(headers)

    last_error: Optional[Exception] = None
    for attempt in range(1, max_retries + 1):
        req = urllib.request.Request(
            url=url, data=body, headers=request_headers, method=method
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=_ssl_context()) as resp:
                return resp.status, resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            payload = exc.read().decode("utf-8", errors="replace")
            if exc.code in retry_statuses and attempt < max_retries:
                retry_after = exc.headers.get("Retry-After") if exc.headers else None
                try:
                    delay = float(retry_after) if retry_after else 2.0 ** attempt
                except (TypeError, ValueError):
                    delay = 2.0 ** attempt
                delay = min(delay, 30.0)
                sys.stderr.write(
                    f"[review] HTTP {exc.code} from {url}; retrying in {delay:.0f}s "
                    f"(attempt {attempt}/{max_retries})\n"
                )
                time.sleep(delay)
                last_error = exc
                continue
            return exc.code, payload
        except (urllib.error.URLError, TimeoutError, ssl.SSLError, OSError) as exc:
            if attempt < max_retries:
                delay = 2.0 ** attempt
                sys.stderr.write(
                    f"[review] network error talking to {url}: {exc}; retrying in "
                    f"{delay:.0f}s (attempt {attempt}/{max_retries})\n"
                )
                time.sleep(delay)
                last_error = exc
                continue
            raise TransportError(f"network error talking to {url}: {exc}") from exc

    raise TransportError(f"giving up on {url} after {max_retries} attempts: {last_error}")


class GitHubClient:
    """The two or three GitHub REST calls this tool actually needs."""

    def __init__(
        self,
        repo: str,
        token: str,
        api_url: str = "https://api.github.com",
        timeout: int = DEFAULT_TIMEOUT,
    ) -> None:
        if not token:
            raise ConfigError(
                "GITHUB_TOKEN is empty. The workflow passes "
                "secrets.GITHUB_TOKEN; locally, export a token with "
                "'pull requests: write' on this repository."
            )
        self.repo = repo
        self.token = token
        self.api_url = api_url.rstrip("/")
        self.timeout = timeout

    def _request(self, method: str, path: str, payload: Optional[dict] = None) -> object:
        url = f"{self.api_url}{path}"
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        body = None
        if payload is not None:
            body = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        status, text = http_request(
            url, method=method, headers=headers, body=body, timeout=self.timeout
        )
        if status >= 400:
            detail = text
            try:
                parsed = json.loads(text)
                if isinstance(parsed, dict) and parsed.get("message"):
                    detail = parsed["message"]
            except json.JSONDecodeError:
                pass
            raise GitHubError(
                f"GitHub API {method} {path} failed with HTTP {status}: "
                f"{str(detail)[:400]}"
            )
        if not text.strip():
            return None
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise GitHubError(
                f"GitHub API {method} {path} returned non-JSON (HTTP {status}): "
                f"{text[:200]!r}"
            ) from exc

    def list_pr_comments(self, number: int) -> List[dict]:
        """All issue comments on the PR, following pagination.

        Issue comments are the PR conversation thread. Review comments are
        inline-on-the-diff and are a different endpoint, so a comment this tool
        posts is the single issue comment found here.
        """
        comments: List[dict] = []
        page = 1
        while page <= 10:
            batch = self._request(
                "GET",
                f"/repos/{self.repo}/issues/{number}/comments?per_page=100&page={page}",
            )
            if not isinstance(batch, list):
                raise GitHubError(
                    "unexpected response shape listing PR comments (expected a JSON array)"
                )
            comments.extend(batch)
            if len(batch) < 100:
                break
            page += 1
        return comments

    def create_comment(self, number: int, body: str) -> dict:
        result = self._request(
            "POST", f"/repos/{self.repo}/issues/{number}/comments", {"body": body}
        )
        if not isinstance(result, dict):
            raise GitHubError("unexpected response shape creating the comment")
        return result

    def update_comment(self, comment_id: int, body: str) -> dict:
        result = self._request(
            "PATCH", f"/repos/{self.repo}/issues/comments/{comment_id}", {"body": body}
        )
        if not isinstance(result, dict):
            raise GitHubError("unexpected response shape updating the comment")
        return result

    def upsert_comment(
        self, number: int, body: str, marker: str = COMMENT_MARKER
    ) -> Tuple[str, Optional[str]]:
        """Post the comment, or update our own previous comment in place.

        Returns (action, html_url) where action is "created" or "updated".
        """
        existing = find_existing_comment(self.list_pr_comments(number), marker)
        if existing is not None:
            updated = self.update_comment(int(existing["id"]), body)
            return "updated", updated.get("html_url")
        created = self.create_comment(number, body)
        return "created", created.get("html_url")


# --------------------------------------------------------------------------- #
# LLM client
# --------------------------------------------------------------------------- #


def _estimate_tokens(text: str) -> int:
    """Cheap token estimate: ~4 characters per token. Deliberately rough."""
    return max(1, len(text) // 4)


@dataclass
class LLMSettings:
    """Everything about the model call, all overridable by environment."""

    endpoint: str = DEFAULT_ENDPOINT
    model: str = DEFAULT_MODEL
    api_key: str = ""
    api_style: str = DEFAULT_API_STYLE
    max_output_tokens: int = 1500
    temperature: float = 0.0
    timeout: int = DEFAULT_TIMEOUT
    max_retries: int = DEFAULT_MAX_RETRIES

    @classmethod
    def from_env(cls, env: Optional[Dict[str, str]] = None) -> "LLMSettings":
        env = dict(os.environ if env is None else env)

        def pick(*names: str, default: str = "") -> str:
            for name in names:
                value = env.get(name)
                if value and value.strip():
                    return value.strip()
            return default

        style = pick("AI_REVIEW_API_STYLE", default=DEFAULT_API_STYLE).lower()
        if style not in ("openai", "anthropic"):
            raise ConfigError(
                f"AI_REVIEW_API_STYLE must be 'openai' or 'anthropic', got {style!r}"
            )

        if style == "anthropic":
            default_endpoint = "https://api.anthropic.com/v1/messages"
        else:
            default_endpoint = DEFAULT_ENDPOINT

        def to_int(name: str, default: int) -> int:
            raw = env.get(name, "")
            if not raw.strip():
                return default
            try:
                return int(raw)
            except ValueError as exc:
                raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc

        return cls(
            endpoint=pick(
                "AI_REVIEW_ENDPOINT", "LLM_API_URL", "OPENAI_BASE_URL", default=default_endpoint
            ),
            model=pick("AI_REVIEW_MODEL", "LLM_MODEL", default=DEFAULT_MODEL),
            api_key=pick(
                "AI_REVIEW_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "LLM_API_KEY"
            ),
            api_style=style,
            max_output_tokens=to_int("AI_REVIEW_MAX_OUTPUT_TOKENS", 1500),
            temperature=0.0,
            timeout=to_int("AI_REVIEW_TIMEOUT", DEFAULT_TIMEOUT),
            max_retries=to_int("AI_REVIEW_MAX_RETRIES", DEFAULT_MAX_RETRIES),
        )


class LLMClient:
    """Calls a chat-style HTTPS API and returns the assistant text.

    Two request shapes are supported, both deliberately minimal and both
    documented in docs/SETUP.md.  If your provider differs, check its API
    reference before changing this -- the fields below are the only ones this
    tool assumes exist.
    """

    def __init__(self, settings: LLMSettings, transport=None) -> None:
        self.settings = settings
        # Injectable seam used by the test suite; never used in production.
        self._transport = transport or http_request

    def _build_request(self, system_prompt: str, user_prompt: str) -> Tuple[Dict[str, str], bytes]:
        headers = {"Content-Type": "application/json"}
        if self.settings.api_style == "anthropic":
            headers["x-api-key"] = self.settings.api_key
            headers["anthropic-version"] = "2023-06-01"
            payload = {
                "model": self.settings.model,
                "max_tokens": self.settings.max_output_tokens,
                "system": system_prompt,
                "messages": [{"role": "user", "content": user_prompt}],
            }
        else:
            headers["Authorization"] = f"Bearer {self.settings.api_key}"
            payload = {
                "model": self.settings.model,
                "max_tokens": self.settings.max_output_tokens,
                "temperature": self.settings.temperature,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
            }
        return headers, json.dumps(payload).encode("utf-8")

    @staticmethod
    def _extract_text(payload: object, api_style: str) -> str:
        if not isinstance(payload, dict):
            raise ResponseFormatError(
                "the API returned JSON that is not an object; cannot read the reply"
            )
        if payload.get("error"):
            raise TransportError(f"the API returned an error field: {payload['error']}")
        if api_style == "anthropic":
            blocks = payload.get("content")
            if not isinstance(blocks, list):
                raise ResponseFormatError(
                    "anthropic-style response has no 'content' array"
                )
            texts = [
                block.get("text", "")
                for block in blocks
                if isinstance(block, dict) and block.get("type") == "text"
            ]
            text = "\n".join(t for t in texts if t)
        else:
            choices = payload.get("choices")
            if not isinstance(choices, list) or not choices:
                raise ResponseFormatError(
                    "chat-completions response has no 'choices' array"
                )
            first = choices[0] or {}
            message = first.get("message") or {}
            text = message.get("content") or ""
            if isinstance(text, list):
                # Some providers return content as a list of parts.
                text = "\n".join(
                    part.get("text", "")
                    for part in text
                    if isinstance(part, dict)
                )
        if not isinstance(text, str) or not text.strip():
            raise ResponseFormatError("the API reply contained no text content")
        return text

    def complete(self, system_prompt: str, user_prompt: str) -> str:
        headers, body = self._build_request(system_prompt, user_prompt)
        status, text = self._transport(
            self.settings.endpoint,
            method="POST",
            headers=headers,
            body=body,
            timeout=self.settings.timeout,
            max_retries=self.settings.max_retries,
        )
        if status >= 400:
            raise TransportError(
                f"the model API returned HTTP {status}. Response body (first 400 "
                f"characters): {text[:400]}"
            )
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ResponseFormatError(
                f"the model API returned non-JSON (HTTP {status}): {text[:200]!r}"
            ) from exc
        return self._extract_text(payload, self.settings.api_style)


def build_user_prompt(chunk: Chunk, total_chunks: int, min_severity: str) -> str:
    paths = "\n".join(f"- {p}" for p in chunk.paths)
    return (
        f"Review this diff (part {chunk.index + 1} of {total_chunks}). "
        f"Report only findings of severity {min_severity} or worse.\n\n"
        f"Files in this part:\n{paths}\n\n"
        "```diff\n" + chunk.text + "\n```\n"
    )


# --------------------------------------------------------------------------- #
# Dry run preview
# --------------------------------------------------------------------------- #


def demo_findings() -> List[Finding]:
    """A synthetic finding used only by --dry-run to show the comment layout.

    It is labelled as a sample everywhere it appears, because a dry run that
    printed invented findings as if they were real review output would be worse
    than useless.
    """
    return [
        Finding(
            severity="high",
            file="(dry-run sample)",
            line=0,
            rule="illustration only",
            message="[DRY RUN SAMPLE, not a real finding] This is the shape of a "
            "high-severity finding. A real run replaces this with findings the "
            "model produced from your diff and your rule files.",
            suggestion="Run without --dry-run and with AI_REVIEW_API_KEY set to "
            "see real findings.",
            confidence="high",
        )
    ]


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #


@dataclass
class RunResult:
    files: List[DiffFile] = field(default_factory=list)
    chunks: List[Chunk] = field(default_factory=list)
    findings: List[Finding] = field(default_factory=list)
    dropped: List[Finding] = field(default_factory=list)
    body: str = ""
    action: str = "none"
    comment_url: Optional[str] = None
    chunks_failed: int = 0

    @property
    def reviewed_files(self) -> List[DiffFile]:
        return [f for f in self.files if f.skipped_reason is None]

    @property
    def skipped_files(self) -> List[DiffFile]:
        return [f for f in self.files if f.skipped_reason is not None]


def read_diff(args: argparse.Namespace) -> str:
    """Read the diff from --diff, or from stdin when it is not supplied."""
    if args.diff:
        if args.diff == "-":
            return sys.stdin.read()
        path = Path(args.diff)
        if not path.exists():
            raise ConfigError(f"diff file not found: {path}")
        if path.is_dir():
            raise ConfigError(
                f"--diff is a directory, not a diff file: {path}\n"
                "  Pass the output of `git diff`, for example:\n"
                "    git diff origin/main...HEAD > pr.diff && "
                "pr-review-lint --diff pr.diff --dry-run"
            )
        try:
            return path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            raise ConfigError(f"could not read the diff at {path}: {exc}") from exc
    if sys.stdin.isatty():
        raise ConfigError(
            "no diff provided. Pass --diff <file>, pipe one in "
            "(git diff | python3 review.py), or use --dry-run."
        )
    return sys.stdin.read()


def plan_report(result: RunResult, args: argparse.Namespace) -> str:
    lines = ["[review] review plan", ""]
    lines.append(f"  files in diff   : {len(result.files)}")
    lines.append(f"  will review     : {len(result.reviewed_files)}")
    lines.append(f"  will skip       : {len(result.skipped_files)}")
    lines.append(f"  chunks          : {len(result.chunks)}")
    reviewed = ", ".join(f.path for f in result.reviewed_files) or "(none)"
    lines.append(f"  reviewed paths  : {reviewed}")
    if result.skipped_files:
        lines.append("  skipped:")
        for df in result.skipped_files:
            lines.append(f"    - {df.path}  [{df.skipped_reason}]")
    if result.chunks:
        lines.append("  chunk sizes (characters):")
        for chunk in result.chunks:
            lines.append(f"    - chunk {chunk.index + 1}: {chunk.char_count}  {chunk.paths}")
    lines.append("")
    lines.append(
        f"  threshold       : severity >= {args.min_severity}, "
        f"max {args.max_findings} finding(s)"
    )
    return "\n".join(lines)


def run(args: argparse.Namespace) -> int:
    # --- rules -------------------------------------------------------------
    rule_paths = discover_rule_files(args.rules)
    if not rule_paths:
        raise ConfigError(
            "no rule files matched "
            + ", ".join(repr(p) for p in args.rules)
            + ". Point --rules / RULES_GLOB at your team's markdown rule files."
        )
    rules = load_rules(rule_paths)

    # --- diff --------------------------------------------------------------
    diff_text = read_diff(args)
    if not diff_text.strip():
        sys.stderr.write("[review] the diff is empty; nothing to review\n")
        return 0
    if len(diff_text) > args.max_total_chars:
        raise ConfigError(
            f"the diff is {len(diff_text)} characters, above the "
            f"--max-total-chars limit of {args.max_total_chars}. Refusing to "
            "review a partial diff. Split the change, or raise the limit "
            "deliberately."
        )

    files = prepare_files(diff_text)
    if not files:
        raise ConfigError(
            "the input is not a unified diff: 0 file sections were found, so "
            "nothing could be reviewed.\n"
            f"  first 200 characters: {diff_text[:200]!r}\n"
            "  Expected `git diff` / `git format-patch` output or a .diff file. "
            "`git diff --stat` and `git diff --name-only` are not diffs."
        )
    result = RunResult(files=files)
    result.chunks = chunk_files(result.reviewed_files, args.chunk_chars)

    system_prompt = build_system_prompt(rules, args.min_severity)
    changed_paths = [f.path for f in result.reviewed_files]

    sys.stderr.write(
        f"[review] {len(files)} file(s) in diff, {len(result.reviewed_files)} to "
        f"review, {len(result.skipped_files)} skipped, {len(result.chunks)} chunk(s)\n"
    )

    if args.plan_only or args.dry_run:
        sys.stderr.write(plan_report(result, args) + "\n")

    if not result.chunks:
        result.body = render_comment(
            [],
            dropped_count=0,
            files_reviewed=0,
            files_skipped=len(result.skipped_files),
            min_severity=args.min_severity,
            max_findings=args.max_findings,
            header=args.header,
        )
        if args.dry_run or args.plan_only:
            sys.stdout.write(result.body + "\n")
            return 0
        # Nothing reviewable: post/update a short "nothing to review" comment so
        # a stale previous comment does not linger looking like a live result.
        return _publish(result, args, number=args.pr_number)

    if args.plan_only:
        return 0

    if args.dry_run:
        sys.stdout.write("=" * 72 + "\n")
        sys.stdout.write("DRY RUN: no API call was made and nothing was posted.\n")
        sys.stdout.write("Below is the exact comment body this run would post "
                         "(using a labelled sample finding to show the layout).\n")
        sys.stdout.write("=" * 72 + "\n\n")
        result.body = render_comment(
            demo_findings(),
            dropped_count=0,
            files_reviewed=len(result.reviewed_files),
            files_skipped=len(result.skipped_files),
            min_severity=args.min_severity,
            max_findings=args.max_findings,
            header=args.header + " (dry run)",
        )
        sys.stdout.write(result.body + "\n")
        sys.stdout.write("\n" + "=" * 72 + "\n")
        sys.stdout.write(
            "System prompt that would be sent: "
            f"{len(system_prompt)} characters.\n"
        )
        sys.stdout.write(
            "Estimated request size: "
            f"~{_estimate_tokens(system_prompt) + sum(_estimate_tokens(c.text) for c in result.chunks)} tokens "
            "of input (rough estimate).\n"
        )
        return 0

    # --- API key check happens AFTER the plan is printed, so --dry-run and
    # --- --plan-only work with no credentials at all -----------------------
    settings = LLMSettings.from_env()
    if args.endpoint:
        settings.endpoint = args.endpoint
    if args.model:
        settings.model = args.model
    if not settings.api_key:
        raise ConfigError(
            "no API key found. Set AI_REVIEW_API_KEY (or OPENAI_API_KEY / "
            "ANTHROPIC_API_KEY). In the workflow this comes from the "
            "AI_REVIEW_API_KEY repository secret. The bot deliberately fails "
            "loudly here instead of quietly skipping the review, because a "
            "silent skip looks identical to 'reviewed and clean'."
        )

    client = LLMClient(settings)
    raw_findings: List[Finding] = []
    errors: List[str] = []
    for chunk in result.chunks:
        user_prompt = build_user_prompt(chunk, len(result.chunks), args.min_severity)
        try:
            response = client.complete(system_prompt, user_prompt)
            raw_findings.extend(parse_findings(response))
        except ResponseFormatError as exc:
            errors.append(f"chunk {chunk.index + 1} ({', '.join(chunk.paths)}): {exc}")
            sys.stderr.write(
                f"[review] REJECTED output for chunk {chunk.index + 1}: {exc}\n"
            )
        except TransportError as exc:
            errors.append(f"chunk {chunk.index + 1}: {exc}")
            sys.stderr.write(f"[review] API error for chunk {chunk.index + 1}: {exc}\n")

    result.chunks_failed = len(errors)
    if errors and len(errors) == len(result.chunks):
        raise ResponseFormatError(
            "every chunk failed, so no review could be produced. Nothing was "
            "posted. First error:\n" + errors[0]
        )

    kept, dropped = filter_findings(
        raw_findings,
        min_severity=args.min_severity,
        max_findings=args.max_findings,
        changed_paths=changed_paths,
    )
    result.findings = kept
    result.dropped = dropped

    header = args.header
    if errors:
        header += f" (partial: {len(errors)} of {len(result.chunks)} chunk(s) failed)"

    result.body = render_comment(
        kept,
        dropped_count=len(dropped),
        files_reviewed=len(result.reviewed_files),
        files_skipped=len(result.skipped_files),
        min_severity=args.min_severity,
        max_findings=args.max_findings,
        header=header,
        report_url=args.report_url,
    )

    if args.body_file:
        Path(args.body_file).write_text(result.body, encoding="utf-8")
        sys.stderr.write(f"[review] comment body written to {args.body_file}\n")

    if not args.pr_url:
        sys.stdout.write(result.body + "\n")
        sys.stderr.write(
            "\n[review] no --pr-url given, so nothing was posted. Add --pr-url "
            "https://github.com/<owner>/<repo>/pull/<n> to publish this comment.\n"
        )
        return 0

    return _publish(result, args, number=args.pr_number)


def _publish(result: RunResult, args: argparse.Namespace, number: int) -> int:
    if not args.pr_url:
        sys.stdout.write(result.body + "\n")
        sys.stderr.write("[review] no --pr-url given, so nothing was posted.\n")
        return 0
    owner, repo, parsed_number = parse_pr_url(args.pr_url)
    pr_number = number or parsed_number
    token = os.environ.get("GITHUB_TOKEN", "")
    client = GitHubClient(repo=f"{owner}/{repo}", token=token)
    action, url = client.upsert_comment(pr_number, result.body)
    result.action = action
    result.comment_url = url
    sys.stderr.write(
        f"[review] comment {action} on {owner}/{repo}#{pr_number}"
        + (f": {url}" if url else "")
        + "\n"
    )
    return 0


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pr-review-lint",
        description=(
            "Rule-driven first-pass review of a pull request diff. Reads the diff "
            "from a file or stdin so it can be tested locally, sends it to a "
            "configurable chat API together with your team's rule files, and "
            "posts or updates a single PR comment."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  # see what would be reviewed, no API key needed\n"
            "  python3 review.py --diff fixtures/sample.diff --plan-only\n\n"
            "  # full dry run: prints the exact comment body, calls no API\n"
            "  python3 review.py --diff fixtures/sample.diff --dry-run\n\n"
            "  # against a live PR (needs GITHUB_TOKEN and AI_REVIEW_API_KEY)\n"
            "  git diff origin/main...HEAD | python3 review.py "
            "--pr-url https://github.com/me/repo/pull/12\n\n"
            "Installed as a console script, every one of those is just\n"
            "  pr-review-lint --diff fixtures/sample.diff --dry-run\n"
        ),
    )
    parser.add_argument("--diff", help="path to a unified diff, or - for stdin")
    parser.add_argument(
        "--rules",
        action="append",
        default=None,
        help=(
            "glob or path to a markdown rule file; repeatable. Defaults to the "
            "RULES_GLOB environment variable, then 'rules/*.md', "
            "'rules-example/*.md', and finally the example rules bundled in the "
            "installed package."
        ),
    )
    parser.add_argument(
        "--pr-url",
        help="PR URL; when given, the comment is posted or updated there",
    )
    parser.add_argument("--pr-number", type=int, default=0, help="override the PR number")
    parser.add_argument(
        "--min-severity",
        default=os.environ.get("AI_REVIEW_MIN_SEVERITY", DEFAULT_MIN_SEVERITY),
        choices=list(SEVERITIES),
        help=f"do not report findings below this severity (default: {DEFAULT_MIN_SEVERITY})",
    )
    parser.add_argument(
        "--max-findings",
        type=int,
        default=int(os.environ.get("AI_REVIEW_MAX_FINDINGS", DEFAULT_MAX_FINDINGS)),
        help=f"hard cap on findings per run (default: {DEFAULT_MAX_FINDINGS})",
    )
    parser.add_argument(
        "--chunk-chars",
        type=int,
        default=int(os.environ.get("AI_REVIEW_CHUNK_CHARS", DEFAULT_CHUNK_CHARS)),
        help=f"maximum characters of diff per API request (default: {DEFAULT_CHUNK_CHARS})",
    )
    parser.add_argument(
        "--max-total-chars",
        type=int,
        default=int(os.environ.get("AI_REVIEW_MAX_TOTAL_CHARS", DEFAULT_MAX_TOTAL_CHARS)),
        help="refuse to review a diff larger than this",
    )
    parser.add_argument("--endpoint", help="override AI_REVIEW_ENDPOINT")
    parser.add_argument("--model", help="override AI_REVIEW_MODEL")
    parser.add_argument(
        "--header",
        default=os.environ.get("AI_REVIEW_HEADER", "Automated first pass"),
        help="heading text for the posted comment",
    )
    parser.add_argument("--report-url", help="link added to the comment footer")
    parser.add_argument(
        "--body-file",
        "--emit-body",
        dest="body_file",
        help=(
            "also write the comment body to this path. The workflow uses this to "
            "attach the review as a run artifact and to build the job summary."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print the plan and the exact comment body; call no API, post nothing",
    )
    parser.add_argument(
        "--plan-only",
        action="store_true",
        help="print only what would be reviewed and skipped; call no API",
    )
    parser.add_argument("--version", action="version", version=f"pr-review-lint {VERSION}")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.rules is None:
        env_glob = os.environ.get("RULES_GLOB", "")
        if env_glob.strip():
            args.rules = [p for p in re.split(r"[,\n]", env_glob) if p.strip()]
        else:
            # Tried in order: the team's own rule files next to the working
            # directory (the documented workflow), then the four example rule
            # files bundled in the package. Without the last one, an installed
            # `pr-review-lint` would fail from any directory that has no rules/.
            args.rules = ["rules/*.md", "rules-example/*.md", BUNDLED_RULES_GLOB]

    try:
        return run(args)
    except ReviewError as exc:
        sys.stderr.write(f"\n[review] ERROR: {exc}\n")
        return exc.exit_code
    except KeyboardInterrupt:
        sys.stderr.write("\n[review] interrupted\n")
        return 130


if __name__ == "__main__":
    sys.exit(main())
