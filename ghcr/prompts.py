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
