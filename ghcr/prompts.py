"""Review prompt templates. Pure string construction."""

from __future__ import annotations

from .diff_filter import FilteredDiff, file_hunks
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

# Lifted from Claude Code's /code-review false-positive list, caveman-compressed:
# instructions stay terse to save input tokens on every call; semantics unchanged.
# Embedded in every lens so each self-filters before a finding ever reaches scoring.
_FALSE_POSITIVE_GUIDANCE = """\
Do NOT report — false positives here:
- Pre-existing issues on lines this PR does not modify.
- Intentional behavior, or part of the broader change.
- Pedantic nitpicks a senior engineer would not raise.
- Anything a linter, type checker, compiler, or formatter would catch (imports, type \
errors, formatting, style) — CI runs these.
- Generic code-quality wishes (docs, abstraction) with no concrete defect.
- Issues explicitly silenced in code (e.g. lint-ignore comment).
Unsure if real → omit, do not invent."""

_LENS_BASE = """\
Senior engineer reviewing exactly ONE GitHub pull request. Input: unified diff + PR \
metadata + optional "REFERENCED DEFINITIONS" section (real source of symbols the diff \
uses but does not define) — those definitions are authoritative ground truth. An entry \
marked "NOT a verified definition" or a symbol listed UNRESOLVED is NOT evidence. If a \
finding hinges on a symbol in NEITHER the diff NOR the referenced definitions, you cannot \
verify it: do not assume how it "usually" behaves — omit it, or report at reduced severity \
(WARNING/MINOR) and say what you would need to confirm. Never ask for files. Stay strictly \
inside your lens below; other reviewers cover the rest — no duplication, no padding."""

_LENS_FINDINGS_SPEC = """\
Return ONLY a JSON array — no prose, no markdown fences. Element shape:
{"severity": "BLOCKER" | "WARNING" | "MINOR", "file": "<path>", "area": "<symbol or hunk>", \
"issue": "<what is wrong>", "fix": "<concrete suggested fix>"}
Write "issue" and "fix" as clear full sentences — they are posted verbatim to humans in the \
review comment.
Severity follows what your EVIDENCE proves, not how bad it would be if true:
- BLOCKER: diff (+ referenced definitions) directly prove a correctness/security defect — \
zero assumptions about unseen code.
- WARNING: likely real, OR severity hinges on context/behavior not fully shown.
- MINOR: small or local, OR rests on an assumption about code you were not given.
Lens finds nothing → exactly []."""

_LENS_FOCUS = {
    "correctness": """\
## LENS: correctness
Find real correctness bugs: logic errors, off-by-one, null/None/empty handling, wrong \
error handling, broken edge cases, race conditions, resource leaks, API misuse. Few \
high-impact bugs beat many small ones.""",
    "security": """\
## LENS: security
Find security defects this diff introduces or exposes: injection (SQL/command/template), \
authentication/authorization gaps, secret/credential leakage, SSRF, path traversal, \
unsafe deserialization, untrusted input reaching a sink.""",
    "maintainability": """\
## LENS: maintainability
ONLY genuinely significant maintainability problems: dangerous duplication, missing \
critical error handling, control flow confusing enough to breed future bugs, breaking \
change to a public contract. No style or taste notes.""",
    "test_coverage": """\
## LENS: test_coverage
Decide: does this PR ship unit tests for the behavior it develops? Identify new/changed \
BEHAVIOR in non-test files (new functions, endpoints, branches, bug fixes); check whether \
THIS SAME diff adds or updates tests exercising it.
- New/changed behavior has a matching test in the diff: has_tests = true.
- Change needs no tests (docs, comments, config, formatting, pure mechanical refactor with \
unchanged behavior): has_tests = true, say so in detail.
- Real new behavior lacks a test in this diff: has_tests = false, add ONE WARNING finding \
naming the untested symbol/file.""",
}

# test_coverage returns an object (verdict + findings); the others return an array.
_COVERAGE_OUTPUT_SPEC = """\
Return ONLY a JSON object — no prose, no markdown fences:
{"verdict": {"has_tests": true | false, "detail": "<one concise line>"},
 "findings": [ {"severity": "...", "file": "...", "area": "...", "issue": "...", "fix": "..."} ]}
findings stays [] unless real behavior is untested. Write "issue", "fix", and "detail" as \
clear full sentences — they are posted verbatim to humans in the review comment."""


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
You score ONE finding raised by a code reviewer against a pull request. Input: the \
finding, the diff hunks for its file, optional REFERENCED DEFINITIONS (authoritative real \
source of symbols the diff does not define), optional PRIOR PR DISCUSSION.
First actively try to REFUTE the finding; score what survives. The reviewer who raised it \
saw the same evidence you do — agreement is not verification.
Rate confidence the finding is a REAL, worth-reporting issue, 0-100 (use scale verbatim):
- 0: False positive; does not survive light scrutiny; pre-existing issue on unchanged \
lines; or ALREADY RAISED in the PRIOR PR DISCUSSION (by this bot on an earlier commit or \
by a human) and this diff does not reintroduce or worsen it.
- 25: Might be real, might not; you could not verify. Or a stylistic point not explicitly \
required.
- 50: Verified real, but a nitpick or rare in practice; not important to this PR.
- 75: Double-checked; very likely real and hit in practice; PR's current approach \
insufficient; matters to functionality.
- 100: Certain. Definite, frequent issue; the diff (or a referenced definition) is direct \
evidence.

