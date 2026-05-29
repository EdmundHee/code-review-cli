"""Review prompt templates. Pure string construction."""

from __future__ import annotations

from .diff_filter import FilteredDiff
from .models import PullRequest

SYSTEM_PROMPT = """\
You are a senior software engineer performing a focused code review of a single \
GitHub pull request. You see only the unified diff plus minimal PR metadata — \
you do NOT have the full repository, so avoid assumptions about code you cannot \
see and do not ask for files.

Prioritize, in order:
1. Correctness & bugs — logic errors, off-by-one, null/None handling, race \
conditions, incorrect error handling, broken edge cases, API misuse.
2. Security — injection, auth/authorization gaps, secret leakage, unsafe \
deserialization, SSRF, path traversal, unvalidated input.
3. Performance & style — only as MINOR notes; do not pad the review with nits.

Rules:
- Be specific: reference file paths and, where possible, the changed line/hunk.
- If you are unsure, say so rather than inventing problems.
- Do not restate what the code does at length. No praise padding.
- If you find no substantive issues, say so plainly.

Return GitHub-flavored Markdown ONLY, in this structure:
## Summary
<2-4 sentence overview and overall risk read>

## Findings
- **[BLOCKER]** <file:area> — <issue and suggested fix>
- **[WARNING]** <file:area> — <issue and suggested fix>
- **[MINOR]** <file:area> — <issue and suggested fix>
(Omit a severity group entirely if it has no items.)

## Notes
<optional: assumptions made, parts of the diff that were skipped/truncated>
"""


# ===========================================================================
# Multi-pass pipeline prompts.
#
# Each lens is an independent, diff-only review with one narrow focus. Lenses
# emit STRUCTURED JSON (parsed in review.py) rather than prose, so findings can
# be merged, scored, and filtered. A separate scoring pass rates each finding's
# confidence 0-100 and we keep only the high-confidence ones — this is what
# trades cost for accuracy and kills nit padding.
#
# Every prompt carries a unique header line ("## LENS: <name>" / "## PASS:
# scoring") so a model never confuses its job — and so the test fake can route a
# canned response by matching that header.
# ===========================================================================

LENS_NAMES = ("correctness", "security", "maintainability", "test_coverage")

# Lifted ~verbatim from Claude Code's /code-review false-positive list. Embedded
# in every lens so each self-filters before a finding ever reaches scoring.
_FALSE_POSITIVE_GUIDANCE = """\
Do NOT report any of the following — for this review they are false positives:
- Pre-existing issues on lines this PR does not modify.
- Things that look like a bug but are intentional, or are part of the broader change.
- Pedantic nitpicks a senior engineer would not raise in review.
- Anything a linter, type checker, compiler, or formatter would catch (imports, type \
errors, formatting, style) — assume CI runs these separately.
- General code-quality wishes (more docs, more abstraction) with no concrete defect.
- Issues explicitly silenced in the code (e.g. a lint-ignore comment).
If you are unsure whether something is real, do NOT invent it — omit it."""

_LENS_BASE = """\
You are a senior software engineer reviewing exactly ONE GitHub pull request. You see \
only the unified diff plus PR metadata — you do NOT have the full repository, so never \
assume code you cannot see and never ask for files. Stay strictly within your assigned \
lens below; other reviewers cover other concerns, so do not duplicate them or pad."""

_LENS_FINDINGS_SPEC = """\
Return ONLY a JSON array — no prose, no markdown fences. Each element is an object:
{"severity": "BLOCKER" | "WARNING" | "MINOR", "file": "<path>", "area": "<symbol or hunk>", \
"issue": "<what is wrong>", "fix": "<concrete suggested fix>"}
If your lens finds nothing, return exactly []."""

_LENS_FOCUS = {
    "correctness": """\
## LENS: correctness
Find real correctness bugs: logic errors, off-by-one, null/None and empty handling, \
incorrect error handling, broken edge cases, race conditions, resource leaks, and API \
misuse. Prefer a few high-impact bugs over many small ones.""",
    "security": """\
## LENS: security
Find security defects introduced or exposed by this diff: injection (SQL/command/template), \
authentication/authorization gaps, secret or credential leakage, SSRF, path traversal, \
unsafe deserialization, and unvalidated/untrusted input reaching a sink.""",
    "maintainability": """\
## LENS: maintainability
Report ONLY genuinely significant maintainability problems: dangerous duplication, missing \
critical error handling, confusing control flow that will cause future bugs, or a public \
contract changed in a breaking way. Do not list style or taste preferences.""",
    "test_coverage": """\
## LENS: test_coverage
Decide whether this PR ships unit tests for the behavior it develops. Identify new or \
changed BEHAVIOR in non-test files (new functions, endpoints, branches, bug fixes) and \
check whether THIS SAME diff adds or updates tests that exercise it.
- If new/changed behavior has a corresponding test in the diff: has_tests = true.
- If the change does not need tests (docs, comments, config, formatting, or a pure \
mechanical refactor with unchanged behavior): has_tests = true, and say so in detail.
- If real new behavior lacks a test in this diff: has_tests = false, and add ONE WARNING \
finding naming the untested symbol/file.""",
}

