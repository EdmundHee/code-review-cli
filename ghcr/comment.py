"""Pure builders/parsers for the bot comment body + its hidden SHA marker.

The marker is an HTML comment (invisible when rendered, greppable in raw body).
It is the human-visible / GitHub-side record of which head SHA was handled.
"""

from __future__ import annotations

import re

from .models import PullRequest

MARKER_VERSION = "1"
_MARKER_RE = re.compile(r"ghcr:reviewed sha=([0-9a-f]{40})")


def marker(sha: str, model: str) -> str:
    return f"<!-- ghcr:reviewed sha={sha} model={model} v={MARKER_VERSION} -->"


def extract_marker_shas(body: str) -> set[str]:
    return set(_MARKER_RE.findall(body or ""))


def _skipped_note(skipped_files: list[str]) -> str:
    if not skipped_files:
        return ""
    shown = ", ".join(skipped_files[:5])
    more = f" +{len(skipped_files) - 5} more" if len(skipped_files) > 5 else ""
    return f" ({len(skipped_files)} skipped: {shown}{more})"


def build_comment(
    *,
    content: str,
    pr: PullRequest,
    model: str,
    timestamp: str,
    kept_files: int,
    skipped_files: list[str],
    changed_lines: int,
    truncated: bool = False,
    trigger_note: str = "",
) -> str:
    short = pr.head_sha[:7]
    trunc = " · diff truncated" if truncated else ""
    note = f"_{trigger_note}_\n\n" if trigger_note else ""
    return (
        f"{marker(pr.head_sha, model)}\n"
        f"### 🤖 Automated code review\n"
        f"`{model}` · commit `{short}` · {timestamp}\n\n"
        f"{note}"
        f"{content.strip()}\n\n"
        f"---\n"
        f"<sub>GithubCodeReview bot · diff: {changed_lines} lines / {kept_files} files"
        f"{_skipped_note(skipped_files)}{trunc} · automated — verify before acting.</sub>\n"
    )


_NOTICE_TITLES = {
    "oversized": "Diff too large — automated review skipped",
    "budget": "Daily review budget reached — skipped",
}


def build_notice_comment(
    *, kind: str, pr: PullRequest, model: str, timestamp: str, detail: str
) -> str:
    short = pr.head_sha[:7]
    title = _NOTICE_TITLES.get(kind, "Automated review skipped")
    return (
        f"{marker(pr.head_sha, model)}\n"
        f"### 🤖 {title}\n"
        f"`{model}` · commit `{short}` · {timestamp}\n\n"
        f"{detail}\n"
    )