Crucial: the finding depends on the behavior or meaning of a symbol in NEITHER the diff \
NOR the referenced definitions — or the symbol is listed UNRESOLVED, or its snippet is \
marked "NOT a verified definition"? You cannot verify it. Cap confidence at 25: prior \
knowledge of how such code "usually" behaves is NOT evidence; only the diff and referenced \
definitions are.

""" + _FALSE_POSITIVE_GUIDANCE + """

Return ONLY a JSON object — no prose, no markdown fences:
{"confidence": <integer 0-100>, "reason": "<one concise line>"}
"""


# Planner pass: names the unseen symbols whose definitions the reviewer must read.
# review.py resolves each via gh and feeds the result back as REFERENCED DEFINITIONS.
CONTEXT_REQUEST_PROMPT = """\
## PASS: context
You prepare a code review of ONE GitHub pull request; you see only the unified diff. List \
the symbols whose DEFINITION must be read to judge the diff correctly but which the diff \
does NOT itself define — e.g. a base class it subclasses, a function/method it calls, a \
decorator it applies, the type of a parameter/field whose meaning drives correctness or \
security. These are what a reviewer would otherwise GUESS about.
Per symbol: bare name + a module hint when the diff reveals its origin (an import or \
dotted module path). Skip what the diff defines, builtins/stdlib, trivial well-known \
helpers. Few load-bearing symbols beat a long speculative list.
Diff changes a default value, registry entry, public constant, or documented behavior \
that existing tests likely pin → request those EXISTING TESTS: same symbol, kind "tests" \
(e.g. a changed DEFAULT_TIMEOUT default → the test asserting the old value). Only when a \
pinned behavior changes; never speculative.

Return ONLY a JSON object — no prose, no markdown fences:
{"requests": [{"symbol": "<name>", "module_hint": "<import or dotted path, or empty>", \
"reason": "<why it matters>", "kind": "definition" | "tests"}]}
"kind" omitted → "definition". Self-contained diff → {"requests": []}.
"""


def _prior_block(prior_context: str) -> str:
    """A blank-line-padded prior-discussion block, or '' when none was supplied."""
    return f"\n{prior_context.strip()}\n" if prior_context.strip() else ""


def _referenced_block(referenced_context: str) -> str:
    """A blank-line-padded referenced-definitions block, or '' when none was supplied."""
    return f"\n{referenced_context.strip()}\n" if referenced_context.strip() else ""


def build_context_request_user_prompt(pr: PullRequest, fd: FilteredDiff) -> str:
    """User payload for the planner pass: PR metadata + the diff to scan for the
    unseen symbols whose definitions the reviewer needs."""
    header = f"Repository: {pr.repo}\nPR #{pr.number}: {pr.title}\n"
    return f"{header}\nUnified diff:\n```diff\n{fd.text}\n```\n"


def build_scoring_user_prompt(
    pr: PullRequest, fd: FilteredDiff, finding, prior_context: str = "", referenced_context: str = ""
) -> str:
    """User payload for one scoring call: the finding under review + its file's hunks.

    Only the finding's file is sent (the full diff is the dominant token cost when it is
    re-sent per finding per vote); falls back to the whole diff when the finding's path
    is not in the diff. ``prior_context`` lets the scorer return 0 for a finding already
    raised in the PR's discussion; ``referenced_context`` supplies the real definitions of
    symbols the diff references, so the scorer can verify (or cap) the finding (see
    SCORING_SYSTEM_PROMPT)."""
    head = (
        f"Repository: {pr.repo}  PR #{pr.number}: {pr.title}\n\n"
        f"Finding to score (raised by the '{finding.lens}' lens):\n"
        f"- severity: {finding.severity}\n"
        f"- file: {finding.file}\n"
        f"- area: {finding.area}\n"
        f"- issue: {finding.issue}\n"
        f"- suggested fix: {finding.fix}\n"
    )
    hunks = file_hunks(fd.text, finding.file)
    if hunks:
        diff_label = f"Diff for {finding.file} (other files in this PR omitted)"
        diff_body = hunks
    else:
        diff_label = "Unified diff"
        diff_body = fd.text
    return (
        f"{head}{_referenced_block(referenced_context)}{_prior_block(prior_context)}"
        f"\n{diff_label}:\n```diff\n{diff_body}\n```\n"
    )


def build_user_prompt(
    pr: PullRequest, fd: FilteredDiff, truncated: bool = False,
    prior_context: str = "", referenced_context: str = "",
    chunk_index: int = 0, chunk_total: int = 1,
) -> str:
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
    if chunk_total > 1:
        header += (
            f"NOTE: this is part {chunk_index}/{chunk_total} of this PR's diff, split by whole "
            "files; other parts (which may contain the tests) are reviewed separately.\n"
        )
    return (
        f"{header}{_referenced_block(referenced_context)}{_prior_block(prior_context)}"
        f"\nUnified diff:\n```diff\n{fd.text}\n```\n"
    )