# test_coverage returns an object (verdict + findings); the others return an array.
_COVERAGE_OUTPUT_SPEC = """\
Return ONLY a JSON object — no prose, no markdown fences:
{"verdict": {"has_tests": true | false, "detail": "<one concise line>"},
 "findings": [ {"severity": "...", "file": "...", "area": "...", "issue": "...", "fix": "..."} ]}
findings is [] unless real behavior is untested."""


def _build_lens_prompt(name: str) -> str:
    focus = _LENS_FOCUS[name]
    spec = _COVERAGE_OUTPUT_SPEC if name == "test_coverage" else _LENS_FINDINGS_SPEC
    return f"{_LENS_BASE}\n\n{focus}\n\n{_FALSE_POSITIVE_GUIDANCE}\n\n{spec}\n"


LENS_PROMPTS = {name: _build_lens_prompt(name) for name in LENS_NAMES}


def coverage_hint(has_source_changes: bool, has_test_changes: bool) -> str:
    """A factual signal appended to the test_coverage lens's user prompt. Pure
    file-path arithmetic — the model still makes the judgment call."""
    return (
        "\nTest-file signal (path heuristic, not a verdict): "
        f"diff changes non-test/source files = {has_source_changes}; "
        f"diff adds or changes test files = {has_test_changes}.\n"
    )


SCORING_SYSTEM_PROMPT = """\
## PASS: scoring
You are scoring ONE finding raised by a code reviewer against a GitHub pull request diff. \
You see only the diff and the single finding. Rate your confidence that the finding is a \
REAL, worth-reporting issue, on this 0-100 scale (use it verbatim):
- 0: Not confident at all. A false positive that does not survive light scrutiny, or a \
pre-existing issue on unchanged lines.
- 25: Somewhat confident. Might be real, might be a false positive; you could not verify it. \
A stylistic point not explicitly required.
- 50: Moderately confident. Verified real, but possibly a nitpick or rare in practice; \
relative to the PR it is not very important.
- 75: Highly confident. You double-checked; very likely a real issue hit in practice, and \
the PR's current approach is insufficient. Important to functionality.
- 100: Absolutely certain. Confirmed a definite real issue that will happen frequently; the \
diff itself is direct evidence.

""" + _FALSE_POSITIVE_GUIDANCE + """

Return ONLY a JSON object — no prose, no markdown fences:
{"confidence": <integer 0-100>, "reason": "<one concise line>"}
"""


def build_scoring_user_prompt(pr: PullRequest, fd: FilteredDiff, finding) -> str:
    """User payload for one scoring call: the finding under review + the diff."""
    head = (
        f"Repository: {pr.repo}  PR #{pr.number}: {pr.title}\n\n"
        f"Finding to score (raised by the '{finding.lens}' lens):\n"
        f"- severity: {finding.severity}\n"
        f"- file: {finding.file}\n"
        f"- area: {finding.area}\n"
        f"- issue: {finding.issue}\n"
        f"- suggested fix: {finding.fix}\n"
    )
    return f"{head}\nUnified diff:\n```diff\n{fd.text}\n```\n"


def build_user_prompt(pr: PullRequest, fd: FilteredDiff, truncated: bool = False) -> str:
    skipped = ", ".join(fd.skipped_paths) if fd.skipped_paths else "none"
    header = (
        f"Repository: {pr.repo}\n"
        f"PR #{pr.number}: {pr.title}\n"
        f"Author: {pr.author}\n"
        f"Branch: {pr.base_ref} <- {pr.head_ref}\n"
        f"Head commit: {pr.head_sha}\n"
        f"Files reviewed: {len(fd.kept_paths)}; changed lines: {fd.changed_lines}\n"
        f"Files skipped as generated/vendored/binary: {skipped}\n"
    )
    if truncated:
        header += "NOTE: the diff was truncated due to size; review only what is shown.\n"
    return f"{header}\nUnified diff:\n```diff\n{fd.text}\n```\n"
